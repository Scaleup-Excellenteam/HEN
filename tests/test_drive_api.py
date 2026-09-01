"""Tests for the Drive import endpoints, and for search across both corpora.

Every one of these runs against a fake Drive, so the suite needs no Google
account, no credential and no network. The disabled cases matter as much as the
working ones: with the feature off, the endpoints that existed before must
answer exactly as they did.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from autocomplete import composite
from autocomplete.config import Config
from autocomplete.drive.jobs import DriveService
from autocomplete.drive.settings import DriveSettings
from autocomplete.engine import find_completions
from autocomplete.index import SearchIndex
from autocomplete.web.api import EngineState, create_app
from tests.support.fake_drive import FakeDrive, google_doc, text_file, unreachable

WHEN = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
TOKEN = "ya29.a0-a-secret-access-token"

CORPUS = (
    b"the quick brown fox\n"
    b"the quick brown dog\n"
    b"a corpus line about indexing\n"
)
NOTES = b"the quick brown cat\nimported notes about indexing\n"


@pytest.fixture(scope="module")
def base_index(tmp_path_factory) -> SearchIndex:
    root = tmp_path_factory.mktemp("corpus")
    (root / "corpus.txt").write_bytes(CORPUS)
    return SearchIndex.build(root, summary_width=5)


@pytest.fixture
def drive() -> FakeDrive:
    return (
        FakeDrive()
        .add(text_file("f1", "notes.txt", NOTES))
        .add(text_file("f2", "second.txt", b"another imported line\n"))
        .add(google_doc("d1", "Meeting Notes", b"A paragraph of minutes.\n"))
        .add(text_file("big", "big.txt", b"x" * 5000))
        .add(text_file("sheet", "sheet.csv", b"a,b\n"))
        .add(text_file("latin", "latin.txt", b"caf\xe9\n"))
        .add(text_file("broken", "broken.txt", b"x\n", fail_content=unreachable()))
    )


def make_service(tmp_path, drive: FakeDrive, worker=None, **overrides) -> DriveService:
    return DriveService(
        DriveSettings(
            **{
                "enabled": True,
                "client_id": "client-id",
                "api_key": "api-key",
                "app_id": "123456",
                "data_dir": tmp_path / "drive-data",
                "max_file_bytes": 4096,
                "max_total_bytes": 8192,
                **overrides,
            }
        ),
        Config(num_results=5, use_mmap=False),
        client_factory=drive.client,
        worker=worker or (lambda work: work()),
        now=lambda: WHEN,
    )


@pytest.fixture
def enabled(tmp_path, drive, base_index):
    """A running server with the feature configured and the corpus ready."""
    service = make_service(tmp_path, drive)
    app = create_app(prepare=False, drive=service)
    app.state.engine = EngineState(index=base_index)
    with TestClient(app) as client:
        yield client, service


@pytest.fixture
def disabled(base_index):
    """A running server with the feature switched off, as it is by default."""
    app = create_app(
        prepare=False, drive=DriveService(DriveSettings(enabled=False), Config())
    )
    app.state.engine = EngineState(index=base_index)
    with TestClient(app) as client:
        yield client


def start_import(client, *file_ids, token=TOKEN):
    headers = {"X-Drive-Access-Token": token} if token else {}
    return client.post(
        "/api/drive/imports", json={"file_ids": list(file_ids)}, headers=headers
    )


def sentences(client, query, **params):
    body = client.get("/api/complete", params={"q": query, **params}).json()
    return [result["completed_sentence"] for result in body["results"]]


class TestDisabledFeature:
    def test_status_reports_disabled(self, disabled):
        body = disabled.get("/api/drive/status").json()
        assert body["enabled"] is False
        assert body["configured"] is False
        assert body["state"] == "disabled"

    def test_no_configuration_is_handed_out(self, disabled):
        body = disabled.get("/api/drive/status").json()
        assert body["client_id"] == "" and body["api_key"] == "" and body["app_id"] == ""

    def test_listing_documents_is_not_found(self, disabled):
        response = disabled.get("/api/drive/documents")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "disabled"

    def test_importing_is_refused(self, disabled):
        assert start_import(disabled, "f1").status_code == 404

    def test_health_is_unchanged(self, disabled, base_index):
        body = disabled.get("/api/health").json()
        assert body["status"] == "ready"
        assert body["sentences"] == len(base_index)

    def test_completion_is_exactly_the_engine(self, disabled, base_index):
        for query in ["the quick", "indexing", "fox", "brown"]:
            body = disabled.get("/api/complete", params={"q": query}).json()
            expected = find_completions(base_index, query)
            assert [item["completed_sentence"] for item in body["results"]] == [
                result.completed_sentence for result in expected
            ]

    def test_the_completion_response_keeps_its_exact_shape(self, disabled):
        result = disabled.get("/api/complete", params={"q": "indexing"}).json()[
            "results"
        ][0]
        assert set(result) == {"completed_sentence", "source_text", "offset", "score"}

    def test_an_unconfigured_server_says_what_is_missing(self, base_index):
        app = create_app(
            prepare=False,
            drive=DriveService(DriveSettings(enabled=True, client_id="c"), Config()),
        )
        app.state.engine = EngineState(index=base_index)
        with TestClient(app) as client:
            body = client.get("/api/drive/status").json()
            assert body["enabled"] is True and body["configured"] is False
            assert "HEN_DRIVE_API_KEY" in body["detail"]
            assert client.get("/api/drive/documents").status_code == 503


class TestStatus:
    def test_it_serves_the_public_configuration(self, enabled):
        client, _ = enabled
        body = client.get("/api/drive/status").json()
        assert body["client_id"] == "client-id"
        assert body["api_key"] == "api-key"
        assert body["app_id"] == "123456"

    def test_it_names_the_scope_it_asks_for(self, enabled):
        client, _ = enabled
        assert client.get("/api/drive/status").json()["scope"] == (
            "https://www.googleapis.com/auth/drive.file"
        )

    def test_it_publishes_the_limits_the_interface_enforces(self, enabled):
        client, _ = enabled
        limits = client.get("/api/drive/status").json()["limits"]
        assert limits["max_file_bytes"] == 4096
        assert limits["supported_mime_types"] == [
            "text/plain",
            "application/vnd.google-apps.document",
        ]

    def test_it_names_the_source_prefix_the_interface_marks_results_with(self, enabled):
        client, _ = enabled
        assert client.get("/api/drive/status").json()["source_prefix"] == "Google Drive"

    def test_it_is_ready_with_nothing_imported(self, enabled):
        client, _ = enabled
        body = client.get("/api/drive/status").json()
        assert body["state"] == "ready"
        assert body["documents"] == 0


class TestImporting:
    def test_an_import_is_accepted_and_reports_a_job(self, enabled):
        client, _ = enabled
        response = start_import(client, "f1")
        assert response.status_code == 202
        body = response.json()
        assert body["state"] == "complete"
        assert body["progress"]["files_downloaded"] == 1

    def test_a_google_doc_imports(self, enabled):
        client, _ = enabled
        assert start_import(client, "d1").json()["state"] == "complete"

    def test_several_documents_import_together(self, enabled):
        client, _ = enabled
        assert start_import(client, "f1", "f2", "d1").json()["state"] == "complete"
        assert client.get("/api/drive/documents").json()["count"] == 3

    def test_an_import_without_authorization_is_refused(self, enabled):
        client, _ = enabled
        response = start_import(client, "f1", token=None)
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "auth_failed"

    def test_an_empty_selection_is_rejected_by_validation(self, enabled):
        client, _ = enabled
        assert start_import(client).status_code == 422

    def test_an_enormous_selection_is_rejected_by_validation(self, enabled):
        client, _ = enabled
        response = client.post(
            "/api/drive/imports",
            json={"file_ids": [f"f{n}" for n in range(500)]},
            headers={"X-Drive-Access-Token": TOKEN},
        )
        assert response.status_code == 422

    def test_a_body_that_is_not_a_selection_is_rejected(self, enabled):
        client, _ = enabled
        for body in [{}, {"file_ids": "f1"}, {"file_ids": [None]}, {"other": ["f1"]}]:
            response = client.post(
                "/api/drive/imports", json=body, headers={"X-Drive-Access-Token": TOKEN}
            )
            assert response.status_code == 422, body

    def test_no_endpoint_accepts_a_local_path(self, enabled):
        client, _ = enabled
        response = start_import(client, "../../etc/passwd")
        # Accepted as an identifier by the request layer, then simply not found
        # in Drive: nothing ever treats it as a path.
        assert response.json()["error"]["code"] == "not_found"

    def test_no_endpoint_accepts_a_url_to_download(self, enabled):
        client, _ = enabled
        response = start_import(client, "https://evil.example.com/payload.txt")
        assert response.json()["error"]["code"] == "not_found"

    def test_an_unsupported_type_is_reported(self, enabled):
        client, _ = enabled
        body = start_import(client, "sheet").json()
        assert body["state"] == "failed"
        assert body["error"]["code"] == "unsupported"

    def test_an_oversized_file_is_reported(self, enabled):
        client, _ = enabled
        assert start_import(client, "big").json()["error"]["code"] == "too_large"

    def test_invalid_encoding_is_reported(self, enabled):
        client, _ = enabled
        assert start_import(client, "latin").json()["error"]["code"] == "invalid_encoding"

    def test_a_download_failure_is_reported_as_retryable(self, enabled):
        client, _ = enabled
        error = start_import(client, "broken").json()["error"]
        assert error["code"] == "transport"
        assert error["retryable"] is True

    def test_a_job_can_be_polled_by_identifier(self, enabled):
        client, _ = enabled
        job_id = start_import(client, "f1").json()["id"]
        assert client.get(f"/api/drive/imports/{job_id}").json()["state"] == "complete"

    def test_an_unknown_job_identifier_is_not_found(self, enabled):
        client, _ = enabled
        assert client.get("/api/drive/imports/" + "0" * 32).status_code == 404

    def test_a_job_identifier_that_is_not_hex_is_rejected(self, enabled):
        client, _ = enabled
        assert client.get("/api/drive/imports/../../etc").status_code in (404, 422)
        assert client.get("/api/drive/imports/NOT-HEX").status_code == 422


class TestConcurrentJobs:
    def test_a_second_import_while_one_runs_is_a_conflict(self, tmp_path, drive, base_index):
        held = []
        service = make_service(tmp_path, drive, worker=held.append)
        app = create_app(prepare=False, drive=service)
        app.state.engine = EngineState(index=base_index)
        with TestClient(app) as client:
            assert start_import(client, "f1").status_code == 202
            conflict = start_import(client, "f2")
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "busy"

    def test_search_keeps_working_while_an_import_runs(
        self, tmp_path, drive, base_index
    ):
        held = []
        service = make_service(tmp_path, drive, worker=held.append)
        app = create_app(prepare=False, drive=service)
        app.state.engine = EngineState(index=base_index)
        with TestClient(app) as client:
            start_import(client, "f1")
            assert client.get("/api/drive/status").json()["state"] == "downloading"
            # Still the previous searchable state: the corpus alone.
            assert sentences(client, "the quick brown") == [
                "the quick brown dog",
                "the quick brown fox",
            ]
            held[0]()
            assert "the quick brown cat" in sentences(client, "the quick brown")

    def test_a_search_never_sees_a_half_built_index(self, tmp_path, drive, base_index):
        """The imported index is published by one assignment of a finished
        object, so a query gets the whole of one state or the whole of the
        other."""
        held = []
        service = make_service(tmp_path, drive, worker=held.append)
        app = create_app(prepare=False, drive=service)
        app.state.engine = EngineState(index=base_index)
        with TestClient(app) as client:
            start_import(client, "f1")
            during = sentences(client, "indexing")
            held[0]()
            after = sentences(client, "indexing")
        assert during == ["a corpus line about indexing"]
        assert after == [
            "a corpus line about indexing",
            "imported notes about indexing",
        ]


class TestSearchAcrossBoth:
    def test_imported_sentences_become_searchable(self, enabled):
        client, _ = enabled
        start_import(client, "f1")
        assert "imported notes about indexing" in sentences(client, "indexing")

    def test_results_are_globally_ranked(self, enabled, base_index):
        client, service = enabled
        start_import(client, "f1")
        for query in ["the quick brown", "indexing", "brown", "about"]:
            expected = composite.search(base_index, service.overlay, query, 5)
            assert sentences(client, query) == [
                result.completed_sentence for result in expected
            ]

    def test_imported_results_do_not_outrank_the_corpus_automatically(self, enabled):
        client, _ = enabled
        start_import(client, "f1")
        # cat, dog, fox tie on score, so they interleave alphabetically rather
        # than the imported one being placed first or last as a group.
        assert sentences(client, "the quick brown") == [
            "the quick brown cat",
            "the quick brown dog",
            "the quick brown fox",
        ]

    def test_a_result_reports_its_own_source_and_line(self, enabled):
        client, _ = enabled
        start_import(client, "f1")
        body = client.get("/api/complete", params={"q": "imported notes"}).json()
        result = body["results"][0]
        assert result["source_text"] == "Google Drive/notes.txt"
        assert result["offset"] == 2

    def test_the_response_shape_is_unchanged(self, enabled):
        client, _ = enabled
        start_import(client, "f1")
        result = client.get("/api/complete", params={"q": "imported"}).json()["results"][0]
        assert set(result) == {"completed_sentence", "source_text", "offset", "score"}

    def test_the_limit_still_applies_across_both(self, enabled):
        client, _ = enabled
        start_import(client, "f1", "f2")
        assert len(sentences(client, "the", limit=2)) <= 2

    def test_asking_for_more_than_k_is_still_rejected(self, enabled):
        client, _ = enabled
        start_import(client, "f1")
        assert client.get("/api/complete", params={"q": "the", "limit": 9}).status_code == 400

    def test_removing_a_document_removes_its_sentences(self, enabled):
        client, _ = enabled
        start_import(client, "f1")
        document_id = client.get("/api/drive/documents").json()["documents"][0]["id"]
        assert client.delete(f"/api/drive/documents/{document_id}").status_code == 202
        assert "imported notes about indexing" not in sentences(client, "indexing")

    def test_the_corpus_is_untouched_by_an_import(self, enabled, base_index):
        client, _ = enabled
        before = len(base_index)
        start_import(client, "f1", "f2")
        assert len(base_index) == before
        assert client.get("/api/health").json()["sentences"] == before


class TestListingAndRemoval:
    def test_an_empty_list_before_anything_is_imported(self, enabled):
        client, _ = enabled
        body = client.get("/api/drive/documents").json()
        assert body == {"count": 0, "total_bytes": 0, "documents": []}

    def test_imported_documents_are_listed(self, enabled):
        client, _ = enabled
        start_import(client, "f1", "d1")
        body = client.get("/api/drive/documents").json()
        assert body["count"] == 2
        assert {document["name"] for document in body["documents"]} == {
            "notes.txt",
            "Meeting Notes",
        }

    def test_a_listing_never_exposes_the_drive_file_identifier(self, enabled):
        client, _ = enabled
        start_import(client, "f1")
        body = client.get("/api/drive/documents").json()
        assert "drive_file_id" not in body["documents"][0]
        assert "f1" not in str(body["documents"][0].get("id", ""))

    def test_a_listing_reports_what_each_contributed(self, enabled):
        client, _ = enabled
        start_import(client, "f1")
        document = client.get("/api/drive/documents").json()["documents"][0]
        assert document["sentences"] == 2
        assert document["bytes"] == len(NOTES)
        assert document["status"] == "indexed"

    def test_removing_a_missing_document_is_not_found(self, enabled):
        client, _ = enabled
        response = client.delete("/api/drive/documents/" + "a" * 16)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "not_found"

    @pytest.mark.parametrize(
        "identifier",
        ["NOT-HEX", "abc def", "../etc", "a" * 100, "%2e%2e", "id;rm -rf /"],
    )
    def test_a_removal_identifier_that_is_not_hex_is_rejected(self, enabled, identifier):
        """Refused either by the pattern or by not resolving to a route at all.
        Neither outcome reaches the filesystem, which is the property that
        matters."""
        client, _ = enabled
        response = client.delete(f"/api/drive/documents/{identifier}")
        assert response.status_code in (404, 422), identifier
        assert "Traceback" not in response.text

    def test_removing_the_last_document_returns_to_the_corpus_alone(
        self, enabled, base_index
    ):
        client, service = enabled
        start_import(client, "f1")
        document_id = client.get("/api/drive/documents").json()["documents"][0]["id"]
        client.delete(f"/api/drive/documents/{document_id}")
        assert service.overlay is None
        assert sentences(client, "the quick brown") == [
            result.completed_sentence
            for result in find_completions(base_index, "the quick brown")
        ]


class TestRetry:
    def test_a_failed_import_can_be_retried(self, tmp_path, drive, base_index):
        service = make_service(tmp_path, drive)
        app = create_app(prepare=False, drive=service)
        app.state.engine = EngineState(index=base_index)
        with TestClient(app) as client:
            assert start_import(client, "broken").json()["state"] == "failed"
            drive.add(text_file("broken", "broken.txt", b"now it works\n"))
            response = client.post(
                "/api/drive/retry", headers={"X-Drive-Access-Token": TOKEN}
            )
            assert response.status_code == 202
            assert response.json()["state"] == "complete"

    def test_retrying_an_import_without_a_token_is_refused(self, enabled):
        client, _ = enabled
        start_import(client, "broken")
        response = client.post("/api/drive/retry")
        assert response.status_code == 500 or response.status_code == 400
        assert "authorization" in response.json()["detail"]["message"]

    def test_retrying_with_nothing_failed_is_not_found(self, enabled):
        client, _ = enabled
        response = client.post(
            "/api/drive/retry", headers={"X-Drive-Access-Token": TOKEN}
        )
        assert response.status_code == 404


class TestPersistence:
    def test_imports_survive_a_server_restart(self, tmp_path, drive, base_index):
        first = make_service(tmp_path, drive)
        app = create_app(prepare=False, drive=first)
        app.state.engine = EngineState(index=base_index)
        with TestClient(app) as client:
            start_import(client, "f1")

        second = make_service(tmp_path, drive)
        restarted = create_app(prepare=False, drive=second)
        restarted.state.engine = EngineState(index=base_index)
        with TestClient(restarted) as client:
            assert client.get("/api/drive/documents").json()["count"] == 1
            assert "imported notes about indexing" in sentences(client, "indexing")

    def test_a_malformed_state_reports_itself_and_leaves_search_working(
        self, tmp_path, drive, base_index
    ):
        first = make_service(tmp_path, drive)
        app = create_app(prepare=False, drive=first)
        app.state.engine = EngineState(index=base_index)
        with TestClient(app) as client:
            start_import(client, "f1")
        (first.corpus.generation / "manifest.json").write_text("{", encoding="utf-8")

        second = make_service(tmp_path, drive)
        restarted = create_app(prepare=False, drive=second)
        restarted.state.engine = EngineState(index=base_index)
        with TestClient(restarted) as client:
            assert client.get("/api/drive/status").json()["load_error"]
            assert sentences(client, "the quick brown") == [
                "the quick brown dog",
                "the quick brown fox",
            ]


class TestNothingSensitiveEscapes:
    def test_no_response_carries_the_access_token(self, enabled):
        client, _ = enabled
        start_import(client, "f1")
        for path in ("/api/drive/status", "/api/drive/documents"):
            assert TOKEN not in client.get(path).text

    def test_no_error_response_carries_the_access_token(self, enabled):
        client, _ = enabled
        for file_id in ("sheet", "big", "latin", "broken", "nope"):
            assert TOKEN not in start_import(client, file_id).text

    def test_nothing_is_logged_that_should_not_be(self, enabled, caplog):
        """Neither the token nor a line of an imported document reaches the log."""
        client, _ = enabled
        with caplog.at_level(logging.DEBUG):
            start_import(client, "f1")
            client.get("/api/complete", params={"q": "imported notes"})
            start_import(client, "latin")
            start_import(client, "broken")

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert TOKEN not in logged
        assert "imported notes about indexing" not in logged
        assert "Authorization" not in logged
        assert "Bearer" not in logged

    def test_an_error_body_never_carries_a_traceback(self, enabled):
        client, _ = enabled
        for response in (
            client.get("/api/drive/documents/nope"),
            client.delete("/api/drive/documents/" + "a" * 16),
            start_import(client, "sheet"),
        ):
            assert "Traceback" not in response.text

    def test_a_document_name_is_returned_as_data_not_markup(self, tmp_path, base_index):
        """A name from Drive is untrusted text. It comes back as a JSON string,
        so nothing about it can become markup in the page that shows it."""
        drive = FakeDrive().add(
            text_file("x", "<img src=x onerror=alert(1)>.txt", b"line\n")
        )
        service = make_service(tmp_path, drive)
        app = create_app(prepare=False, drive=service)
        app.state.engine = EngineState(index=base_index)
        with TestClient(app) as client:
            start_import(client, "x")
            document = client.get("/api/drive/documents").json()["documents"][0]
            assert document["name"] == "<img src=x onerror=alert(1)>.txt"
            assert "<" not in document["source_text"]
            # Everything outside the allowed set becomes an underscore, so the
            # angle brackets and equals signs cannot survive into a path.
            assert document["source_text"] == (
                "Google Drive/img src_x onerror_alert(1).txt"
            )
