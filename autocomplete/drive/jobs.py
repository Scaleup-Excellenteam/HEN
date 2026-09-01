"""The import and removal lifecycle, and the state the server searches.

One object owns everything the feature has: the settings, the store, the
imported corpus currently being searched, and the single slot in which a change
to it can be running.

Serving during a change
-----------------------

The imported corpus is published by assigning one completed
:class:`~autocomplete.drive.store.ImportedCorpus` to :attr:`DriveService.corpus`.
A query reads that attribute once and searches what it got, so it sees either
the state before a change or the state after it. There is no moment at which a
half-built index is reachable, because nothing partial is ever assigned there:
the new generation is written, indexed, validated and adopted on disk first, and
only the finished object is published. This is the same handover the corpus
index already uses, for the same reason.

A failure therefore costs nothing that was working. The attribute still holds
the last good state, the pointer on disk still names the generation that state
came from, and the job records why it stopped.

One change at a time
--------------------

Two imports running together would each build a generation from a different idea
of what the document set is, and the later pointer would win, silently discarding
the other's work. So a change takes a slot, and a second request while it is held
is refused with an error the interface can explain rather than being queued into
a race. Searching is not affected: it never takes the slot.

States
------

``disabled`` the feature is off or unconfigured; ``ready`` nothing is running;
``downloading``, ``building`` and ``adopting`` are the three phases of a change;
``failed`` the last change stopped and why. A sixth state, ``disconnected``, is
the browser's to report: whether it holds an authorization is not something the
server can know, so :meth:`DriveService.status` says whether one is *needed*
and the interface turns that into the state a person sees.

Progress is counted, never estimated. Files selected, files downloaded, bytes
downloaded and lines read are all things the job actually knows. A percentage
would have to be invented, because the cost of indexing is not known before it
is done, so none is offered.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..config import Config
from ..index import SearchIndex
from .client import DriveClient, DriveFile, HttpDriveClient
from .documents import fetch_document
from .errors import (
    DocumentNotFoundError,
    DriveDisabledError,
    DriveError,
    DriveNotConfiguredError,
    ImportLimitError,
    JobInProgressError,
    StoreCorruptError,
)
from .settings import (
    DRIVE_SCOPE,
    DRIVE_SOURCE_PREFIX,
    DriveSettings,
    load_settings,
)
from .store import DriveStore, ImportedCorpus, ImportedDocument, PreparedDocument

__all__ = [
    "MAX_FILE_ID_LENGTH",
    "DriveService",
    "ImportJob",
    "JobKind",
    "JobState",
    "Progress",
    "ServiceState",
]

logger = logging.getLogger("autocomplete.drive")

#: Drive identifiers are short. A cap stops a request body from carrying
#: something that is not one.
MAX_FILE_ID_LENGTH = 256

Worker = Callable[[Callable[[], None]], None]
ClientFactory = Callable[[str], DriveClient]


class JobState(str, Enum):
    """Where one change has got to."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    BUILDING = "building"
    ADOPTING = "adopting"
    COMPLETE = "complete"
    FAILED = "failed"

    @property
    def finished(self) -> bool:
        return self in (JobState.COMPLETE, JobState.FAILED)


class ServiceState(str, Enum):
    """What the feature as a whole is doing, for the interface to render."""

    DISABLED = "disabled"
    READY = "ready"
    DOWNLOADING = "downloading"
    BUILDING = "building"
    ADOPTING = "adopting"
    FAILED = "failed"


class JobKind(str, Enum):
    IMPORT = "import"
    REMOVE = "remove"


@dataclass
class Progress:
    """What a running change has actually accomplished.

    Attributes:
        files_selected: How many documents this change covers.
        files_downloaded: How many have been fetched from Drive so far.
        files_reused: How many were already stored and did not need fetching.
        bytes_downloaded: Total bytes fetched in this change.
        lines_read: Lines in the text fetched so far. Counted while
            downloading, so it is known before indexing starts.
        sentences_indexed: Searchable sentences in the finished index. Known
            only once it is built, and lower than ``lines_read`` because blank
            lines and lines that normalize to nothing are not stored.
        detail: One sentence describing the current step.
    """

    files_selected: int = 0
    files_downloaded: int = 0
    files_reused: int = 0
    bytes_downloaded: int = 0
    lines_read: int = 0
    sentences_indexed: int = 0
    detail: str = ""

    def to_json(self) -> dict:
        return {
            "files_selected": self.files_selected,
            "files_downloaded": self.files_downloaded,
            "files_reused": self.files_reused,
            "bytes_downloaded": self.bytes_downloaded,
            "lines_read": self.lines_read,
            "sentences_indexed": self.sentences_indexed,
            "detail": self.detail,
        }


