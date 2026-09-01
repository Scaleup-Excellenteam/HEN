"""Tests for the HTTP interface.

The API must add nothing to the search, so most of these compare what it returns
against what the engine returns directly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autocomplete.config import Config
from autocomplete.engine import find_completions
from autocomplete.index import SearchIndex
from autocomplete.web.api import EngineState, create_app

DEMO = (
    b"Alpha: this is a demo.\n"
    b"Beta: this is a demo.\n"
    b"Delta: this is a demo.\n"
    b"Gamma: this is a demo.\n"
    b"Omega: this is a demo.\n"
)


def write_corpus(root, files: dict[str, bytes]):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


@pytest.fixture(scope="module")
def index(tmp_path_factory) -> SearchIndex:
    root = write_corpus(
        tmp_path_factory.mktemp("corpus"),
        {"example.txt": DEMO, "deep/more.txt": b"one of a kind\n"},
    )
    return SearchIndex.build(root, summary_width=5)


@pytest.fixture
def client(index) -> TestClient:
    """A server whose index is already prepared."""
    app = create_app(prepare=False)
    app.state.engine = EngineState(index=index)
    with TestClient(app) as running:
        yield running


@pytest.fixture
def preparing_client() -> TestClient:
    """A server whose index has not finished preparing."""
    app = create_app(prepare=False)
    with TestClient(app) as running:
        yield running


@pytest.fixture
def failed_client() -> TestClient:
    """A server whose index could not be prepared."""
    app = create_app(prepare=False)
    app.state.engine = EngineState(error="CorpusNotFoundError: no such directory")
    with TestClient(app) as running:
        yield running


class TestHealth:
    def test_reports_ready_with_the_corpus_size(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ready"
        assert body["ready"] is True
        assert body["sentences"] == 6
        assert body["sources"] == 2

    def test_reports_preparing_before_the_index_exists(self, preparing_client):
        body = preparing_client.get("/api/health").json()
        assert body["status"] == "preparing"
        assert body["ready"] is False
        assert body["detail"]

    def test_reports_failure_without_leaking_internals(self, failed_client):
        body = failed_client.get("/api/health").json()
        assert body["status"] == "failed"
        assert body["ready"] is False
        assert "corpus_root" in body["detail"]
        assert "Traceback" not in body["detail"]

    def test_health_is_always_answerable(self, preparing_client):
        assert preparing_client.get("/api/health").status_code == 200


class TestCompletions:
    def test_an_exact_query(self, client):
        body = client.get("/api/complete", params={"q": "this is"}).json()
        assert body["count"] == 5
        assert body["results"][0]["completed_sentence"] == "Alpha: this is a demo."

    @pytest.mark.parametrize(
        "query,kind",
        [
            ("thi is a demo", "a missing character"),
            ("this is a demoo", "one character too many"),
            ("this is a deno", "one character wrong"),
        ],
    )
    def test_a_query_with_one_typing_error(self, client, query, kind):
        body = client.get("/api/complete", params={"q": query}).json()
        assert body["count"] >= 1, kind
        assert all(result["score"] > 0 for result in body["results"])

    def test_two_typing_errors_find_nothing(self, client):
        """"thsi" transposes two characters, which is two edits, not one."""
        assert client.get("/api/complete", params={"q": "thsi is a demo"}).json()["count"] == 0

    def test_every_field_is_serialized(self, client):
        result = client.get("/api/complete", params={"q": "one of"}).json()["results"][0]
        assert result == {
            "completed_sentence": "one of a kind",
            "source_text": "deep/more.txt",
            "offset": 1,
            "score": 12,
        }

    def test_the_query_comes_back_unmodified(self, client):
        typed = "  ThIs   Is,  "
        body = client.get("/api/complete", params={"q": typed}).json()
        assert body["query"] == typed

    def test_order_matches_the_engine_exactly(self, client, index):
        for query in ["this is", "demo", "thi is", "one of", "a", "this is a demoo"]:
            body = client.get("/api/complete", params={"q": query}).json()
            expected = find_completions(index, query)
            assert [item["completed_sentence"] for item in body["results"]] == [
                result.completed_sentence for result in expected
            ]
            assert [item["score"] for item in body["results"]] == [
                result.score for result in expected
            ]
            assert [item["offset"] for item in body["results"]] == [
                result.offset for result in expected
            ]

    def test_no_duplicate_sentences_in_one_answer(self, client):
        results = client.get("/api/complete", params={"q": "demo"}).json()["results"]
        seen = [(item["source_text"], item["offset"]) for item in results]
        assert len(seen) == len(set(seen))

    def test_a_query_with_no_matches(self, client):
        body = client.get("/api/complete", params={"q": "zzzzqqqq"}).json()
        assert body["count"] == 0
        assert body["results"] == []

    @pytest.mark.parametrize("query", ["", "   ", "\t"])
    def test_an_empty_query_is_answered_without_searching(self, client, query):
        body = client.get("/api/complete", params={"q": query}).json()
        assert body["count"] == 0

    def test_a_missing_query_parameter_is_an_empty_query(self, client):
        assert client.get("/api/complete").json()["count"] == 0

    def test_the_limit_can_be_lowered(self, client):
        body = client.get("/api/complete", params={"q": "this is", "limit": 2}).json()
        assert body["count"] == 2

    def test_asking_for_more_than_the_index_answers_is_rejected(self, client):
        response = client.get("/api/complete", params={"q": "this is", "limit": 9})
        assert response.status_code == 400
        assert "message" in response.json()["detail"]

    def test_an_out_of_range_limit_is_rejected(self, client):
        assert client.get("/api/complete", params={"q": "a", "limit": 0}).status_code == 422

    def test_an_overlong_query_is_rejected(self, client):
        response = client.get("/api/complete", params={"q": "a" * 5000})
        assert response.status_code == 422


class TestUnavailableIndex:
    def test_searching_while_preparing_is_refused_with_a_state(self, preparing_client):
        response = preparing_client.get("/api/complete", params={"q": "this is"})
        assert response.status_code == 503
        assert response.json()["detail"]["status"] == "preparing"

    def test_searching_after_a_failure_is_refused_with_a_state(self, failed_client):
        response = failed_client.get("/api/complete", params={"q": "this is"})
        assert response.status_code == 503
        assert response.json()["detail"]["status"] == "failed"

    def test_a_failure_message_names_the_setting_to_fix(self, failed_client):
        detail = failed_client.get("/api/complete", params={"q": "x"}).json()["detail"]
        assert "config.yaml" in detail["message"]
        assert "CorpusNotFoundError" not in detail["message"]


class TestIndexLifecycle:
    def test_the_index_is_prepared_once_however_many_requests_arrive(self, client, index):
        """The prepared index is shared, not rebuilt: every request must see the
        same object."""
        seen = set()
        for _ in range(25):
            client.get("/api/complete", params={"q": "this is"})
            seen.add(id(client.app.state.engine.index))
        assert seen == {id(index)}

    def test_preparation_starts_only_once(self, tmp_path):
        root = write_corpus(tmp_path / "corpus", {"a.txt": DEMO})
        config = Config(corpus_root=root, cache_dir=tmp_path / "cache")
        state = EngineState()

        state.prepare(config)
        first = state._started
        state.prepare(config)

        assert first is True
        assert state._started is True

    def test_a_prepared_index_is_reported_ready(self, tmp_path):
        root = write_corpus(tmp_path / "corpus", {"a.txt": DEMO})
        config = Config(corpus_root=root, cache_dir=tmp_path / "cache")
        state = EngineState()
        state.prepare(config)

        deadline = 60
        while state.status == "preparing" and deadline:
            import time

            time.sleep(0.1)
            deadline -= 1

        assert state.status == "ready", state.error
        assert state.ready is True

    def test_a_missing_corpus_is_recorded_rather_than_raised(self, tmp_path):
        config = Config(corpus_root=tmp_path / "absent", cache_dir=tmp_path / "cache")
        state = EngineState()
        state.prepare(config)

        deadline = 60
        while state.status == "preparing" and deadline:
            import time

            time.sleep(0.1)
            deadline -= 1

        assert state.status == "failed"
        assert "CorpusNotFound" in (state.error or "")

    def test_requests_never_see_a_half_prepared_index(self):
        """There is no state between "nothing" and "finished": the index is
        published by one assignment of a completed object."""
        state = EngineState()
        assert state.index is None and not state.ready
        state.index = object()
        assert state.ready


class TestBoundaries:
    def test_cors_is_limited_to_the_development_frontend(self, client):
        allowed = client.get(
            "/api/health", headers={"Origin": "http://localhost:5173"}
        )
        assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_another_origin_is_not_granted_access(self, client):
        response = client.get(
            "/api/health", headers={"Origin": "http://evil.example.com"}
        )
        assert "access-control-allow-origin" not in response.headers

    def test_no_endpoint_takes_a_filesystem_path(self, client):
        """Every parameter any endpoint accepts is one of a known few, and none
        of them is free-form text that could become a path.

        The search endpoints take only ``q`` and ``limit``. The Drive import
        endpoints add two path parameters and one header; the path parameters
        are constrained to bounded lowercase hex in the schema itself, which is
        what makes "no endpoint takes a path" checkable rather than asserted.
        """
        schema = client.get("/openapi.json").json()
        parameters = {
            parameter["name"]: parameter
            for path in schema["paths"].values()
            for operation in path.values()
            for parameter in operation.get("parameters", [])
        }
        assert set(parameters) == {
            "q",
            "limit",
            "job_id",
            "document_id",
            "X-Drive-Access-Token",
        }
        for name in ("job_id", "document_id"):
            assert parameters[name]["schema"]["pattern"] == r"^[0-9a-f]+$"
            assert parameters[name]["schema"]["maxLength"] == 64

    def test_an_unknown_route_is_a_plain_not_found(self, client):
        response = client.get("/api/whatever")
        assert response.status_code == 404
        assert "Traceback" not in response.text
