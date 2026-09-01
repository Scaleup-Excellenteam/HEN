"""The only code in the project that speaks to Google.

Everything above this module works with :class:`DriveFile` and the typed errors
in :mod:`autocomplete.drive.errors`, so the rest of the feature can be exercised
without a network, without credentials, and without a Google account: tests
supply a different :class:`DriveClient` and nothing else changes.

Why the server downloads, and not the browser
---------------------------------------------

The browser is where the user authorizes and picks files, so the access token
starts there. It could also download the files there and send the text up. It
does not, because then the *only* description of a file the server would ever
see is the one the browser sent, and "this is a 4 kB plain-text file" would be a
claim rather than a fact. Instead the browser sends the file identifiers it was
given and the token, and this module asks Drive itself what those files are. The
MIME type, the size and the name that decide whether an import is allowed then
come from Drive, which is the only party in a position to know them.

What is never done with the token
---------------------------------

It is held in memory for the duration of one job and dropped. It is never
written to disk, never placed in a URL or a query string where a proxy or a
server log would capture it, never included in an exception message, and never
logged. :meth:`HttpDriveClient._request` is the only place it is read, and it
puts it in a request header and nowhere else.

Scope
-----

Everything here works under ``drive.file``, which Google documents as
"Create new Drive files, or modify existing files, that you open with an app or
that the user shares with an app while using the Google Picker API". It grants
access to the files the user picked and to nothing else: there is no listing
call in this module, and adding one would not work under that scope anyway.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .errors import (
    DocumentNotFoundError,
    DocumentTooLargeError,
    DriveAuthError,
    DriveQuotaError,
    DriveTransportError,
)

__all__ = [
    "DRIVE_API_ROOT",
    "METADATA_FIELDS",
    "DriveClient",
    "DriveFile",
    "HttpDriveClient",
]

DRIVE_API_ROOT = "https://www.googleapis.com/drive/v3"

#: Exactly what the feature needs to decide whether a file may be imported and
#: to recognise it again later. Asking for a narrow field list rather than the
#: default keeps everything else about the user's file out of this process.
METADATA_FIELDS = "id,name,mimeType,size,modifiedTime,headRevisionId,capabilities/canDownload"

#: HTTP statuses worth trying again: Drive was momentarily unable to answer.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})

_BACKOFF_SECONDS = 0.5

_RATE_LIMITED = (
    "Google Drive is rate limiting this project. Wait a moment and try again."
)


@dataclass(frozen=True)
class DriveFile:
    """What Drive says a file is.

    Attributes:
        file_id: Drive's own identifier.
        name: The file's name in Drive.
        mime_type: Drive's MIME type. A native Google Doc has
            ``application/vnd.google-apps.document`` and no bytes of its own.
        size: Size in bytes, where Drive reports one. Google Workspace files
            have no stored size, so this is ``None`` for them.
        modified_time: RFC 3339 timestamp of the last change, if given.
        revision_id: Head revision identifier, if given. Together with
            ``modified_time`` this is what tells a re-import of an unchanged
            file from a re-import of an edited one.
        can_download: Whether the user's permissions allow reading the content.
    """

    file_id: str
    name: str
    mime_type: str
    size: int | None = None
    modified_time: str | None = None
    revision_id: str | None = None
    can_download: bool = True


class DriveClient(Protocol):
    """What the rest of the feature needs from Drive.

    Kept to three operations, none of which can enumerate anything: a file is
    only ever addressed by an identifier the user chose in the Picker.
    """

    def metadata(self, file_id: str) -> DriveFile:
        """What Drive says the file is."""

    def download(self, file_id: str, *, max_bytes: int) -> bytes:
        """The bytes of a stored file, refusing more than ``max_bytes``."""

    def export_text(self, file_id: str, *, max_bytes: int) -> bytes:
        """A Google Workspace document exported as plain text."""


class HttpDriveClient:
    """A :class:`DriveClient` over the Drive v3 REST API.

    Uses the standard library rather than an HTTP package, so enabling this
    feature adds no dependency and the command line stays exactly as light as it
    was. Calls block, which is why they are only ever made from a worker thread
    and never from the HTTP event loop.
    """

    def __init__(
        self,
        access_token: str,
        *,
        timeout: float = 30.0,
        retries: int = 2,
        opener: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not access_token:
            raise DriveAuthError(
                "No Google authorization was supplied. Connect to Google Drive "
                "and try again."
            )
        # Private, and read in exactly one place. Nothing else in the class may
        # touch it, which is what keeps it out of messages and logs.
        self._access_token = access_token
        self._timeout = timeout
        self._retries = max(0, retries)
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    def metadata(self, file_id: str) -> DriveFile:
        payload = self._request(
            f"{DRIVE_API_ROOT}/files/{urllib.parse.quote(file_id, safe='')}",
            {"fields": METADATA_FIELDS, "supportsAllDrives": "true"},
            # Metadata is small by construction; the cap is only a guard against
            # an answer that is not what we asked for.
            max_bytes=1 << 20,
            what="metadata",
        )
        return _parse_metadata(payload, file_id)

    def download(self, file_id: str, *, max_bytes: int) -> bytes:
        return self._request(
            f"{DRIVE_API_ROOT}/files/{urllib.parse.quote(file_id, safe='')}",
            {"alt": "media", "supportsAllDrives": "true"},
            max_bytes=max_bytes,
            what="content",
        )

    def export_text(self, file_id: str, *, max_bytes: int) -> bytes:
        return self._request(
            f"{DRIVE_API_ROOT}/files/{urllib.parse.quote(file_id, safe='')}/export",
            {"mimeType": "text/plain"},
            max_bytes=max_bytes,
            what="export",
        )

    # ------------------------------------------------------------ transport ---

    def _request(
        self, url: str, parameters: dict[str, str], *, max_bytes: int, what: str
    ) -> bytes:
        full_url = f"{url}?{urllib.parse.urlencode(parameters)}"
        attempt = 0
        while True:
            try:
                return self._once(full_url, max_bytes=max_bytes)
            except (DriveQuotaError, DriveTransportError) as failure:
                if attempt >= self._retries or not failure.retryable:
                    raise
                attempt += 1
                self._sleep(_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            except DocumentTooLargeError:
                raise
            except (DriveAuthError, DocumentNotFoundError):
                raise
            except Exception as exc:
                # Whatever the transport raised, the caller learns what failed
                # and not what the exception happened to contain.
                raise DriveTransportError(
                    f"Google Drive could not be reached while fetching {what}."
                ) from exc

    def _once(self, url: str, *, max_bytes: int) -> bytes:
        request = urllib.request.Request(url, method="GET")
        # The one place the token is read. It goes in a header, never in the
        # URL, so it cannot be captured by a proxy log or a browser history.
        request.add_header("Authorization", f"Bearer {self._access_token}")
        request.add_header("Accept", "*/*")

        try:
            response = self._open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            raise _from_status(error.code, _reason(error)) from None
        except urllib.error.URLError as error:
            raise DriveTransportError(
                "Google Drive could not be reached. Check the network connection "
                "and try again.",
                retryable=True,
            ) from error
        except TimeoutError as error:
            raise DriveTransportError(
                "Google Drive did not answer in time. Try again.", retryable=True
            ) from error

        with response:
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise DocumentTooLargeError(
                    f"Google Drive reports this file as {int(declared):,} bytes, "
                    f"over the {max_bytes:,} byte limit."
                )
            # One byte past the limit is enough to know the limit was passed,
            # and stops a very large file being pulled into memory to find out.
            data = response.read(max_bytes + 1)

        if len(data) > max_bytes:
            raise DocumentTooLargeError(
                f"This file is over the {max_bytes:,} byte limit."
            )
        return data


def _from_status(status: int, reason: str) -> Exception:
    if status in (401,):
        return DriveAuthError(
            "Google rejected the authorization. It may have expired; connect to "
            "Google Drive again."
        )
    if status == 403:
        if any(
            marker in reason
            for marker in ("rateLimit", "userRateLimit", "quotaExceeded", "dailyLimit")
        ):
            return DriveQuotaError(_RATE_LIMITED, retryable=True)
        return DriveAuthError(
            "Google refused access to this file. Only files chosen in the picker "
            "can be read, and the Drive account must be allowed to download them."
        )
    if status == 404:
        return DocumentNotFoundError(
            "Google Drive did not return that file. Under the drive.file scope "
            "only files selected in the picker are reachable; select it again."
        )
    if status == 429:
        return DriveQuotaError(_RATE_LIMITED, retryable=True)
    if status in _RETRYABLE:
        return DriveTransportError(
            f"Google Drive replied with a temporary error ({status}). Try again.",
            retryable=True,
        )
    return DriveTransportError(f"Google Drive replied with an unexpected error ({status}).")


def _reason(error: urllib.error.HTTPError) -> str:
    """The machine-readable reason from a Drive error body, if it carries one.

    Only the ``reason`` fields are read. The body is never returned to a caller
    or logged, because a Drive error can quote the file's own name back.
    """
    try:
        body = json.loads(error.read().decode("utf-8", errors="replace"))
    except Exception:
        return ""
    errors = body.get("error", {}).get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return ""
    return " ".join(
        str(item.get("reason", "")) for item in errors if isinstance(item, dict)
    )


def _parse_metadata(payload: bytes, file_id: str) -> DriveFile:
    try:
        body = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise DriveTransportError(
            "Google Drive returned something that is not file information."
        ) from exc
    if not isinstance(body, dict) or not isinstance(body.get("mimeType"), str):
        raise DriveTransportError(
            "Google Drive returned file information without a type."
        )

    size = body.get("size")
    capabilities = body.get("capabilities")
    return DriveFile(
        file_id=str(body.get("id") or file_id),
        name=str(body.get("name") or ""),
        mime_type=body["mimeType"],
        size=int(size) if isinstance(size, (int, str)) and str(size).isdigit() else None,
        modified_time=_text_or_none(body.get("modifiedTime")),
        revision_id=_text_or_none(body.get("headRevisionId")),
        can_download=(
            capabilities.get("canDownload", True)
            if isinstance(capabilities, dict)
            else True
        ),
    )


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