@dataclass
class ImportJob:
    """One import or removal, and how it went."""

    id: str
    kind: JobKind
    state: JobState = JobState.PENDING
    progress: Progress = field(default_factory=Progress)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    #: What to run again on a retry. File identifiers for an import, a document
    #: identifier for a removal. Never the access token, which is not kept.
    file_ids: tuple[str, ...] = ()
    document_id: str | None = None
    started_at: str = ""
    finished_at: str | None = None

    @property
    def needs_authorization(self) -> bool:
        """Whether re-running this job would need a fresh token from Google."""
        return self.kind is JobKind.IMPORT

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "state": self.state.value,
            "progress": self.progress.to_json(),
            "error": (
                None
                if self.error_code is None
                else {
                    "code": self.error_code,
                    "message": self.error_message,
                    "retryable": self.retryable,
                }
            ),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "needs_authorization": self.needs_authorization,
        }


def _in_background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="drive-import", daemon=True).start()


class DriveService:
    """The feature's state, and the one place a change to it can start.

    Args:
        settings: What the feature may do. Defaults to the environment.
        config: Used for the number of results, so the imported index answers
            as many as the corpus one, and to anchor the data directory.
        client_factory: Builds a Drive client from an access token. Replaced by
            tests, which is what lets every one of them run with no network.
        worker: Runs a job. The default starts a thread, so an import does not
            occupy the request that asked for it; tests substitute something
            they control.
        now: The clock, for deterministic timestamps in tests.
    """

    def __init__(
        self,
        settings: DriveSettings | None = None,
        config: Config | None = None,
        *,
        client_factory: ClientFactory | None = None,
        worker: Worker = _in_background,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.settings = settings if settings is not None else load_settings(config)
        self.results_wanted = config.num_results if config is not None else 5
        self._client_factory = client_factory or self._default_client
        self._worker = worker
        self._now = now or (lambda: datetime.now(timezone.utc))

        self._lock = threading.Lock()
        self._job: ImportJob | None = None
        #: The published imported corpus. Assigned whole, never mutated.
        self.corpus: ImportedCorpus | None = None
        #: Set when a state exists on disk but cannot be read. The feature then
        #: says so instead of quietly searching fewer sentences.
        self.load_error: str | None = None

        self.store = DriveStore(
            self.settings.data_dir,
            summary_width=self.results_wanted,
            use_mmap=config.use_mmap if config is not None else True,
        )

    # ------------------------------------------------------------ lifecycle ---

    def load_published_state(self) -> None:
        """Adopt whatever was published before, so imports survive a restart.

        Never raises: a corpus that cannot be read is recorded and reported
        through the status endpoint, because the search must keep working.
        """
        if not self.settings.configured:
            return
        try:
            self.corpus = self.store.load()
            self.load_error = None
            if self.corpus is not None:
                logger.info(
                    "imported corpus ready: %d document(s), %d sentences",
                    len(self.corpus.documents),
                    self.corpus.sentences,
                )
        except StoreCorruptError as failure:
            self.corpus = None
            self.load_error = failure.message
            logger.error("imported corpus could not be loaded: %s", failure.message)
        except Exception as failure:  # never take the search down for this
            self.corpus = None
            self.load_error = (
                f"The imported documents could not be loaded "
                f"({type(failure).__name__}). Searching the corpus only."
            )
            logger.error("imported corpus could not be loaded: %s", type(failure).__name__)

    @property
    def overlay(self) -> SearchIndex | None:
        """The index a search should consult alongside the corpus, if any.

        Read once per query. Because publication is a single assignment of a
        finished object, whatever comes back is a complete index.
        """
        corpus = self.corpus
        return None if corpus is None else corpus.index

    @property
    def documents(self) -> tuple[ImportedDocument, ...]:
        corpus = self.corpus
        return () if corpus is None else corpus.documents

    @property
    def state(self) -> ServiceState:
        if not self.settings.configured:
            return ServiceState.DISABLED
        job = self._job
        if job is None:
            return ServiceState.READY
        if job.state is JobState.DOWNLOADING:
            return ServiceState.DOWNLOADING
        if job.state is JobState.BUILDING:
            return ServiceState.BUILDING
        if job.state is JobState.ADOPTING:
            return ServiceState.ADOPTING
        if job.state is JobState.FAILED:
            return ServiceState.FAILED
        return ServiceState.READY

    @property
    def busy(self) -> bool:
        job = self._job
        return job is not None and not job.state.finished

    def job(self, job_id: str) -> ImportJob:
        job = self._job
        if job is None or job.id != job_id:
            raise DocumentNotFoundError("No import or removal has that identifier.")
        return job

    @property
    def last_job(self) -> ImportJob | None:
        return self._job

    def status(self) -> dict:
        """Everything the interface needs to render the feature."""
        job = self._job
        corpus = self.corpus
        return {
            "enabled": self.settings.enabled,
            "configured": self.settings.configured,
            "state": self.state.value,
            "detail": self._detail(),
            # Public configuration, served rather than compiled into the bundle
            # so a deployment can change it without a rebuild. Empty unless the
            # feature is fully configured, so a disabled server hands out
            # nothing at all.
            "client_id": self.settings.client_id if self.settings.configured else "",
            "api_key": self.settings.api_key if self.settings.configured else "",
            "app_id": self.settings.app_id if self.settings.configured else "",
            "scope": DRIVE_SCOPE,
            "source_prefix": DRIVE_SOURCE_PREFIX,
            "limits": {
                "max_files": self.settings.max_files,
                "max_file_bytes": self.settings.max_file_bytes,
                "max_total_bytes": self.settings.max_total_bytes,
                "supported_mime_types": list(self.settings.supported_mime_types),
            },
            "documents": len(self.documents),
            "sentences": 0 if corpus is None else corpus.sentences,
            "total_bytes": 0 if corpus is None else corpus.total_bytes,
            "job": None if job is None else job.to_json(),
            "load_error": self.load_error,
        }

    def _detail(self) -> str:
        if not self.settings.enabled:
            return "Google Drive import is switched off on this server."
        if not self.settings.configured:
            return self.settings.describe_missing()
        if self.load_error:
            return self.load_error
        job = self._job
        if job is not None and not job.state.finished:
            return job.progress.detail
        if job is not None and job.state is JobState.FAILED:
            return job.error_message or "The last change failed."
        count = len(self.documents)
        if count == 0:
            return "No documents imported yet."
        return f"{count} document{'' if count == 1 else 's'} imported."

    # -------------------------------------------------------------- starting ---

    def start_import(self, file_ids: Sequence[str], access_token: str) -> ImportJob:
        """Begin importing the files the user selected in the picker."""
        self._require_available()
        wanted = _clean_file_ids(file_ids, self.settings.max_files)
        job = self._claim(JobKind.IMPORT, file_ids=wanted)
        job.progress.files_selected = len(wanted)
        job.progress.detail = f"Reading {len(wanted)} document(s) from Google Drive."
        # The token lives in this closure for the job's lifetime and is never
        # stored on the job, so nothing that is serialized or logged can hold it.
        self._run(job, lambda: self._do_import(job, wanted, access_token))
        return job

    def start_removal(self, document_id: str) -> ImportJob:
        """Begin removing one imported document."""
        self._require_available()
        document = self._find(document_id)
        job = self._claim(JobKind.REMOVE, document_id=document.id)
        job.progress.files_selected = 1
        job.progress.detail = f"Removing {document.name}."
        self._run(job, lambda: self._do_removal(job, document.id))
        return job

    def retry(self, access_token: str | None = None) -> ImportJob:
        """Run the last failed change again.

        An import needs a fresh authorization, because none was kept. A removal
        needs nothing, so it can simply be repeated.
        """
        self._require_available()
        previous = self._job
        if previous is None or previous.state is not JobState.FAILED:
            raise DocumentNotFoundError("There is no failed change to retry.")
        if previous.kind is JobKind.IMPORT:
            if not access_token:
                raise DriveError(
                    "Retrying an import needs a fresh Google authorization. "
                    "Connect to Google Drive and select the files again."
                )
            return self.start_import(previous.file_ids, access_token)
        return self.start_removal(previous.document_id or "")

    def _require_available(self) -> None:
        if not self.settings.enabled:
            raise DriveDisabledError(
                "Google Drive import is switched off on this server."
            )
        if not self.settings.configured:
            raise DriveNotConfiguredError(self.settings.describe_missing())

    def _claim(self, kind: JobKind, **plan) -> ImportJob:
        """Take the one slot a change may run in, or refuse."""
        with self._lock:
            if self.busy:
                raise JobInProgressError(
                    "Another import is already running. Wait for it to finish "
                    "and try again."
                )
            job = ImportJob(
                id=uuid.uuid4().hex,
                kind=kind,
                state=JobState.DOWNLOADING
                if kind is JobKind.IMPORT
                else JobState.BUILDING,
                started_at=self._now().isoformat(),
                **plan,
            )
            self._job = job
            return job

    def _run(self, job: ImportJob, work: Callable[[], None]) -> None:
        def guarded() -> None:
            try:
                work()
            except DriveError as failure:
                self._fail(job, failure.code, failure.message, failure.retryable)
            except Exception as failure:
                # Nothing internal reaches the interface: it gets a sentence it
                # can show, and the type goes to the log without the message,
                # which could quote a document.
                logger.error("%s job failed: %s", job.kind.value, type(failure).__name__)
                self._fail(
                    job,
                    "internal",
                    "The change could not be completed. The previously imported "
                    "documents are still searchable.",
                    retryable=True,
                )

        self._worker(guarded)

    def _fail(self, job: ImportJob, code: str, message: str, retryable: bool) -> None:
        job.error_code = code
        job.error_message = message
        job.retryable = retryable
        job.state = JobState.FAILED
        job.finished_at = self._now().isoformat()

    def _finish(self, job: ImportJob, corpus: ImportedCorpus) -> None:
        # The one assignment that publishes. Everything below it is complete.
        self.corpus = corpus
        self.load_error = None
        job.progress.sentences_indexed = corpus.sentences
        job.progress.detail = (
            f"{len(corpus.documents)} document"
            f"{'' if len(corpus.documents) == 1 else 's'} searchable."
        )
        job.state = JobState.COMPLETE
        job.finished_at = self._now().isoformat()

    # ---------------------------------------------------------------- work ----

    def _do_import(
        self, job: ImportJob, file_ids: Sequence[str], access_token: str
    ) -> None:
        client = self._client_factory(access_token)
        existing = {document.drive_file_id: document for document in self.documents}
        corpus = self.corpus

        fresh: dict[str, PreparedDocument] = {}
        for file_id in file_ids:
            metadata = client.metadata(file_id)
            known = existing.get(metadata.file_id)

            if known is not None and _unchanged(known, metadata):
                # Same file, same revision: there is nothing to fetch, and
                # re-downloading it would only cost the user's quota.
                job.progress.files_reused += 1
                job.progress.detail = f"{known.name} is already imported and unchanged."
                continue

            source_text = (
                known.source_text
                if known is not None
                else self.store.choose_source_text(
                    metadata.name,
                    [document.source_text for document in existing.values()]
                    + [item.document.source_text for item in fresh.values()],
                )
            )
            prepared = fetch_document(
                client, metadata, self.settings, source_text=source_text, now=self._now()
            )
            fresh[metadata.file_id] = prepared

            job.progress.files_downloaded += 1
            job.progress.bytes_downloaded += prepared.document.bytes
            job.progress.lines_read += (prepared.text or b"").count(b"\n")
            job.progress.detail = (
                f"Downloaded {job.progress.files_downloaded} of "
                f"{job.progress.files_selected} document(s)."
            )

        if not fresh:
            # Everything selected was already imported and unchanged. Nothing
            # needs rebuilding, so nothing is: the current state is the answer.
            job.progress.detail = "Everything selected was already imported."
            self._finish(job, corpus or self.store.publish([]))
            return

        proposed = self._compose(fresh)
        self._check_total(proposed)
        published = self.store.publish(
            proposed, on_stage=lambda stage: self._stage(job, stage)
        )
        self._finish(job, published)

    def _do_removal(self, job: ImportJob, document_id: str) -> None:
        document = self._find(document_id)
        corpus = self.corpus
        assert corpus is not None  # _find only succeeds when there is one

        remaining = [
            PreparedDocument(document=other, copy_from=corpus.source_path(other))
            for other in corpus.documents
            if other.id != document.id
        ]
        published = self.store.publish(
            remaining, on_stage=lambda stage: self._stage(job, stage)
        )
        self._finish(job, published)

    def _compose(
        self, fresh: dict[str, PreparedDocument]
    ) -> list[PreparedDocument]:
        """The whole document set the new generation will hold.

        Documents that were already imported and were not part of this request
        are carried over from the generation now serving, so a second import
        does not re-download the first one's files.
        """
        corpus = self.corpus
        carried: list[PreparedDocument] = []
        if corpus is not None:
            carried = [
                PreparedDocument(document=document, copy_from=corpus.source_path(document))
                for document in corpus.documents
                if document.drive_file_id not in fresh
            ]
        # Sorted so the same document set always produces the same generation,
        # whatever order the picker returned the files in.
        return sorted(
            carried + list(fresh.values()),
            key=lambda item: item.document.source_text,
        )

    def _check_total(self, proposed: Iterable[PreparedDocument]) -> None:
        total = sum(item.document.bytes for item in proposed)
        if total > self.settings.max_total_bytes:
            raise ImportLimitError(
                f"Importing these would bring the total to {total:,} bytes, over "
                f"the {self.settings.max_total_bytes:,} byte limit. Remove a "
                f"document first."
            )

    def _stage(self, job: ImportJob, stage: str) -> None:
        if stage == "building":
            job.state = JobState.BUILDING
            job.progress.detail = "Building the search index over the imported text."
        elif stage == "adopting":
            job.state = JobState.ADOPTING
            job.progress.detail = "Publishing the new imported index."

    def _find(self, document_id: str) -> ImportedDocument:
        for document in self.documents:
            if document.id == document_id:
                return document
        raise DocumentNotFoundError("No imported document has that identifier.")

    def _default_client(self, access_token: str) -> DriveClient:
        return HttpDriveClient(
            access_token,
            timeout=self.settings.timeout_seconds,
            retries=self.settings.retries,
        )


def _unchanged(known: ImportedDocument, metadata: DriveFile) -> bool:
    """Whether Drive's copy is the same revision as the one already imported.

    Compared on the revision identifier where Drive supplies one, and on the
    modification time otherwise. When Drive supplies neither, the answer is no,
    and the file is fetched again: importing the same content twice is wasteful,
    but serving stale content would be wrong.
    """
    if metadata.revision_id and known.revision_id:
        return metadata.revision_id == known.revision_id
    if metadata.modified_time and known.modified_time:
        return metadata.modified_time == known.modified_time
    return False


def _clean_file_ids(file_ids: Sequence[str], limit: int) -> tuple[str, ...]:
    """Check what the browser sent is a list of plausible Drive identifiers.

    Duplicates are collapsed rather than refused: a picker can return the same
    file twice, and importing it once is what the user meant.
    """
    if not file_ids:
        raise ImportLimitError("Select at least one document to import.")

    cleaned: list[str] = []
    for value in file_ids:
        if not isinstance(value, str):
            raise ImportLimitError("A selected file identifier is not text.")
        identifier = value.strip()
        if not identifier or len(identifier) > MAX_FILE_ID_LENGTH:
            raise ImportLimitError("A selected file identifier is not usable.")
        if any(not character.isprintable() for character in identifier):
            raise ImportLimitError("A selected file identifier is not usable.")
        if identifier not in cleaned:
            cleaned.append(identifier)

    if len(cleaned) > limit:
        raise ImportLimitError(
            f"{len(cleaned)} documents were selected, and at most {limit} can be "
            f"imported at once."
        )
    return tuple(cleaned)
