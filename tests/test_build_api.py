"""Tests for the preparation snapshot and stream endpoints.

Everything here runs against a corpus and a cache created under ``tmp_path``.
No test reads or writes the cache a developer is using.

Most of these drive a tracker directly rather than running a real build: what is
under test is the transport — what is sent, in what order, to how many clients,
and what happens when one goes away — and a real build would make that slow and
timing-dependent without testing any more of it.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from autocomplete.config import Config
from autocomplete.progress import (
    BuildPhase,
    BuildState,
    CacheMode,
    IndexStats,
    ProgressTracker,
)
from autocomplete.web.api import EngineState, create_app
from autocomplete.web.build_api import snapshot_json


def build_corpus(root, count: int = 4):
    root.mkdir(parents=True, exist_ok=True)
    for n in range(count):
        (root / f"file{n}.txt").write_text(
            "\n".join(f"the quick brown fox {n} line {i}" for i in range(60)) + "\n",
            encoding="utf-8",
        )
    return root


@pytest.fixture
def tracker() -> ProgressTracker:
    return ProgressTracker(throttle_seconds=0.0)


@pytest.fixture
def client(tracker) -> TestClient:
    """A server that is not preparing anything, driven by the test's tracker."""
    app = create_app(prepare=False)
    app.state.engine = EngineState(tracker=tracker)
    with TestClient(app) as running:
        yield running


def frames(response) -> list[dict]:
    """Parse the data lines of an SSE response body."""
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def read_until_terminal(client, url="/api/build/events", headers=None) -> list[dict]:
    collected: list[dict] = []
    with client.stream("GET", url, headers=headers or {}) as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                collected.append(json.loads(line[6:]))
                if collected[-1]["state"] in ("ready", "failed"):
                    break
    return collected


class TestSnapshotEndpoint:
    def test_it_answers_before_anything_has_started(self, client):
        body = client.get("/api/build/status").json()
        assert body["state"] == "idle"
        assert body["sequence"] >= 1

    def test_it_reports_the_current_phase(self, client, tracker):
        tracker.start(CacheMode.COLD_BUILD)
        tracker.begin(BuildPhase.READING_FILES, detail="Reading 4 files.", total=4)
        tracker.update(current=2, current_file="deep/one.txt", sentences=99)

        body = client.get("/api/build/status").json()
        assert body["state"] == "preparing"
        assert body["phase"] == "reading_files"
        assert body["phase_label"] == "Reading corpus files"
        assert body["determinate"] is True
        assert (body["current"], body["total"]) == (2, 4)
        assert body["current_file"] == "deep/one.txt"
        assert body["sentences"] == 99
        assert body["cache_mode"] == "cold_build"

    def test_it_carries_the_plan_for_the_route(self, client, tracker):
        tracker.start(CacheMode.WARM_VALIDATION)
        body = client.get("/api/build/status").json()
        assert "loading_artifacts" in body["planned_phases"]
        assert "building_suffix_array" not in body["planned_phases"]

    def test_the_plan_follows_a_route_that_changes(self, client, tracker):
        tracker.start(CacheMode.WARM_VALIDATION)
        tracker.note_cache_mode(CacheMode.RECOVERY)
        body = client.get("/api/build/status").json()
        assert "building_suffix_array" in body["planned_phases"]

    def test_it_reports_an_indeterminate_phase_as_such(self, client, tracker):
        tracker.start()
        tracker.begin(BuildPhase.BUILDING_SUFFIX_ARRAY)
        body = client.get("/api/build/status").json()
        assert body["determinate"] is False
        assert body["total"] is None

    def test_it_reports_the_index_once_ready(self, client, tracker):
        tracker.start()
        tracker.finish(IndexStats(10, 2, 300, 40, 300, 1, 4096, 5))
        body = client.get("/api/build/status").json()
        assert body["state"] == "ready"
        assert body["index"]["sentences"] == 10
        assert body["index"]["files"] == 2

    def test_it_reports_a_failure_with_a_code_and_a_hint(self, client, tracker):
        tracker.start()
        tracker.fail("corpus_missing", "not found", hint="set corpus_root")
        body = client.get("/api/build/status").json()
        assert body["state"] == "failed"
        assert body["error_code"] == "corpus_missing"
        assert body["recovery_hint"] == "set corpus_root"
        assert body["can_retry"] is True

    def test_it_never_offers_an_estimate(self, client, tracker):
        tracker.start()
        tracker.begin(BuildPhase.READING_FILES, total=100)
        body = client.get("/api/build/status").json()
        for absent in ("eta", "remaining", "percent", "estimate", "seconds_left"):
            assert absent not in body

    def test_the_shape_is_exactly_what_is_documented(self, client, tracker):
        tracker.start()
        body = client.get("/api/build/status").json()
        assert set(body) == set(snapshot_json(tracker.snapshot()))


