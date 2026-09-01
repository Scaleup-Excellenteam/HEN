"""HTTP endpoints for importing documents from Google Drive.

Kept in its own router so that switching the feature off is a matter of not
mounting it, and so that nothing about the existing ``/api/health`` and
``/api/complete`` contract changes. Those two behave exactly as they did before
this feature existed; the only difference is that a completion may now come from
an imported document, and it says so in the ``source_text`` it already carried.

The access token
----------------

An import needs the user's Google authorization, and it arrives in the
``X-Drive-Access-Token`` header. A header rather than the body or the query
string, for three reasons: a query string is written to access logs and browser
history, a body field would be echoed back inside a validation error when some
*other* field of the same request is wrong, and a header is what the value is
for. It is passed to the job that needs it and kept nowhere else.

Errors
------

Every failure the feature raises carries a code and a sentence written for the
person reading it. Those are what is returned. An unexpected exception is not:
it becomes a 500 with a fixed message, because an internal message can quote a
path or a document.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel, Field

from ..drive.errors import (
    DocumentNotFoundError,
    DriveAuthError,
    DriveDisabledError,
    DriveError,
    DriveNotConfiguredError,
    DriveQuotaError,
    ImportLimitError,
    JobInProgressError,
    StoreCorruptError,
    UnsupportedDocumentError,
)
from ..drive.jobs import MAX_FILE_ID_LENGTH, DriveService

__all__ = ["MAX_SELECTED_FILES", "create_drive_router"]

#: An upper bound on the request itself, before the configured per-import limit
#: is consulted. Refusing a thousand-element list is the request layer's job;
#: refusing eleven when ten are allowed is the feature's.
MAX_SELECTED_FILES = 100

#: Which HTTP status each failure becomes. Anything absent is a 500.
_STATUS = {
    DriveDisabledError: 404,
    DriveNotConfiguredError: 503,
    DriveAuthError: 401,
    DriveQuotaError: 429,
    UnsupportedDocumentError: 415,
    ImportLimitError: 400,
    JobInProgressError: 409,
    DocumentNotFoundError: 404,
    StoreCorruptError: 500,
}


class ImportRequest(BaseModel):
    """The files the user chose in the Google Picker."""

    file_ids: list[
        Annotated[str, Field(min_length=1, max_length=MAX_FILE_ID_LENGTH)]
    ] = Field(
        min_length=1,
        max_length=MAX_SELECTED_FILES,
        description="Drive file identifiers, exactly as the picker returned them.",
    )


class Limits(BaseModel):
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    supported_mime_types: list[str]


class JobProgress(BaseModel):
    files_selected: int
    files_downloaded: int
    files_reused: int
    bytes_downloaded: int
    lines_read: int = Field(
        description="Lines fetched so far. Counted, not estimated."
    )
    sentences_indexed: int = Field(
        description="Searchable sentences in the finished index; zero until it is built."
    )
    detail: str


class JobError(BaseModel):
    code: str
    message: str
    retryable: bool


class JobStatus(BaseModel):
    id: str
    kind: str
    state: str = Field(
        description="pending, downloading, building, adopting, complete or failed."
    )
    progress: JobProgress
    error: JobError | None = None
    started_at: str
    finished_at: str | None = None
    needs_authorization: bool


class DriveStatus(BaseModel):
    """Everything the interface needs to render the feature."""

    enabled: bool
    configured: bool
    state: str = Field(
        description=(
            "disabled, ready, downloading, building, adopting or failed. The "
            "interface adds 'disconnected' when the browser holds no "
            "authorization, which the server cannot know."
        )
    )
    detail: str
    client_id: str = Field(
        description="OAuth client ID for the browser. Public configuration, empty "
        "unless the feature is fully configured."
    )
    api_key: str = Field(description="Browser API key for the picker. Public.")
    app_id: str = Field(description="Cloud project number the picker needs. Public.")
    scope: str
    source_prefix: str = Field(
        description="The source_text namespace imported sentences appear under."
    )
    limits: Limits
    documents: int
    sentences: int
    total_bytes: int
    job: JobStatus | None = None
    load_error: str | None = None


class ImportedDocumentModel(BaseModel):
    """One imported document, as the interface lists it.

    The Drive file identifier is deliberately absent. The interface never needs
    it, and it is the one field here that identifies something in the user's
    own Drive.
    """

    id: str
    name: str
    mime_type: str
    source_text: str
    imported_at: str
    modified_time: str | None = None
    bytes: int
    sentences: int
    status: str


class DocumentList(BaseModel):
    count: int
    total_bytes: int
    documents: list[ImportedDocumentModel]


def _drive(request: Request) -> DriveService:
    return request.app.state.drive


#: These live at module scope on purpose. ``from __future__ import annotations``
#: makes every annotation a string, resolved against this module's globals, so an
#: alias defined inside the factory below would not be found and the parameter
#: would be read as a query string instead of a dependency.
Service = Annotated[DriveService, Depends(_drive)]

Token = Annotated[
    str | None,
    Header(
        alias="X-Drive-Access-Token",
        max_length=4096,
        description=(
            "The Google access token from the browser. Used for this request "
            "only; never stored and never logged."
        ),
    ),
]

#: Both identifiers this API accepts in a path are lowercase hex of bounded
#: length: a document identifier is a truncated hash and a job identifier is a
#: UUID's hex. Nothing that reaches the filesystem is ever taken from a URL, and
#: this pattern is what makes that checkable from the schema alone.
_HEX_ID = Path(min_length=1, max_length=64, pattern=r"^[0-9a-f]+$")
DocumentId = Annotated[str, _HEX_ID]
JobId = Annotated[str, _HEX_ID]


def create_drive_router() -> APIRouter:
    """Build the router over the Drive service held on the application."""
    router = APIRouter(prefix="/api/drive", tags=["drive"])

    @router.get("/status", response_model=DriveStatus)
    def status(service: Service) -> DriveStatus:
        """Whether the feature is available, what it is doing, and what it holds."""
        return DriveStatus(**service.status())

    @router.get("/documents", response_model=DocumentList)
    def documents(service: Service) -> DocumentList:
        """The documents that have been imported."""
        _require_available(service)
        held = service.documents
        return DocumentList(
            count=len(held),
            total_bytes=sum(document.bytes for document in held),
            documents=[_document(document) for document in held],
        )

    @router.post("/imports", response_model=JobStatus, status_code=202)
    def start_import(
        body: ImportRequest, service: Service, token: Token = None
    ) -> JobStatus:
        """Import the documents the user selected, and report the job started."""
        if not token:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "auth_failed",
                    "message": (
                        "Connect to Google Drive before importing. No "
                        "authorization was supplied."
                    ),
                },
            )
        with _translated():
            return JobStatus(**service.start_import(body.file_ids, token).to_json())

    @router.get("/imports/{job_id}", response_model=JobStatus)
    def job_status(service: Service, job_id: JobId) -> JobStatus:
        """How the import or removal with this identifier is going."""
        with _translated():
            return JobStatus(**service.job(job_id).to_json())

    @router.delete("/documents/{document_id}", response_model=JobStatus, status_code=202)
    def remove(service: Service, document_id: DocumentId) -> JobStatus:
        """Remove one imported document and rebuild the imported index without it."""
        with _translated():
            return JobStatus(**service.start_removal(document_id).to_json())

    @router.post("/retry", response_model=JobStatus, status_code=202)
    def retry(service: Service, token: Token = None) -> JobStatus:
        """Run the last failed import or removal again."""
        with _translated():
            return JobStatus(**service.retry(token).to_json())

    return router


def _document(document) -> ImportedDocumentModel:
    return ImportedDocumentModel(
        id=document.id,
        name=document.name,
        mime_type=document.mime_type,
        source_text=document.source_text,
        imported_at=document.imported_at,
        modified_time=document.modified_time,
        bytes=document.bytes,
        sentences=document.sentences,
        status=document.status,
    )


def _require_available(service: DriveService) -> None:
    with _translated():
        if not service.settings.enabled:
            raise DriveDisabledError(
                "Google Drive import is switched off on this server."
            )
        if not service.settings.configured:
            raise DriveNotConfiguredError(service.settings.describe_missing())


class _translated:
    """Turn a feature error into an HTTP answer the interface can act on."""

    def __enter__(self) -> "_translated":
        return self

    def __exit__(self, kind, value, traceback) -> bool:
        if value is None or not isinstance(value, DriveError):
            return False
        raise HTTPException(
            status_code=_STATUS.get(type(value), 500),
            detail={
                "code": value.code,
                "message": value.message,
                "retryable": value.retryable,
            },
        ) from None
