"""The failures the Drive import feature reports.

Every error carries a ``code`` the interface can branch on and a message written
for the person who has to fix it. Nothing here ever carries an access token, an
authorization header, document text, or a raw exception from the transport: the
whole point of a typed error is that the layer above can be specific without the
layer below leaking what it saw.
"""

from __future__ import annotations

__all__ = [
    "DriveError",
    "DriveAuthError",
    "DriveDisabledError",
    "DriveNotConfiguredError",
    "DriveQuotaError",
    "DriveTransportError",
    "DocumentTooLargeError",
    "UnsupportedDocumentError",
    "InvalidEncodingError",
    "ImportLimitError",
    "JobInProgressError",
    "DocumentNotFoundError",
    "StoreCorruptError",
]


class DriveError(RuntimeError):
    """Base class for every failure the feature reports.

    Attributes:
        code: A stable identifier the interface matches on, so the wording of a
            message can change without breaking the interface.
        message: What went wrong and what to do about it, safe to display.
        retryable: Whether repeating the same request could succeed. Set by
            whoever raises it, since only they know: a 503 is worth another
            attempt, a rejected file type never will be.
    """

    code = "drive_error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class DriveDisabledError(DriveError):
    """The feature is switched off."""

    code = "disabled"


class DriveNotConfiguredError(DriveError):
    """The feature is on but the Google settings it needs are missing."""

    code = "not_configured"


class DriveAuthError(DriveError):
    """Google refused the authorization, or it has expired."""

    code = "auth_failed"


class DriveQuotaError(DriveError):
    """Google asked us to slow down, or the project is over its quota."""

    code = "quota"


class DriveTransportError(DriveError):
    """Drive could not be reached, or answered in a way we cannot use."""

    code = "transport"


class UnsupportedDocumentError(DriveError):
    """The selected file is not a type this feature imports."""

    code = "unsupported"


class DocumentTooLargeError(DriveError):
    """The file is over the configured size limit."""

    code = "too_large"


class InvalidEncodingError(DriveError):
    """The bytes are not valid UTF-8 text."""

    code = "invalid_encoding"


class ImportLimitError(DriveError):
    """The request would exceed a configured count or total-size limit."""

    code = "limit"


class JobInProgressError(DriveError):
    """Another import or removal is already running."""

    code = "busy"


class DocumentNotFoundError(DriveError):
    """No imported document has that identifier."""

    code = "not_found"


class StoreCorruptError(DriveError):
    """The imported-corpus manifest cannot be read or does not make sense."""

    code = "store_corrupt"
