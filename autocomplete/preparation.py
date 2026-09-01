"""Running one preparation, and reporting how it went.

:func:`prepare` is the whole of it: it drives the phases the pipeline does not
drive itself, hands the pipeline a progress sink, and turns whatever goes wrong
into something a person can act on.

Why failures are rewritten
--------------------------

The exceptions raised underneath are written for a developer reading a terminal,
and several of them quote the path they were looking at. That path is on the
machine running the server, and the browser must never see it. So nothing here
passes an exception's own text outwards. Each known failure is matched to a
stable code, a sentence describing it, and a suggestion; anything unrecognised
becomes one fixed sentence and its *type* name, which is safe, rather than its
message, which is not.

This module knows nothing about HTTP. The command line does not use it and does
not import it; it exists so that an interface can watch a preparation without
either of them reimplementing what :func:`autocomplete.cache.build_or_load`
already does.
"""

from __future__ import annotations

import errno
import logging

from .cache import build_or_load, planned_mode
from .config import Config, ConfigError
from .corpus import CorpusNotFoundError
from .index import SearchIndex
from .progress import (
    NULL_SINK,
    BuildPhase,
    ProgressSink,
    ProgressTracker,
)
from .records import RecordStoreError
from .suffix_index import SuffixIndexError, verify_builder
from .topk import BlockSummaryError, DEFAULT_BLOCK_SIZE

__all__ = ["PreparationFailure", "describe_failure", "prepare"]

logger = logging.getLogger("autocomplete.preparation")