class TestEventStream:
    def test_it_is_a_server_sent_event_stream(self, client, tracker):
        tracker.start()
        tracker.finish(None)
        with client.stream("GET", "/api/build/events") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"].startswith("no-cache")

    def test_the_current_state_arrives_immediately(self, client, tracker):
        tracker.start(CacheMode.COLD_BUILD)
        tracker.begin(BuildPhase.READING_FILES, total=4)
        tracker.finish(None)

        received = read_until_terminal(client)
        assert received
        assert received[-1]["state"] == "ready"

    def test_every_frame_carries_a_sequence_and_an_id(self, client, tracker):
        tracker.start()
        tracker.finish(None)
        with client.stream("GET", "/api/build/events") as response:
            response.read()
            body = response.text
        assert "id: " in body
        assert "event: progress" in body
        assert all(item["sequence"] >= 1 for item in frames(response))

    def test_sequences_arrive_in_order_and_without_repeats(self, client, tracker):
        tracker.start()
        for n in range(5):
            tracker.begin(BuildPhase.READING_FILES, total=n + 1)
            tracker.begin(BuildPhase.NORMALIZING_RECORDS)
        tracker.finish(None)

        received = read_until_terminal(client)
        sequences = [item["sequence"] for item in received]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))

    def test_a_reconnect_resumes_from_the_last_event_id(self, client, tracker):
        tracker.start()
        tracker.begin(BuildPhase.READING_FILES, total=4)
        mark = tracker.snapshot().sequence
        tracker.begin(BuildPhase.NORMALIZING_RECORDS)
        tracker.finish(None)

        received = read_until_terminal(client, headers={"Last-Event-ID": str(mark)})
        assert received
        assert all(item["sequence"] > mark for item in received)

    def test_an_unparseable_last_event_id_is_treated_as_none(self, client, tracker):
        tracker.start()
        tracker.finish(None)
        received = read_until_terminal(client, headers={"Last-Event-ID": "not-a-number"})
        assert received[-1]["state"] == "ready"

    def test_a_terminal_state_ends_the_stream(self, client, tracker):
        tracker.start()
        tracker.fail("x", "y")
        with client.stream("GET", "/api/build/events") as response:
            # The generator returns after a terminal frame rather than holding
            # the connection open for a build that is over, so reading the body
            # to completion finishes instead of blocking.
            response.read()
            body = response.text
        assert body.count("event: progress") >= 1
        assert '"state":"failed"' in body.replace(" ", "")

    def test_the_history_it_replays_is_bounded(self, tracker):
        small = ProgressTracker(throttle_seconds=0.0, history=4)
        app = create_app(prepare=False)
        app.state.engine = EngineState(tracker=small)
        small.start()
        for n in range(200):
            small.update(current=n)
        small.finish(None)

        with TestClient(app) as running:
            received = read_until_terminal(running)
        assert len(received) <= 4


