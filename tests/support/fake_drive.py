"""A stand-in for Google Drive, so the feature can be tested without one.

Everything above :mod:`autocomplete.drive.client` works through the
:class:`~autocomplete.drive.client.DriveClient` protocol, so substituting this
covers the whole feature: authorization, download, export, quota responses and
failures included. No test in this project needs a Google account, a credential
or a network connection, and this is what makes that true.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autocomplete.drive.client import DriveFile
from autocomplete.drive.errors import (
    DocumentNotFoundError,
    DriveAuthError,
    DriveError,
    DriveQuotaError,
    DriveTransportError,
)
from autocomplete.drive.settings import GOOGLE_DOC_MIME_TYPE, PLAIN_TEXT_MIME_TYPE

__all__ = ["FakeDrive", "FakeFile", "google_doc", "text_file"]


@dataclass
class FakeFile:
    """One file the fake Drive holds."""

    metadata: DriveFile
    content: bytes = b""
    #: Raised instead of answering, for testing how a failure is handled.
    fail_metadata: DriveError | None = None
    fail_content: DriveError | None = None


def text_file(
    file_id: str,
    name: str,
    content: bytes,
    *,
    modified_time: str = "2026-09-01T09:00:00.000Z",
    revision_id: str = "rev-1",
    can_download: bool = True,
    reported_size: int | None = None,
    **failures,
) -> FakeFile:
    """A stored plain-text file."""
    return FakeFile(
        metadata=DriveFile(
            file_id=file_id,
            name=name,
            mime_type=PLAIN_TEXT_MIME_TYPE,
            size=len(content) if reported_size is None else reported_size,
            modified_time=modified_time,
            revision_id=revision_id,
            can_download=can_download,
        ),
        content=content,
        **failures,
    )


def google_doc(
    file_id: str,
    name: str,
    exported: bytes,
    *,
    modified_time: str = "2026-09-01T09:00:00.000Z",
    revision_id: str = "rev-1",
    **failures,
) -> FakeFile:
    """A native Google Doc, with the plain text its export produces.

    Drive reports no size for one of these, which is why ``size`` is left unset:
    a test that assumes otherwise would not be testing the real shape.
    """
    return FakeFile(
        metadata=DriveFile(
            file_id=file_id,
            name=name,
            mime_type=GOOGLE_DOC_MIME_TYPE,
            size=None,
            modified_time=modified_time,
            revision_id=revision_id,
        ),
        content=exported,
        **failures,
    )


@dataclass
class FakeDrive:
    """A Drive holding a fixed set of files, recording what was asked of it.

    Attributes:
        files: What the account holds, by Drive file ID.
        token: The authorization the caller must present, if any is required.
        calls: Every operation performed, in order, for asserting that only the
            files the user chose were ever touched.
    """

    files: dict[str, FakeFile] = field(default_factory=dict)
    token: str | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def add(self, file: FakeFile) -> "FakeDrive":
        self.files[file.metadata.file_id] = file
        return self

    def with_token(self, token: str) -> "FakeDrive":
        self.token = token
        return self

    def client(self, access_token: str = "test-token"):
        """A client for this Drive, as the feature would be handed one."""
        return _FakeClient(self, access_token)

    @property
    def touched(self) -> set[str]:
        """Every file identifier this Drive was asked about."""
        return {file_id for _, file_id in self.calls}


class _FakeClient:
    def __init__(self, drive: FakeDrive, access_token: str) -> None:
        if not access_token:
            raise DriveAuthError("No Google authorization was supplied.")
        self._drive = drive
        self._token = access_token

    def metadata(self, file_id: str) -> DriveFile:
        return self._fetch("metadata", file_id).metadata

    def download(self, file_id: str, *, max_bytes: int) -> bytes:
        return self._content("download", file_id, max_bytes)

    def export_text(self, file_id: str, *, max_bytes: int) -> bytes:
        return self._content("export", file_id, max_bytes)

    def _fetch(self, operation: str, file_id: str) -> FakeFile:
        self._drive.calls.append((operation, file_id))
        if self._drive.token is not None and self._token != self._drive.token:
            raise DriveAuthError("Google rejected the authorization.")
        try:
            file = self._drive.files[file_id]
        except KeyError:
            raise DocumentNotFoundError(
                "Google Drive did not return that file."
            ) from None
        failure = file.fail_metadata if operation == "metadata" else file.fail_content
        if failure is not None:
            raise failure
        return file

    def _content(self, operation: str, file_id: str, max_bytes: int) -> bytes:
        file = self._fetch(operation, file_id)
        # The real client stops one byte past the limit rather than reading a
        # huge file into memory; the caller checks the length either way.
        return file.content[: max_bytes + 1]


def rate_limited() -> DriveQuotaError:
    return DriveQuotaError(
        "Google Drive is rate limiting this project. Wait a moment and try again.",
        retryable=True,
    )


def unreachable() -> DriveTransportError:
    return DriveTransportError(
        "Google Drive could not be reached. Check the network connection and try again.",
        retryable=True,
    )