class PreparationFailure(RuntimeError):
    """A preparation that stopped, described in terms safe to show anyone.

    Attributes:
        code: A stable identifier the interface branches on.
        message: One sentence saying what happened.
        hint: What to try, when there is something sensible to try.
        phase: The phase that was running when it stopped.
    """

    def __init__(
        self, code: str, message: str, hint: str | None, phase: BuildPhase
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.phase = phase


def describe_failure(error: BaseException) -> tuple[str, str, str | None]:
    """Turn an exception into a code, a safe message and a suggestion.

    The exception's own text is never returned. Several of these carry the
    filesystem path they were looking at, which is exactly what must not leave
    the machine, so each message below names the *setting* to change rather than
    the value it currently holds.
    """
    if isinstance(error, CorpusNotFoundError):
        return (
            "corpus_missing",
            "The corpus directory was not found.",
            "Set corpus_root in config.yaml to the directory holding the "
            "extracted text files, then try again.",
        )
    if isinstance(error, ConfigError):
        return (
            "configuration_invalid",
            "The configuration could not be read.",
            "Check config.yaml for a mistyped key or value.",
        )
    if isinstance(error, SuffixIndexError):
        return (
            "suffix_builder_unavailable",
            "The suffix array builder is missing or not working.",
            "Install the dependencies with pip install -r requirements.txt, "
            "then check it with: python -c \"from autocomplete.suffix_index "
            "import verify_builder; verify_builder()\"",
        )
    if isinstance(error, RecordStoreError):
        return (
            "corpus_too_large",
            "The corpus could not be laid out for searching.",
            "The normalized corpus may be larger than this build can index. "
            "Reduce corpus_root, or check the message in the server log.",
        )
    if isinstance(error, BlockSummaryError):
        return (
            "summary_build_failed",
            "The block summaries could not be built.",
            "Rebuild the index with python main.py --rebuild.",
        )
    if isinstance(error, PermissionError):
        return (
            "permission_denied",
            "The corpus or the cache directory could not be read or written.",
            "Check the permissions on corpus_root and cache_dir in config.yaml.",
        )
    if isinstance(error, OSError):
        if error.errno == errno.ENOSPC:
            return (
                "disk_full",
                "There is not enough space to write the index.",
                "Free space on the volume holding cache_dir, then try again.",
            )
        return (
            "storage_error",
            "The index could not be read from or written to disk.",
            "Check that cache_dir in config.yaml is writable, then try again.",
        )
    if isinstance(error, MemoryError):
        return (
            "out_of_memory",
            "There was not enough memory to build the index.",
            "Close other programs and try again, or index a smaller corpus.",
        )
    # Nothing is assumed about an unrecognised failure, so nothing of its own
    # text is repeated. The type name is safe and is enough to find it in a log.
    return (
        "internal_error",
        f"Preparation stopped unexpectedly ({type(error).__name__}).",
        "Check the server log for details, then try again.",
    )


class _GuardedSink:
    """A sink that cannot break a build, whatever the real one does.

    Progress reporting is an accessory. A watcher that raises must not be able
    to abandon a build that was going to succeed, so every call is wrapped and a
    failure is logged once and then ignored. The wrapper costs one call per
    event, which is why the pipeline reports coarsely rather than per line.
    """

    def __init__(self, inner: ProgressSink) -> None:
        self._inner = inner
        self._complained = False

    def _swallow(self, error: BaseException) -> None:
        if not self._complained:
            self._complained = True
            logger.warning(
                "progress reporting failed and is being ignored: %s",
                type(error).__name__,
            )

    def begin(self, phase, *, detail="", total=None, determinate=None) -> None:
        try:
            self._inner.begin(
                phase, detail=detail, total=total, determinate=determinate
            )
        except Exception as error:
            self._swallow(error)

    def update(self, **fields) -> None:
        try:
            self._inner.update(**fields)
        except Exception as error:
            self._swallow(error)

    def note_cache_mode(self, mode) -> None:
        try:
            self._inner.note_cache_mode(mode)
        except Exception as error:
            self._swallow(error)


def prepare(
    config: Config,
    tracker: ProgressTracker | None = None,
    *,
    force_rebuild: bool = False,
    block_size: int = DEFAULT_BLOCK_SIZE,
    log=None,
) -> SearchIndex:
    """Prepare the index, reporting progress and rewriting any failure.

    Args:
        config: What to prepare.
        tracker: Where progress goes. Without one this is ``build_or_load`` with
            two extra checks and no reporting at all.
        force_rebuild: Build even when the cache is valid.
        block_size: Suffix-array entries per summary row.
        log: The plain text logger, unchanged and independent of ``tracker``.

    Returns:
        A complete index. Nothing partial is ever returned: either the whole
        thing is built and validated, or this raises.

    Raises:
        PreparationFailure: for every failure, carrying nothing unsafe.
    """
    sink: ProgressSink = _GuardedSink(tracker) if tracker is not None else NULL_SINK

    if tracker is not None:
        # Announced before the first phase, so an interface can say "checking
        # the cache" or "first build" from the very first frame instead of
        # showing an unknown route for the first few milliseconds.
        tracker.start(planned_mode(config, force_rebuild))

    try:
        sink.begin(
            BuildPhase.LOADING_CONFIGURATION,
            detail="Reading settings and locating the corpus.",
            determinate=False,
        )

        # Checked before the corpus is read rather than part-way through
        # indexing it, so a broken installation costs milliseconds instead of
        # most of a build.
        sink.begin(
            BuildPhase.VERIFYING_SUFFIX_BUILDER,
            detail="Checking the suffix array builder against a known answer.",
            determinate=False,
        )
        verify_builder()

        index = build_or_load(
            config,
            force_rebuild=force_rebuild,
            block_size=block_size,
            log=log,
            sink=sink if tracker is not None else None,
        )
    except BaseException as error:
        code, message, hint = describe_failure(error)
        phase = (
            tracker.snapshot().phase
            if tracker is not None
            else BuildPhase.LOADING_CONFIGURATION
        )
        # The type is logged, never the message: an exception here can quote a
        # path, and the log is the only place that is acceptable.
        logger.error("preparation failed during %s: %s", phase.value, type(error).__name__)
        if tracker is not None:
            tracker.fail(code, message, hint=hint)
        raise PreparationFailure(code, message, hint, phase) from error

    if tracker is not None:
        tracker.finish(index.stats())
    return index