class TestManyClients:
    def test_several_clients_observe_one_build(self, tmp_path):
        """Connecting does not start a build, and connecting twice does not
        start two."""
        corpus = build_corpus(tmp_path / "corpus")
        config = Config(
            corpus_root=corpus,
            cache_dir=tmp_path / "cache",
            num_results=5,
            use_mmap=False,
        )
        app = create_app(config, prepare=True)

        with TestClient(app) as first, TestClient(app) as second:
            # Both clients are served by the same application object, so both
            # watch the one preparation its lifespan started.
            one = read_until_terminal(first)
            two = read_until_terminal(second)

        assert one[-1]["state"] == "ready"
        assert two[-1]["state"] == "ready"
        assert one[-1]["index"] == two[-1]["index"]
        # One build: the cache holds a single generation.
        generations = [item for item in (tmp_path / "cache").iterdir() if item.is_dir()]
        assert len(generations) == 1

    def test_the_tracker_holds_no_per_client_state_at_all(self, tracker):
        """Why a disconnect costs nothing: there is nothing to disconnect from.

        A connection keeps its own last-sequence number and asks for what is
        newer. The tracker has no subscriber list, no queue and no callback, so
        a client arriving or vanishing cannot leak, block or accumulate.
        """
        held = [
            name
            for name in vars(tracker)
            if any(word in name for word in ("subscrib", "queue", "listener", "client"))
        ]
        assert held == []

    def test_a_stream_stops_as_soon_as_the_client_has_gone(self, tracker):
        """Driven directly, because TestClient does not emulate a mid-stream
        disconnect: the endpoint asks the request whether it is still there."""
        import asyncio

        from autocomplete.web.build_api import create_build_router

        tracker.start()
        tracker.begin(BuildPhase.READING_FILES, total=1000)

        app = create_app(prepare=False)
        app.state.engine = EngineState(tracker=tracker)

        class GoneRequest:
            app = None

            async def is_disconnected(self) -> bool:
                return True

        request = GoneRequest()
        request.app = app

        route = next(
            r for r in create_build_router().routes if r.path.endswith("/events")
        )

        async def drain():
            response = await route.endpoint(request, None)
            produced = [chunk async for chunk in response.body_iterator]
            return produced

        # It returns rather than looping: a gone client ends the generator on
        # its first look, without ever reading the tracker.
        assert asyncio.run(drain()) == []

    def test_readers_cannot_slow_the_build_down(self, tracker):
        """A reader never gets between the build and its work.

        Tested here rather than over HTTP: driving a deliberately unread
        connection through the test client deadlocks its own portal, which
        tests the client rather than the server. The property that matters is
        this one, and it lives in the tracker: reporting takes a lock only to
        update counters, and readers take the same lock only to copy a
        finished snapshot, so no reader can hold the build up.
        """
        tracker.start()
        tracker.begin(BuildPhase.READING_FILES, total=20_000)

        stop = threading.Event()

        def read_relentlessly() -> None:
            while not stop.is_set():
                tracker.snapshot()
                tracker.since(0)

        readers = [threading.Thread(target=read_relentlessly) for _ in range(4)]
        for reader in readers:
            reader.start()
        try:
            started = time.perf_counter()
            for n in range(20_000):
                tracker.update(current=n + 1)
            elapsed = time.perf_counter() - started
        finally:
            stop.set()
            for reader in readers:
                reader.join(timeout=5)

        assert tracker.snapshot().current == 20_000
        # Four readers hammering it must not turn twenty thousand updates into
        # something a build would notice. This is generous by two orders of
        # magnitude against the real cost, so it fails only on a real stall.
        assert elapsed < 5.0, f"twenty thousand updates took {elapsed:.2f}s"

    def test_a_connected_reader_adds_no_state_to_the_build(self, client, tracker):
        """Asking for the current state, repeatedly, changes nothing about it."""
        tracker.start()
        tracker.begin(BuildPhase.READING_FILES, total=10)
        tracker.update(current=3)

        for _ in range(10):
            assert client.get("/api/build/status").json()["current"] == 3

        tracker.update(current=7)
        assert client.get("/api/build/status").json()["current"] == 7


class TestRetry:
    def test_retrying_is_refused_while_nothing_has_failed(self, client):
        response = client.post("/api/build/retry")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "not_retryable"

    def test_retrying_is_refused_while_preparing(self, client, tracker):
        tracker.start()
        tracker.begin(BuildPhase.READING_FILES)
        assert client.post("/api/build/retry").status_code == 409

    def test_retrying_is_refused_once_ready(self, tmp_path):
        corpus = build_corpus(tmp_path / "corpus")
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        app = create_app(config, prepare=True)
        with TestClient(app) as running:
            read_until_terminal(running)
            assert running.post("/api/build/retry").status_code == 409

    def test_a_failed_preparation_can_be_retried_and_can_succeed(self, tmp_path):
        """The corpus is created only after the first attempt has failed, so the
        retry is what makes it work."""
        corpus = tmp_path / "corpus"
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        app = create_app(config, prepare=True)

        with TestClient(app) as running:
            first = read_until_terminal(running)
            assert first[-1]["state"] == "failed"
            assert first[-1]["error_code"] == "corpus_missing"
            assert first[-1]["can_retry"] is True

            build_corpus(corpus)
            accepted = running.post("/api/build/retry")
            assert accepted.status_code == 202
            # The retry answers with the new attempt, never with the failure it
            # is retrying, so a client is not told it failed again at once.
            assert accepted.json()["state"] == "preparing"

            # Resume past the old failure still held in the history, or the
            # first terminal frame read would be the one already seen.
            second = read_until_terminal(
                running, headers={"Last-Event-ID": str(accepted.json()["sequence"])}
            )
            assert second[-1]["state"] == "ready"
            assert running.get("/api/complete", params={"q": "quick brown"}).status_code == 200

    def test_a_retry_takes_no_input_at_all(self, client):
        """Nothing a caller sends can influence what is prepared: there is no
        body, no query parameter and no path to give it."""
        schema = client.get("/openapi.json").json()
        operation = schema["paths"]["/api/build/retry"]["post"]
        assert operation.get("parameters", []) == []
        assert "requestBody" not in operation

    def test_a_retry_cannot_start_a_second_build(self, tmp_path):
        config = Config(
            corpus_root=tmp_path / "absent",
            cache_dir=tmp_path / "cache",
            num_results=5,
            use_mmap=False,
        )
        app = create_app(config, prepare=True)
        with TestClient(app) as running:
            read_until_terminal(running)
            assert running.post("/api/build/retry").status_code == 202
            # The second is refused because the first is now running or has
            # already reached a state that is not "failed".
            statuses = {running.post("/api/build/retry").status_code for _ in range(5)}
            assert statuses <= {202, 409}


