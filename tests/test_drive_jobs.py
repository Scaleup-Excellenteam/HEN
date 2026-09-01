"""Tests for the import and removal lifecycle.

Jobs run inline here, through a worker the test supplies, so an assertion never
races a thread. The one test that needs a job to be genuinely in flight holds it
open deliberately.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autocomplete.config import Config
from autocomplete.drive.errors import (
    DocumentNotFoundError,
    DriveDisabledError,
    DriveError,
    DriveNotConfiguredError,
    ImportLimitError,
    JobInProgressError,
)
from autocomplete.drive.jobs import (
    DriveService,
    JobState,
    ServiceState,
)
from autocomplete.drive.settings import DriveSettings
from tests.support.fake_drive import (
    FakeDrive,
    google_doc,
    rate_limited,
    text_file,
    unreachable,
)

WHEN = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def settings(tmp_path, **overrides) -> DriveSettings:
    return DriveSettings(
        **{
            "enabled": True,
            "client_id": "client",
            "api_key": "key",
            "app_id": "123",
            "data_dir": tmp_path / "drive-data",
            **overrides,
        }
    )


def service(tmp_path, drive: FakeDrive | None = None, **overrides) -> DriveService:
    """A service whose jobs run inline, against a fake Drive."""
    drive = drive if drive is not None else FakeDrive()
    return DriveService(
        settings(tmp_path, **overrides),
        Config(num_results=5, use_mmap=False),
        client_factory=drive.client,
        worker=lambda work: work(),
        now=lambda: WHEN,
    )


def run_import(service_under_test, *file_ids, token="test-token"):
    job = service_under_test.start_import(list(file_ids), token)
    return job


@pytest.fixture
def drive() -> FakeDrive:
    return (
        FakeDrive()
        .add(text_file("f1", "notes.txt", b"first note line\nsecond note line\n"))
        .add(text_file("f2", "other.txt", b"other content here\n"))
        .add(google_doc("d1", "Meeting Notes", b"A paragraph of minutes.\n"))
    )


class TestDisabled:
    def test_a_disabled_service_refuses_an_import(self, tmp_path):
        disabled = DriveService(DriveSettings(enabled=False), Config())
        with pytest.raises(DriveDisabledError):
            disabled.start_import(["f1"], "token")

    def test_a_disabled_service_reports_the_disabled_state(self, tmp_path):
        status = DriveService(DriveSettings(enabled=False), Config()).status()
        assert status["state"] == ServiceState.DISABLED.value
        assert status["enabled"] is False

    def test_a_disabled_service_hands_out_no_configuration(self):
        status = DriveService(
            DriveSettings(enabled=False, client_id="leak", api_key="leak", app_id="leak"),
            Config(),
        ).status()
        assert (status["client_id"], status["api_key"], status["app_id"]) == ("", "", "")

    def test_a_disabled_service_has_no_overlay(self):
        assert DriveService(DriveSettings(enabled=False), Config()).overlay is None

    def test_a_disabled_service_touches_no_disk(self, tmp_path):
        disabled = DriveService(
            DriveSettings(enabled=False, data_dir=tmp_path / "nope"), Config()
        )
        disabled.load_published_state()
        assert not (tmp_path / "nope").exists()

    def test_an_unconfigured_service_says_what_is_missing(self, tmp_path):
        incomplete = DriveService(
            DriveSettings(enabled=True, client_id="c", data_dir=tmp_path / "d"), Config()
        )
        status = incomplete.status()
        assert status["state"] == ServiceState.DISABLED.value
        assert status["enabled"] is True
        assert status["configured"] is False
        assert "HEN_DRIVE_API_KEY" in status["detail"]

    def test_an_unconfigured_service_refuses_an_import(self, tmp_path):
        incomplete = DriveService(
            DriveSettings(enabled=True, data_dir=tmp_path / "d"), Config()
        )
        with pytest.raises(DriveNotConfiguredError):
            incomplete.start_import(["f1"], "token")


class TestImporting:
    def test_a_plain_text_file_becomes_searchable(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        job = run_import(under_test, "f1")
        assert job.state is JobState.COMPLETE
        assert under_test.overlay is not None
        assert len(under_test.overlay) == 2

    def test_a_google_doc_becomes_searchable(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "d1")
        assert under_test.documents[0].name == "Meeting Notes"
        assert under_test.documents[0].source_text == "Google Drive/Meeting Notes.txt"

    def test_several_documents_import_together(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        job = run_import(under_test, "f1", "f2", "d1")
        assert job.progress.files_downloaded == 3
        assert len(under_test.documents) == 3

    def test_only_the_selected_files_are_touched(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        assert drive.touched == {"f1"}

    def test_a_second_import_keeps_the_first(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        run_import(under_test, "f2")
        assert {document.name for document in under_test.documents} == {
            "notes.txt",
            "other.txt",
        }

    def test_a_second_import_does_not_download_the_first_again(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        drive.calls.clear()
        run_import(under_test, "f2")
        assert drive.touched == {"f2"}

    def test_a_duplicate_selection_is_imported_once(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        job = run_import(under_test, "f1", "f1", "f1")
        assert job.progress.files_selected == 1
        assert len(under_test.documents) == 1

    def test_progress_counts_what_actually_happened(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        job = run_import(under_test, "f1")
        assert job.progress.files_selected == 1
        assert job.progress.files_downloaded == 1
        assert job.progress.bytes_downloaded == 33
        assert job.progress.lines_read == 2
        assert job.progress.sentences_indexed == 2

    def test_progress_offers_no_invented_percentage(self, tmp_path, drive):
        job = run_import(service(tmp_path, drive), "f1")
        assert "percent" not in job.to_json()["progress"]
        assert "progress" not in job.to_json()["progress"]

    def test_the_job_never_carries_the_access_token(self, tmp_path, drive):
        job = run_import(service(tmp_path, drive), "f1", token="ya29.secret")
        assert "ya29.secret" not in str(job.to_json())
        assert "ya29.secret" not in str(vars(job))

    def test_an_empty_selection_is_refused(self, tmp_path, drive):
        with pytest.raises(ImportLimitError, match="at least one"):
            run_import(service(tmp_path, drive))

    def test_more_files_than_the_limit_are_refused(self, tmp_path, drive):
        under_test = service(tmp_path, drive, max_files=2)
        with pytest.raises(ImportLimitError, match="at most 2"):
            run_import(under_test, "f1", "f2", "d1")

    @pytest.mark.parametrize("bad", ["", "   ", "x" * 300, "with\nnewline", "\x00"])
    def test_an_implausible_identifier_is_refused(self, tmp_path, drive, bad):
        with pytest.raises(ImportLimitError, match="not usable"):
            run_import(service(tmp_path, drive), bad)

    def test_a_non_string_identifier_is_refused(self, tmp_path, drive):
        with pytest.raises(ImportLimitError, match="not text"):
            service(tmp_path, drive).start_import([123], "token")  # type: ignore[list-item]

    def test_the_total_size_limit_is_enforced(self, tmp_path):
        drive = (
            FakeDrive()
            .add(text_file("a", "a.txt", b"x" * 60 + b"\n"))
            .add(text_file("b", "b.txt", b"y" * 60 + b"\n"))
        )
        under_test = service(tmp_path, drive, max_file_bytes=100, max_total_bytes=100)
        job = run_import(under_test, "a", "b")
        assert job.state is JobState.FAILED
        assert job.error_code == "limit"
        assert under_test.overlay is None


class TestReimporting:
    def test_an_unchanged_file_is_not_downloaded_again(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        drive.calls.clear()

        job = run_import(under_test, "f1")
        assert job.state is JobState.COMPLETE
        assert job.progress.files_reused == 1
        assert job.progress.files_downloaded == 0
        assert [operation for operation, _ in drive.calls] == ["metadata"]

    def test_an_unchanged_file_leaves_the_index_alone(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        before = under_test.corpus.generation
        run_import(under_test, "f1")
        assert under_test.corpus.generation == before

    def test_a_changed_revision_replaces_the_content(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")

        drive.add(
            text_file("f1", "notes.txt", b"rewritten entirely\n", revision_id="rev-2")
        )
        job = run_import(under_test, "f1")

        assert job.progress.files_downloaded == 1
        assert len(under_test.documents) == 1
        assert under_test.overlay is not None
        assert under_test.overlay.records.sentence(0) == "rewritten entirely"

    def test_a_changed_revision_keeps_the_original_source_text(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        before = under_test.documents[0].source_text

        drive.add(text_file("f1", "renamed.txt", b"new\n", revision_id="rev-2"))
        run_import(under_test, "f1")
        assert under_test.documents[0].source_text == before

    def test_two_files_with_the_same_name_get_distinct_sources(self, tmp_path):
        drive = (
            FakeDrive()
            .add(text_file("a", "notes.txt", b"from a\n"))
            .add(text_file("b", "notes.txt", b"from b\n"))
        )
        under_test = service(tmp_path, drive)
        run_import(under_test, "a", "b")
        assert {document.source_text for document in under_test.documents} == {
            "Google Drive/notes.txt",
            "Google Drive/notes (2).txt",
        }


class TestFailures:
    def test_a_download_failure_leaves_the_previous_state_searchable(
        self, tmp_path, drive
    ):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        good = under_test.corpus.generation

        drive.add(text_file("bad", "bad.txt", b"x\n", fail_content=unreachable()))
        job = run_import(under_test, "bad")

        assert job.state is JobState.FAILED
        assert job.error_code == "transport"
        assert job.retryable is True
        assert under_test.corpus.generation == good
        assert len(under_test.overlay) == 2

    def test_an_authorization_failure_is_reported_as_such(self, tmp_path, drive):
        under_test = service(tmp_path, drive.with_token("right"))
        job = under_test.start_import(["f1"], "wrong")
        assert job.state is JobState.FAILED
        assert job.error_code == "auth_failed"

    def test_a_quota_failure_is_reported_as_retryable(self, tmp_path):
        drive = FakeDrive().add(
            text_file("f1", "notes.txt", b"x\n", fail_metadata=rate_limited())
        )
        job = run_import(service(tmp_path, drive), "f1")
        assert job.error_code == "quota"
        assert job.retryable is True

    def test_an_unsupported_file_fails_before_any_index_is_built(self, tmp_path):
        drive = FakeDrive().add(text_file("f1", "data.csv", b"a,b\n"))
        under_test = service(tmp_path, drive)
        job = run_import(under_test, "f1")
        assert job.error_code == "unsupported"
        assert under_test.overlay is None

    def test_an_oversized_file_fails_without_touching_the_state(self, tmp_path, drive):
        under_test = service(tmp_path, drive, max_file_bytes=4)
        job = run_import(under_test, "f1")
        assert job.error_code == "too_large"
        assert under_test.corpus is None

    def test_invalid_encoding_is_reported(self, tmp_path):
        drive = FakeDrive().add(text_file("f1", "notes.txt", b"caf\xe9\n"))
        job = run_import(service(tmp_path, drive), "f1")
        assert job.error_code == "invalid_encoding"

    def test_an_index_build_failure_preserves_the_previous_index(
        self, tmp_path, drive, monkeypatch
    ):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        good = under_test.corpus

        monkeypatch.setattr(
            "autocomplete.drive.store.SearchIndex.build",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))),
        )
        job = run_import(under_test, "f2")

        assert job.state is JobState.FAILED
        assert under_test.corpus is good
        assert len(under_test.overlay) == 2

    def test_an_unexpected_failure_does_not_leak_its_message(
        self, tmp_path, drive, monkeypatch
    ):
        under_test = service(tmp_path, drive)
        monkeypatch.setattr(
            "autocomplete.drive.store.SearchIndex.build",
            staticmethod(
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("/secret/path leaked"))
            ),
        )
        job = run_import(under_test, "f1")
        assert job.error_code == "internal"
        assert "/secret/path" not in (job.error_message or "")
        assert "still searchable" in (job.error_message or "")

    def test_a_failure_leaves_the_service_usable(self, tmp_path, drive):
        under_test = service(tmp_path, drive.with_token("right"))
        under_test.start_import(["f1"], "wrong")
        assert under_test.state is ServiceState.FAILED

        job = under_test.start_import(["f1"], "right")
        assert job.state is JobState.COMPLETE
        assert under_test.state is ServiceState.READY


class TestRemoval:
    def test_a_document_can_be_removed(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1", "f2")
        target = next(d for d in under_test.documents if d.name == "notes.txt")

        job = under_test.start_removal(target.id)
        assert job.state is JobState.COMPLETE
        assert [d.name for d in under_test.documents] == ["other.txt"]

    def test_removal_does_not_go_back_to_drive(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1", "f2")
        drive.calls.clear()
        under_test.start_removal(under_test.documents[0].id)
        assert drive.calls == []

    def test_removing_the_last_document_leaves_no_overlay(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        under_test.start_removal(under_test.documents[0].id)
        assert under_test.overlay is None
        assert under_test.documents == ()

    def test_removing_something_that_is_not_there_is_refused(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        with pytest.raises(DocumentNotFoundError):
            under_test.start_removal("no-such-document")

    def test_removing_from_an_empty_state_is_refused(self, tmp_path, drive):
        with pytest.raises(DocumentNotFoundError):
            service(tmp_path, drive).start_removal("anything")

    def test_a_failed_removal_keeps_the_document(self, tmp_path, drive, monkeypatch):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1", "f2")
        target = under_test.documents[0].id

        monkeypatch.setattr(
            "autocomplete.drive.store.SearchIndex.build",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))),
        )
        job = under_test.start_removal(target)
        assert job.state is JobState.FAILED
        assert len(under_test.documents) == 2


class TestConcurrency:
    def test_a_second_change_while_one_runs_is_refused(self, tmp_path, drive):
        held = []
        under_test = DriveService(
            settings(tmp_path),
            Config(num_results=5, use_mmap=False),
            client_factory=drive.client,
            # Keep the work rather than run it, so the job stays in flight.
            worker=held.append,
            now=lambda: WHEN,
        )
        under_test.start_import(["f1"], "token")

        with pytest.raises(JobInProgressError, match="already running"):
            under_test.start_import(["f2"], "token")

        held[0]()  # let the first finish
        second = under_test.start_import(["f2"], "token")
        held[1]()
        assert second.state is JobState.COMPLETE

    def test_searching_is_never_blocked_by_a_running_change(self, tmp_path, drive):
        held = []
        under_test = DriveService(
            settings(tmp_path),
            Config(num_results=5, use_mmap=False),
            client_factory=drive.client,
            worker=held.append,
            now=lambda: WHEN,
        )
        under_test.start_import(["f1"], "token")
        # The overlay is readable throughout, and is still the previous state.
        assert under_test.overlay is None
        assert under_test.state is ServiceState.DOWNLOADING

        held[0]()
        assert under_test.overlay is not None

    def test_a_running_change_reports_its_state(self, tmp_path, drive):
        held = []
        under_test = DriveService(
            settings(tmp_path),
            Config(num_results=5, use_mmap=False),
            client_factory=drive.client,
            worker=held.append,
            now=lambda: WHEN,
        )
        job = under_test.start_import(["f1"], "token")
        assert under_test.status()["job"]["state"] == "downloading"
        assert under_test.busy is True
        held[0]()
        assert under_test.busy is False
        assert under_test.job(job.id).state is JobState.COMPLETE


class TestRetry:
    def test_a_failed_import_can_be_retried_with_a_fresh_token(self, tmp_path, drive):
        under_test = service(tmp_path, drive.with_token("right"))
        under_test.start_import(["f1"], "wrong")
        job = under_test.retry("right")
        assert job.state is JobState.COMPLETE
        assert len(under_test.documents) == 1

    def test_retrying_an_import_without_a_token_says_so(self, tmp_path, drive):
        under_test = service(tmp_path, drive.with_token("right"))
        under_test.start_import(["f1"], "wrong")
        with pytest.raises(DriveError, match="fresh Google authorization"):
            under_test.retry()

    def test_a_failed_removal_can_be_retried_without_a_token(
        self, tmp_path, drive, monkeypatch
    ):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1", "f2")
        target = under_test.documents[0].id

        real_build = __import__(
            "autocomplete.drive.store", fromlist=["SearchIndex"]
        ).SearchIndex.build
        monkeypatch.setattr(
            "autocomplete.drive.store.SearchIndex.build",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))),
        )
        assert under_test.start_removal(target).state is JobState.FAILED

        monkeypatch.setattr("autocomplete.drive.store.SearchIndex.build", real_build)
        assert under_test.retry().state is JobState.COMPLETE
        assert len(under_test.documents) == 1

    def test_retrying_when_nothing_failed_is_refused(self, tmp_path, drive):
        with pytest.raises(DocumentNotFoundError, match="no failed change"):
            service(tmp_path, drive).retry("token")


class TestPersistence:
    def test_imports_survive_a_restart(self, tmp_path, drive):
        first = service(tmp_path, drive)
        run_import(first, "f1", "d1")

        second = service(tmp_path, drive)
        second.load_published_state()
        assert len(second.documents) == 2
        assert second.overlay is not None
        assert len(second.overlay) == 3

    def test_a_restart_with_nothing_imported_finds_nothing(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        under_test.load_published_state()
        assert under_test.overlay is None
        assert under_test.load_error is None

    def test_a_malformed_state_is_reported_and_does_not_raise(self, tmp_path, drive):
        first = service(tmp_path, drive)
        run_import(first, "f1")
        (first.corpus.generation / "manifest.json").write_text("{", encoding="utf-8")

        second = service(tmp_path, drive)
        second.load_published_state()
        assert second.overlay is None
        assert "not valid JSON" in (second.load_error or "")
        assert second.status()["load_error"]

    def test_a_removal_survives_a_restart(self, tmp_path, drive):
        first = service(tmp_path, drive)
        run_import(first, "f1", "f2")
        first.start_removal(first.documents[0].id)

        second = service(tmp_path, drive)
        second.load_published_state()
        assert len(second.documents) == 1


class TestStatus:
    def test_it_reports_the_public_configuration(self, tmp_path, drive):
        status = service(tmp_path, drive).status()
        assert status["client_id"] == "client"
        assert status["api_key"] == "key"
        assert status["app_id"] == "123"
        assert status["scope"] == "https://www.googleapis.com/auth/drive.file"
        assert status["source_prefix"] == "Google Drive"

    def test_it_asks_for_the_narrowest_scope(self, tmp_path, drive):
        assert service(tmp_path, drive).status()["scope"].endswith("drive.file")

    def test_it_reports_the_limits(self, tmp_path, drive):
        limits = service(tmp_path, drive).status()["limits"]
        assert limits["max_files"] == 10
        assert limits["supported_mime_types"] == [
            "text/plain",
            "application/vnd.google-apps.document",
        ]

    def test_it_counts_what_is_imported(self, tmp_path, drive):
        under_test = service(tmp_path, drive)
        run_import(under_test, "f1")
        status = under_test.status()
        assert status["documents"] == 1
        assert status["sentences"] == 2
        assert status["total_bytes"] == 33

    def test_an_empty_state_reads_sensibly(self, tmp_path, drive):
        status = service(tmp_path, drive).status()
        assert status["state"] == "ready"
        assert status["documents"] == 0
        assert status["detail"] == "No documents imported yet."
