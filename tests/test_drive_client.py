"""Tests for the Google API boundary.

Nothing here reaches the network: the client takes the function that opens a
URL, so a test supplies one and inspects exactly what would have been sent. That
is what lets the security properties be asserted rather than asserted-about, in
particular that the access token appears in one header and nowhere else.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from autocomplete.drive.client import (
    DRIVE_API_ROOT,
    HttpDriveClient,
)
from autocomplete.drive.errors import (
    DocumentNotFoundError,
    DocumentTooLargeError,
    DriveAuthError,
    DriveQuotaError,
    DriveTransportError,
)

TOKEN = "ya29.a0-secret-access-token"

METADATA = {
    "id": "file-1",
    "name": "notes.txt",
    "mimeType": "text/plain",
    "size": "42",
    "modifiedTime": "2026-09-01T09:00:00.000Z",
    "headRevisionId": "rev-7",
    "capabilities": {"canDownload": True},
}


class Recorder:
    """Stands in for ``urlopen``, keeping every request it was given."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeout = timeout
        outcome = self.responses.pop(0) if self.responses else _ok(b"")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def urls(self) -> list[str]:
        return [request.full_url for request in self.requests]

    @property
    def headers(self) -> dict:
        return dict(self.requests[-1].header_items())


class _Response(io.BytesIO):
    def __init__(self, data: bytes, headers: dict | None = None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _ok(data: bytes, headers: dict | None = None) -> _Response:
    return _Response(data, headers)


def _json(body: dict) -> _Response:
    return _ok(json.dumps(body).encode("utf-8"))


def _http_error(status: int, body: dict | None = None) -> urllib.error.HTTPError:
    payload = json.dumps(body or {}).encode("utf-8")
    return urllib.error.HTTPError(
        "https://www.googleapis.com/", status, "error", {}, io.BytesIO(payload)
    )


def _http_errors(count: int, status: int, body: dict | None = None) -> list:
    """A distinct error per attempt.

    Reusing one object would be a poor imitation of HTTP: reading its body
    consumes it, so the second attempt would see an empty one and be classified
    differently from the first.
    """
    return [_http_error(status, body) for _ in range(count)]


def client(opener, **kwargs) -> HttpDriveClient:
    return HttpDriveClient(
        TOKEN, opener=opener, sleep=lambda _seconds: None, **kwargs
    )


class TestAuthorizationHandling:
    def test_the_token_is_sent_as_a_bearer_header(self):
        recorder = Recorder(_json(METADATA))
        client(recorder).metadata("file-1")
        assert recorder.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_the_token_never_appears_in_a_url(self):
        recorder = Recorder(_json(METADATA), _ok(b"hello"), _ok(b"hello"))
        drive = client(recorder)
        drive.metadata("file-1")
        drive.download("file-1", max_bytes=100)
        drive.export_text("file-1", max_bytes=100)
        for url in recorder.urls:
            assert TOKEN not in url
            assert "access_token" not in url

    def test_an_empty_token_is_refused_before_any_request(self):
        recorder = Recorder()
        with pytest.raises(DriveAuthError, match="No Google authorization"):
            HttpDriveClient("", opener=recorder)
        assert recorder.requests == []

    def test_a_rejected_authorization_is_reported_as_such(self):
        with pytest.raises(DriveAuthError, match="may have expired"):
            client(Recorder(_http_error(401))).metadata("file-1")

    def test_a_refusal_names_the_picker_rule_rather_than_the_status(self):
        with pytest.raises(DriveAuthError, match="chosen in the picker"):
            client(Recorder(_http_error(403))).metadata("file-1")

    def test_an_authorization_failure_is_not_retried(self):
        recorder = Recorder(*_http_errors(3, 401))
        with pytest.raises(DriveAuthError):
            client(recorder, retries=2).metadata("file-1")
        assert len(recorder.requests) == 1


class TestErrorTranslation:
    def test_a_missing_file_explains_the_scope(self):
        with pytest.raises(DocumentNotFoundError, match="drive.file scope"):
            client(Recorder(_http_error(404))).metadata("file-1")

    def test_a_rate_limit_status_is_a_quota_error(self):
        recorder = Recorder(*_http_errors(3, 429))
        with pytest.raises(DriveQuotaError, match="rate limiting"):
            client(recorder, retries=2).metadata("file-1")

    def test_a_rate_limit_reason_inside_a_403_is_a_quota_error(self):
        body = {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
        recorder = Recorder(*_http_errors(3, 403, body))
        with pytest.raises(DriveQuotaError, match="rate limiting"):
            client(recorder, retries=2).metadata("file-1")

    def test_a_network_failure_is_reported_without_the_exception(self):
        recorder = Recorder(*[urllib.error.URLError("Name or service not known")] * 3)
        with pytest.raises(DriveTransportError, match="could not be reached"):
            client(recorder, retries=2).metadata("file-1")

    def test_a_timeout_is_reported_as_one(self):
        recorder = Recorder(*[TimeoutError()] * 3)
        with pytest.raises(DriveTransportError, match="did not answer in time"):
            client(recorder, retries=2).metadata("file-1")

    def test_an_unexpected_status_does_not_leak_a_body(self):
        body = {"error": {"message": "secret internal detail"}}
        with pytest.raises(DriveTransportError) as raised:
            client(Recorder(_http_error(418, body))).metadata("file-1")
        assert "secret internal detail" not in str(raised.value)

    def test_a_body_that_is_not_file_information_is_refused(self):
        with pytest.raises(DriveTransportError, match="not file information"):
            client(Recorder(_ok(b"<html>sign in</html>"))).metadata("file-1")

    def test_file_information_without_a_type_is_refused(self):
        with pytest.raises(DriveTransportError, match="without a type"):
            client(Recorder(_json({"id": "file-1"}))).metadata("file-1")


class TestRetries:
    def test_a_temporary_failure_is_tried_again_and_can_succeed(self):
        recorder = Recorder(_http_error(503), _json(METADATA))
        assert client(recorder, retries=2).metadata("file-1").name == "notes.txt"
        assert len(recorder.requests) == 2

    def test_a_rate_limit_that_clears_is_tried_again_and_can_succeed(self):
        body = {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
        recorder = Recorder(_http_error(403, body), _json(METADATA))
        assert client(recorder, retries=2).metadata("file-1").name == "notes.txt"

    def test_retries_are_bounded(self):
        recorder = Recorder(*_http_errors(10, 503))
        with pytest.raises(DriveTransportError):
            client(recorder, retries=2).metadata("file-1")
        assert len(recorder.requests) == 3

    def test_retries_can_be_switched_off(self):
        recorder = Recorder(*_http_errors(5, 503))
        with pytest.raises(DriveTransportError):
            client(recorder, retries=0).metadata("file-1")
        assert len(recorder.requests) == 1


class TestRequestShape:
    def test_metadata_asks_for_a_narrow_field_list(self):
        recorder = Recorder(_json(METADATA))
        client(recorder).metadata("file-1")
        url = recorder.urls[0]
        assert url.startswith(f"{DRIVE_API_ROOT}/files/file-1?")
        assert "fields=" in url
        # Nothing about the file beyond what the import needs.
        for absent in ("permissions", "owners", "parents", "webViewLink"):
            assert absent not in url

    def test_a_download_uses_the_documented_alt_media_form(self):
        recorder = Recorder(_ok(b"data"))
        client(recorder).download("file-1", max_bytes=100)
        assert "alt=media" in recorder.urls[0]

    def test_an_export_asks_for_plain_text(self):
        recorder = Recorder(_ok(b"data"))
        client(recorder).export_text("file-1", max_bytes=100)
        assert recorder.urls[0].startswith(f"{DRIVE_API_ROOT}/files/file-1/export?")
        assert "mimeType=text%2Fplain" in recorder.urls[0]

    def test_an_identifier_cannot_escape_the_path(self):
        recorder = Recorder(_json(METADATA))
        client(recorder).metadata("../../tokeninfo")
        assert "/files/..%2F..%2Ftokeninfo?" in recorder.urls[0]

    def test_the_configured_timeout_is_applied(self):
        recorder = Recorder(_json(METADATA))
        client(recorder, timeout=7.5).metadata("file-1")
        assert recorder.timeout == 7.5

    def test_no_call_can_list_the_drive(self):
        """There is no listing operation, which is what makes "only the files
        you picked" a property of the code and not a promise."""
        assert not hasattr(HttpDriveClient, "list")
        assert not any(
            "list" in name for name in dir(HttpDriveClient) if not name.startswith("_")
        )


class TestSizeLimits:
    def test_a_declared_size_over_the_limit_stops_the_transfer(self):
        recorder = Recorder(_ok(b"x" * 10, {"Content-Length": "99999"}))
        with pytest.raises(DocumentTooLargeError, match="over the"):
            client(recorder).download("file-1", max_bytes=100)

    def test_a_body_longer_than_the_limit_is_refused(self):
        recorder = Recorder(_ok(b"x" * 500))
        with pytest.raises(DocumentTooLargeError, match="over the 100 byte limit"):
            client(recorder).download("file-1", max_bytes=100)

    def test_a_body_at_the_limit_is_accepted(self):
        recorder = Recorder(_ok(b"x" * 100))
        assert client(recorder).download("file-1", max_bytes=100) == b"x" * 100

    def test_an_oversized_file_is_not_retried(self):
        recorder = Recorder(*[_ok(b"x" * 500)] * 3)
        with pytest.raises(DocumentTooLargeError):
            client(recorder, retries=2).download("file-1", max_bytes=100)
        assert len(recorder.requests) == 1


class TestMetadataParsing:
    def test_every_field_is_read(self):
        file = client(Recorder(_json(METADATA))).metadata("file-1")
        assert (file.file_id, file.name, file.mime_type) == (
            "file-1",
            "notes.txt",
            "text/plain",
        )
        assert (file.size, file.modified_time, file.revision_id) == (
            42,
            "2026-09-01T09:00:00.000Z",
            "rev-7",
        )
        assert file.can_download is True

    def test_a_google_doc_reports_no_size(self):
        body = {
            "id": "doc-1",
            "name": "Meeting Notes",
            "mimeType": "application/vnd.google-apps.document",
        }
        assert client(Recorder(_json(body))).metadata("doc-1").size is None

    def test_a_file_that_cannot_be_downloaded_says_so(self):
        body = {**METADATA, "capabilities": {"canDownload": False}}
        assert client(Recorder(_json(body))).metadata("file-1").can_download is False
