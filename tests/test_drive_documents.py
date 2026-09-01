"""Tests for turning a Drive file into corpus text.

These use the fake Drive rather than the HTTP client, so what is under test is
the validation and the decoding rather than the transport.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autocomplete.drive.client import DriveFile
from autocomplete.drive.documents import (
    check_supported,
    decode_text,
    fetch_document,
    normalize_line_endings,
)
from autocomplete.drive.errors import (
    DocumentTooLargeError,
    InvalidEncodingError,
    UnsupportedDocumentError,
)
from autocomplete.drive.settings import DriveSettings
from tests.support.fake_drive import FakeDrive, google_doc, text_file

SETTINGS = DriveSettings(
    enabled=True,
    client_id="c",
    api_key="k",
    app_id="1",
    max_file_bytes=1024,
    max_total_bytes=4096,
)
WHEN = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def fetch(file, settings=SETTINGS, source_text="Google Drive/notes.txt"):
    drive = FakeDrive().add(file)
    return fetch_document(
        drive.client(),
        file.metadata,
        settings,
        source_text=source_text,
        now=WHEN,
    )


def metadata(**overrides) -> DriveFile:
    base = {
        "file_id": "f1",
        "name": "notes.txt",
        "mime_type": "text/plain",
        "size": 10,
    }
    return DriveFile(**{**base, **overrides})


class TestSupportedTypes:
    def test_a_plain_text_file_is_accepted(self):
        check_supported(metadata(), SETTINGS)

    def test_a_google_doc_is_accepted(self):
        check_supported(
            metadata(
                name="Meeting Notes",
                mime_type="application/vnd.google-apps.document",
                size=None,
            ),
            SETTINGS,
        )

    @pytest.mark.parametrize(
        "mime_type",
        [
            "application/pdf",
            "image/png",
            "application/vnd.google-apps.spreadsheet",
            "application/vnd.google-apps.presentation",
            "application/vnd.google-apps.folder",
            "application/octet-stream",
            "text/csv",
            "application/zip",
        ],
    )
    def test_everything_else_is_refused_by_name(self, mime_type):
        with pytest.raises(UnsupportedDocumentError, match="plain text files and Google Docs"):
            check_supported(metadata(mime_type=mime_type, name="thing"), SETTINGS)

    def test_plain_text_must_also_be_named_txt(self):
        with pytest.raises(UnsupportedDocumentError, match="not named .txt"):
            check_supported(metadata(name="data.csv"), SETTINGS)

    def test_the_extension_check_ignores_case(self):
        check_supported(metadata(name="NOTES.TXT"), SETTINGS)

    def test_a_google_doc_needs_no_extension(self):
        check_supported(
            metadata(
                name="Untitled document",
                mime_type="application/vnd.google-apps.document",
                size=None,
            ),
            SETTINGS,
        )

    def test_a_file_the_account_cannot_download_is_refused(self):
        with pytest.raises(UnsupportedDocumentError, match="cannot be downloaded"):
            check_supported(metadata(can_download=False), SETTINGS)

    def test_the_supported_list_is_configurable(self):
        only_text = DriveSettings(supported_mime_types=("text/plain",))
        with pytest.raises(UnsupportedDocumentError):
            check_supported(
                metadata(mime_type="application/vnd.google-apps.document"), only_text
            )


class TestSizeLimits:
    def test_a_declared_size_over_the_limit_is_refused_before_downloading(self):
        drive = FakeDrive().add(text_file("f1", "big.txt", b"x" * 5000))
        with pytest.raises(DocumentTooLargeError, match="over the 1,024 byte limit"):
            check_supported(drive.files["f1"].metadata, SETTINGS)
        assert drive.calls == []

    def test_a_file_at_the_limit_is_accepted(self):
        content = b"a\n" * 512
        assert len(content) == 1024
        assert fetch(text_file("f1", "ok.txt", content)).document.bytes == 1024

    def test_a_google_doc_whose_export_is_too_large_is_refused(self):
        """Drive reports no size for a Doc, so the limit is enforced on what
        the export actually produces."""
        with pytest.raises(DocumentTooLargeError, match="over the"):
            fetch(google_doc("d1", "Huge", b"x" * 5000))


class TestEncoding:
    def test_utf8_text_is_accepted(self):
        assert decode_text("héllo wörld\n".encode("utf-8"), metadata()).startswith(b"h")

    def test_a_byte_order_mark_is_removed(self):
        assert decode_text(b"\xef\xbb\xbfhello\n", metadata()) == b"hello\n"

    def test_latin1_bytes_are_refused_with_the_position(self):
        with pytest.raises(InvalidEncodingError, match="not valid UTF-8"):
            decode_text(b"caf\xe9\n", metadata())

    def test_utf16_is_refused(self):
        with pytest.raises(InvalidEncodingError):
            decode_text("hello".encode("utf-16"), metadata())

    def test_binary_content_is_refused_as_binary(self):
        with pytest.raises(InvalidEncodingError, match="binary data"):
            decode_text(b"\x89PNG\r\n\x1a\n\x00\x00", metadata())

    def test_an_empty_file_decodes_to_nothing(self):
        assert decode_text(b"", metadata()) == b""

    def test_the_message_names_the_file_without_letting_it_run_lines_together(self):
        awkward = metadata(name="line\none\ttwo")
        with pytest.raises(InvalidEncodingError) as raised:
            decode_text(b"\xff\xfe", awkward)
        assert "\n" not in str(raised.value)

    def test_a_refusal_does_not_quote_the_content(self):
        with pytest.raises(InvalidEncodingError) as raised:
            decode_text(b"secret\xff", metadata())
        assert "secret" not in str(raised.value)


class TestLineEndings:
    @pytest.mark.parametrize(
        "given,expected",
        [
            (b"a\nb\n", b"a\nb\n"),
            (b"a\r\nb\r\n", b"a\nb\n"),
            (b"a\rb\r", b"a\nb\n"),
            (b"a\r\nb\rc\n", b"a\nb\nc\n"),
            (b"no trailing newline", b"no trailing newline\n"),
            (b"", b""),
        ],
    )
    def test_every_ending_becomes_one_newline(self, given, expected):
        assert normalize_line_endings(given) == expected

    def test_a_carriage_return_file_does_not_collapse_into_one_sentence(self):
        """The reason this matters: leaving lone carriage returns in place would
        make the whole file a single record reporting line 1."""
        assert normalize_line_endings(b"one\rtwo\rthree").count(b"\n") == 3

    def test_blank_lines_are_preserved(self):
        assert normalize_line_endings(b"a\n\n\nb\n") == b"a\n\n\nb\n"


class TestFetching:
    def test_a_plain_text_file_is_downloaded(self):
        prepared = fetch(text_file("f1", "notes.txt", b"first line\nsecond line\n"))
        assert prepared.text == b"first line\nsecond line\n"
        assert prepared.document.mime_type == "text/plain"

    def test_a_google_doc_is_exported_rather_than_downloaded(self):
        drive = FakeDrive().add(google_doc("d1", "Meeting Notes", b"A paragraph.\n"))
        fetch_document(
            drive.client(),
            drive.files["d1"].metadata,
            SETTINGS,
            source_text="Google Drive/Meeting Notes.txt",
            now=WHEN,
        )
        assert [operation for operation, _ in drive.calls] == ["export"]

    def test_a_google_doc_paragraph_becomes_one_sentence(self):
        prepared = fetch(google_doc("d1", "Doc", b"First para.\nSecond para.\n"))
        assert prepared.text.count(b"\n") == 2

    def test_the_recorded_identity_comes_from_drive(self):
        prepared = fetch(
            text_file(
                "f1",
                "notes.txt",
                b"x\n",
                modified_time="2026-08-30T08:00:00.000Z",
                revision_id="rev-9",
            )
        )
        document = prepared.document
        assert document.drive_file_id == "f1"
        assert document.name == "notes.txt"
        assert document.modified_time == "2026-08-30T08:00:00.000Z"
        assert document.revision_id == "rev-9"
        assert document.imported_at == WHEN.isoformat()

    def test_a_content_fingerprint_is_recorded(self):
        first = fetch(text_file("f1", "a.txt", b"same\n")).document
        second = fetch(text_file("f2", "b.txt", b"same\n")).document
        third = fetch(text_file("f3", "c.txt", b"different\n")).document
        assert first.content_sha256 == second.content_sha256
        assert first.content_sha256 != third.content_sha256

    def test_the_fingerprint_covers_the_stored_text_not_the_raw_bytes(self):
        """Two files differing only in line endings store the same text, so
        re-importing one after the other is correctly seen as unchanged."""
        unix = fetch(text_file("f1", "a.txt", b"one\ntwo\n")).document
        windows = fetch(text_file("f2", "b.txt", b"one\r\ntwo\r\n")).document
        assert unix.content_sha256 == windows.content_sha256

    def test_the_source_text_is_the_one_the_caller_chose(self):
        prepared = fetch(
            text_file("f1", "notes.txt", b"x\n"), source_text="Google Drive/chosen.txt"
        )
        assert prepared.document.source_text == "Google Drive/chosen.txt"

    def test_the_identifier_does_not_expose_the_drive_file_id(self):
        prepared = fetch(text_file("weird/../id", "notes.txt", b"x\n"))
        assert "/" not in prepared.document.id
        assert ".." not in prepared.document.id

    def test_an_unsupported_file_is_never_downloaded(self):
        drive = FakeDrive().add(text_file("f1", "sheet.csv", b"a,b\n"))
        with pytest.raises(UnsupportedDocumentError):
            fetch_document(
                drive.client(),
                drive.files["f1"].metadata,
                SETTINGS,
                source_text="Google Drive/sheet.txt",
            )
        assert drive.calls == []

    def test_only_the_selected_file_is_ever_touched(self):
        drive = FakeDrive()
        drive.add(text_file("wanted", "wanted.txt", b"x\n"))
        drive.add(text_file("private", "private.txt", b"secret\n"))
        fetch_document(
            drive.client(),
            drive.files["wanted"].metadata,
            SETTINGS,
            source_text="Google Drive/wanted.txt",
        )
        assert drive.touched == {"wanted"}
