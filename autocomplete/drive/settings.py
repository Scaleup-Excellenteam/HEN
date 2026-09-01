"""Configuration for the Google Drive import feature.

The feature is deployment-specific and optional, so its settings come from the
environment rather than from ``config.yaml``: the YAML file describes the corpus
and is committed, while a Google client identifier belongs to whoever is running
the server. ``.env.example`` documents every variable.

**Disabled is the default and the safe state.** With nothing set, the feature is
off, no Google identifier is needed, nothing contacts Google, and the rest of the
program behaves exactly as it does without this module. Turning it on without the
identifiers it needs is reported as "enabled but not configured" rather than
failing at start-up, so an incomplete deployment degrades to the disabled
behaviour instead of taking the search down with it.

Nothing here is a secret. Google's own guidance treats an OAuth *client ID* and a
browser *API key* as public configuration: both are visible to anyone who opens
the page, and both are protected by origin restrictions in the Cloud Console
rather than by being hidden. A client *secret* or a service-account key would be
a secret, and this feature uses neither.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config

__all__ = [
    "DRIVE_SOURCE_PREFIX",
    "GOOGLE_DOC_MIME_TYPE",
    "PLAIN_TEXT_MIME_TYPE",
    "SUPPORTED_MIME_TYPES",
    "DriveSettings",
    "DriveSettingsError",
    "load_settings",
]

#: Namespace every imported sentence's ``source_text`` sits under, so a result
#: from Drive is distinguishable from a corpus one at a glance and the two can
#: never be confused for the same file.
DRIVE_SOURCE_PREFIX = "Google Drive"

PLAIN_TEXT_MIME_TYPE = "text/plain"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"

#: The two types this feature imports. Anything else is refused before it is
#: downloaded. PDFs, images, spreadsheets and presentations are deliberately out
#: of scope; they need parsing or extraction this feature does not do.
SUPPORTED_MIME_TYPES: tuple[str, ...] = (PLAIN_TEXT_MIME_TYPE, GOOGLE_DOC_MIME_TYPE)

_PREFIX = "HEN_DRIVE_"

#: Drive caps an export of a Google Doc at 10 MB, so a larger per-file limit
#: could not be honoured for half the supported types anyway.
_DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_DEFAULT_MAX_FILES = 10


class DriveSettingsError(ValueError):
    """Raised when an environment variable holds a value that cannot be used."""


@dataclass(frozen=True)
class DriveSettings:
    """Validated settings for the Drive import feature.

    Attributes:
        enabled: Whether the feature is offered at all.
        client_id: OAuth 2.0 client ID for the browser. Public configuration.
        api_key: Browser API key the Picker needs. Public configuration.
        app_id: The Cloud project *number*, which the Picker needs in order to
            grant this application per-file access under ``drive.file``.
        data_dir: Where imported text and the overlay index are kept. Never
            inside the source tree, and ignored by git.
        max_files: Most documents one import request may carry.
        max_file_bytes: Largest single document, before decoding.
        max_total_bytes: Largest total imported corpus, across all documents.
        supported_mime_types: Drive MIME types this feature accepts.
        timeout_seconds: How long one Drive request may take.
        retries: How many times a retryable Drive failure is tried again.
    """

    enabled: bool = False
    client_id: str = ""
    api_key: str = ""
    app_id: str = ""
    data_dir: Path = Path(".drive-data")
    max_files: int = _DEFAULT_MAX_FILES
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES
    supported_mime_types: tuple[str, ...] = SUPPORTED_MIME_TYPES
    timeout_seconds: float = 30.0
    retries: int = 2
    #: Set when ``enabled`` is true but an identifier is missing; the feature
    #: then behaves as disabled and says why.
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def configured(self) -> bool:
        """Whether the feature can actually be used."""
        return self.enabled and not self.missing

    def describe_missing(self) -> str:
        return (
            "Google Drive import is switched on but "
            f"{', '.join(self.missing)} {'is' if len(self.missing) == 1 else 'are'} "
            "not set. See .env.example."
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        base_dir: Path | None = None,
    ) -> "DriveSettings":
        """Read the settings, filling in defaults for everything unset.

        ``base_dir`` anchors a relative ``data_dir``, exactly as ``config.yaml``
        anchors its own paths, so the location does not depend on the working
        directory the server happened to be started from.
        """
        source = os.environ if environ is None else environ

        enabled = _boolean(source, "ENABLED", default=False)
        client_id = _text(source, "CLIENT_ID")
        api_key = _text(source, "API_KEY")
        app_id = _text(source, "APP_ID")

        missing = tuple(
            name
            for name, value in (
                (f"{_PREFIX}CLIENT_ID", client_id),
                (f"{_PREFIX}API_KEY", api_key),
                (f"{_PREFIX}APP_ID", app_id),
            )
            if not value
        )

        data_dir = Path(_text(source, "DATA_DIR") or ".drive-data").expanduser()
        if not data_dir.is_absolute() and base_dir is not None:
            data_dir = base_dir / data_dir
        data_dir = Path(os.path.normpath(data_dir))

        max_file_bytes = _positive_int(source, "MAX_FILE_BYTES", _DEFAULT_MAX_FILE_BYTES)
        max_total_bytes = _positive_int(
            source, "MAX_TOTAL_BYTES", _DEFAULT_MAX_TOTAL_BYTES
        )
        if max_total_bytes < max_file_bytes:
            raise DriveSettingsError(
                f"{_PREFIX}MAX_TOTAL_BYTES ({max_total_bytes}) is below "
                f"{_PREFIX}MAX_FILE_BYTES ({max_file_bytes}); no file could be imported"
            )

        return cls(
            enabled=enabled,
            client_id=client_id,
            api_key=api_key,
            app_id=app_id,
            data_dir=data_dir,
            max_files=_positive_int(source, "MAX_FILES", _DEFAULT_MAX_FILES),
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            timeout_seconds=_positive_float(source, "HTTP_TIMEOUT_SECONDS", 30.0),
            retries=_non_negative_int(source, "HTTP_RETRIES", 2),
            missing=missing if enabled else (),
        )


def load_settings(
    config: Config | None = None,
    environ: Mapping[str, str] | None = None,
) -> DriveSettings:
    """Read the feature's settings, anchoring its data directory sensibly.

    A relative ``HEN_DRIVE_DATA_DIR`` is resolved against the directory holding
    the corpus cache, which is the project's own generated-data location and is
    already ignored by git.
    """
    base_dir = config.cache_dir.parent if config is not None else None
    return DriveSettings.from_environment(environ, base_dir=base_dir)


def _text(source: Mapping[str, str], name: str) -> str:
    return str(source.get(f"{_PREFIX}{name}", "")).strip()


def _boolean(source: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = _text(source, name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise DriveSettingsError(
        f"{_PREFIX}{name} must be true or false, got {raw!r}"
    )


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    value = _non_negative_int(source, name, default)
    if value < 1:
        raise DriveSettingsError(f"{_PREFIX}{name} must be at least 1, got {value}")
    return value


def _non_negative_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = _text(source, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise DriveSettingsError(
            f"{_PREFIX}{name} must be a whole number, got {raw!r}"
        ) from exc
    if value < 0:
        raise DriveSettingsError(f"{_PREFIX}{name} must not be negative, got {value}")
    return value


def _positive_float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = _text(source, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise DriveSettingsError(
            f"{_PREFIX}{name} must be a number, got {raw!r}"
        ) from exc
    if value <= 0:
        raise DriveSettingsError(f"{_PREFIX}{name} must be positive, got {value}")
    return value