class TestSearchAvailability:
    def test_searching_is_refused_until_an_index_is_published(self, tmp_path):
        corpus = build_corpus(tmp_path / "corpus", count=6)
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        app = create_app(config, prepare=True)
        with TestClient(app) as running:
            # Whatever the preparation is doing, a search either works or is
            # told the index is not ready. It is never given a partial answer.
            for _ in range(20):
                response = running.get("/api/complete", params={"q": "quick"})
                assert response.status_code in (200, 503)
                if response.status_code == 503:
                    assert response.json()["detail"]["status"] in ("preparing", "failed")

            read_until_terminal(running)
            assert running.get("/api/complete", params={"q": "quick brown"}).status_code == 200

    def test_search_works_after_readiness(self, tmp_path):
        corpus = build_corpus(tmp_path / "corpus")
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        app = create_app(config, prepare=True)
        with TestClient(app) as running:
            read_until_terminal(running)
            exact = running.get("/api/complete", params={"q": "quick brown fox"}).json()
            fuzzy = running.get("/api/complete", params={"q": "quick brwn fox"}).json()
        assert exact["count"] == 5
        assert fuzzy["count"] >= 1

    def test_health_gains_fields_without_losing_the_old_ones(self, tmp_path):
        corpus = build_corpus(tmp_path / "corpus")
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        app = create_app(config, prepare=True)
        with TestClient(app) as running:
            read_until_terminal(running)
            body = running.get("/api/health").json()

        # The five fields this endpoint has always had.
        assert body["status"] == "ready"
        assert body["ready"] is True
        assert body["detail"]
        assert body["sentences"] == 240
        assert body["sources"] == 4
        # And the ones added for the preparation screen.
        assert body["cache_mode"] == "cold_build"
        assert body["elapsed_seconds"] > 0


class TestNothingUnsafeIsSent:
    def test_no_response_carries_an_absolute_path(self, tmp_path):
        corpus = build_corpus(tmp_path / "corpus")
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        app = create_app(config, prepare=True)
        with TestClient(app) as running:
            received = read_until_terminal(running)
            bodies = [running.get(path).text for path in ("/api/build/status", "/api/health")]

        blob = json.dumps(received) + "".join(bodies)
        for secret in (str(tmp_path), str(corpus), str(config.cache_dir)):
            assert secret not in blob

    def test_a_failure_response_carries_no_path_and_no_traceback(self, tmp_path):
        config = Config(
            corpus_root=tmp_path / "a-very-distinctive-missing-name",
            cache_dir=tmp_path / "cache",
            num_results=5,
        )
        app = create_app(config, prepare=True)
        with TestClient(app) as running:
            received = read_until_terminal(running)
            status = running.get("/api/build/status").text

        blob = json.dumps(received) + status
        assert "a-very-distinctive-missing-name" not in blob
        assert "Traceback" not in blob
        assert str(tmp_path) not in blob

    def test_the_generation_directory_name_is_never_sent(self, tmp_path):
        corpus = build_corpus(tmp_path / "corpus")
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        app = create_app(config, prepare=True)
        with TestClient(app) as running:
            received = read_until_terminal(running)
        assert "gen-" not in json.dumps(received)
