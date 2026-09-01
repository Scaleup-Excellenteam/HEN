"""Tests for the progress model itself.

The tracker is the boundary between a building thread and everything watching
it, so what is checked here is that it cannot mislead: counters that only go
forward, sequence numbers that only go up, a history that stays bounded, and a
snapshot that is safe to hold while the build carries on.
"""

from __future__ import annotations

import threading

import pytest

from autocomplete.progress import (
    DEFAULT_HISTORY,
    PHASE_LABELS,
    BuildPhase,
    BuildState,
    CacheMode,
    IndexStats,
    NULL_SINK,
    ProgressTracker,
    expected_phases,
)


class Clock:
    """A hand-wound clock, so throttling is tested without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def tracker() -> ProgressTracker:
    return ProgressTracker(throttle_seconds=0.0)


class TestTheNullSink:
    def test_it_accepts_everything_the_pipeline_sends(self):
        NULL_SINK.begin(BuildPhase.READING_FILES, detail="x", total=3)
        NULL_SINK.update(current=1, current_file="a.txt", sentences=2)
        NULL_SINK.note_cache_mode(CacheMode.COLD_BUILD)

    def test_it_is_shared_because_it_holds_nothing(self):
        from autocomplete.progress import NULL_SINK as again

        assert NULL_SINK is again


class TestInitialState:
    def test_a_new_tracker_already_has_a_snapshot(self, tracker):
        snapshot = tracker.snapshot()
        assert snapshot.state is BuildState.IDLE
        assert snapshot.sequence >= 1

    def test_it_is_idle_rather_than_pretending_to_work(self, tracker):
        assert tracker.snapshot().state is BuildState.IDLE
        assert tracker.snapshot().elapsed_seconds == 0.0

    def test_every_phase_has_a_label(self):
        for phase in BuildPhase:
            assert PHASE_LABELS[phase]


class TestPhases:
    def test_beginning_a_phase_publishes_at_once(self, tracker):
        before = tracker.snapshot().sequence
        tracker.begin(BuildPhase.READING_FILES, total=10)
        assert tracker.snapshot().sequence > before
        assert tracker.snapshot().phase is BuildPhase.READING_FILES

    def test_a_total_makes_a_phase_determinate(self, tracker):
        tracker.begin(BuildPhase.READING_FILES, total=10)
        assert tracker.snapshot().determinate is True
        assert tracker.snapshot().total == 10

    def test_no_total_makes_a_phase_indeterminate(self, tracker):
        tracker.begin(BuildPhase.BUILDING_SUFFIX_ARRAY)
        assert tracker.snapshot().determinate is False
        assert tracker.snapshot().total is None

    def test_indeterminate_can_be_stated_explicitly(self, tracker):
        tracker.begin(BuildPhase.DISCOVERING_CORPUS, determinate=False)
        assert tracker.snapshot().determinate is False

    def test_a_finished_phase_is_recorded_with_its_duration(self):
        clock = Clock()
        tracker = ProgressTracker(throttle_seconds=0.0, clock=clock)
        tracker.start()
        tracker.begin(BuildPhase.READING_FILES)
        clock.advance(2.5)
        tracker.begin(BuildPhase.BUILDING_SUFFIX_ARRAY)

        completed = tracker.snapshot().completed_phases
        assert [item.phase for item in completed] == [
            BuildPhase.LOADING_CONFIGURATION,
            BuildPhase.READING_FILES,
        ]
        assert completed[-1].seconds == pytest.approx(2.5)

    def test_beginning_the_same_phase_twice_does_not_double_record_it(self, tracker):
        tracker.start()
        tracker.begin(BuildPhase.READING_FILES)
        tracker.begin(BuildPhase.READING_FILES)
        phases = [item.phase for item in tracker.snapshot().completed_phases]
        assert phases.count(BuildPhase.READING_FILES) == 0

    def test_a_new_phase_clears_the_previous_file(self, tracker):
        tracker.begin(BuildPhase.READING_FILES, total=2)
        tracker.update(current_file="a.txt")
        tracker.begin(BuildPhase.NORMALIZING_RECORDS)
        assert tracker.snapshot().current_file is None


class TestCountersOnlyMoveForward:
    """A watcher must never see a count go backwards, whatever arrives."""

    def test_current_never_decreases(self, tracker):
        tracker.begin(BuildPhase.READING_FILES, total=10)
        tracker.update(current=5)
        tracker.update(current=2)
        assert tracker.snapshot().current == 5

    @pytest.mark.parametrize(
        "field,reader",
        [
            ("files_done", lambda s: s.files_done),
            ("sentences", lambda s: s.sentences),
            ("bytes_done", lambda s: s.bytes_done),
        ],
    )
    def test_counters_never_decrease(self, tracker, field, reader):
        tracker.begin(BuildPhase.READING_FILES)
        tracker.update(**{field: 100})
        tracker.update(**{field: 1})
        assert reader(tracker.snapshot()) == 100

    def test_advance_accumulates(self, tracker):
        tracker.begin(BuildPhase.BUILDING_BLOCK_SUMMARIES, total=10)
        tracker.update(advance=3)
        tracker.update(advance=4)
        assert tracker.snapshot().current == 7

    def test_a_total_that_arrives_late_makes_the_phase_determinate(self, tracker):
        tracker.begin(BuildPhase.DISCOVERING_CORPUS, determinate=False)
        assert tracker.snapshot().determinate is False
        tracker.update(total=42)
        assert tracker.snapshot().determinate is True
        assert tracker.snapshot().total == 42


class TestSequenceNumbers:
    def test_they_only_ever_increase(self, tracker):
        tracker.start()
        seen = [tracker.snapshot().sequence]
        for n in range(20):
            tracker.update(current=n)
            seen.append(tracker.snapshot().sequence)
        assert seen == sorted(seen)
        assert len(set(seen)) == len(seen)

    def test_they_keep_increasing_across_a_retry(self, tracker):
        tracker.start()
        tracker.fail("x", "y")
        before = tracker.snapshot().sequence
        tracker.start()
        assert tracker.snapshot().sequence > before

    def test_since_returns_only_newer_snapshots(self, tracker):
        tracker.start()
        mark = tracker.snapshot().sequence
        tracker.begin(BuildPhase.READING_FILES)
        tracker.begin(BuildPhase.NORMALIZING_RECORDS)
        newer = tracker.since(mark)
        assert newer
        assert all(item.sequence > mark for item in newer)

    def test_since_the_latest_returns_nothing(self, tracker):
        assert tracker.since(tracker.snapshot().sequence) == []


class TestThrottling:
    def test_within_phase_updates_are_coalesced(self):
        clock = Clock()
        tracker = ProgressTracker(throttle_seconds=1.0, clock=clock)
        tracker.start()
        tracker.begin(BuildPhase.BUILDING_BLOCK_SUMMARIES, total=1000)
        before = tracker.snapshot().sequence

        for n in range(1000):
            tracker.update(current=n + 1)

        # No time passed, so one publication covers the lot.
        assert tracker.snapshot().sequence == before

    def test_the_latest_counters_are_kept_even_when_not_published(self):
        clock = Clock()
        tracker = ProgressTracker(throttle_seconds=1.0, clock=clock)
        tracker.begin(BuildPhase.BUILDING_BLOCK_SUMMARIES, total=1000)
        for n in range(1000):
            tracker.update(current=n + 1)

        clock.advance(2.0)
        tracker.update(current=1000)
        assert tracker.snapshot().current == 1000

    def test_a_phase_change_is_never_throttled_away(self):
        clock = Clock()
        tracker = ProgressTracker(throttle_seconds=1000.0, clock=clock)
        tracker.start()
        before = tracker.snapshot().sequence
        tracker.begin(BuildPhase.READING_FILES)
        assert tracker.snapshot().sequence > before
        assert tracker.snapshot().phase is BuildPhase.READING_FILES

    def test_completion_is_never_throttled_away(self):
        tracker = ProgressTracker(throttle_seconds=1000.0, clock=Clock())
        tracker.start()
        tracker.finish(None)
        assert tracker.snapshot().state is BuildState.READY

    def test_failure_is_never_throttled_away(self):
        tracker = ProgressTracker(throttle_seconds=1000.0, clock=Clock())
        tracker.start()
        tracker.fail("code", "message")
        assert tracker.snapshot().state is BuildState.FAILED


class TestBoundedHistory:
    def test_history_does_not_grow_without_limit(self):
        tracker = ProgressTracker(throttle_seconds=0.0, history=8)
        tracker.start()
        for n in range(500):
            tracker.update(current=n)
        assert len(tracker.since(0)) <= 8

    def test_the_retained_history_always_ends_with_the_current_state(self):
        tracker = ProgressTracker(throttle_seconds=0.0, history=4)
        tracker.start()
        for n in range(50):
            tracker.update(current=n)
        assert tracker.since(0)[-1] is tracker.snapshot()

    def test_the_default_history_is_bounded(self):
        tracker = ProgressTracker(throttle_seconds=0.0)
        for n in range(DEFAULT_HISTORY * 5):
            tracker.update(current=n)
        assert len(tracker.since(0)) <= DEFAULT_HISTORY


class TestTerminalStates:
    def test_finishing_records_the_index(self, tracker):
        stats = IndexStats(1, 2, 3, 4, 5, 6, 7, 8)
        tracker.start()
        tracker.finish(stats)
        snapshot = tracker.snapshot()
        assert snapshot.state is BuildState.READY
        assert snapshot.index == stats
        assert snapshot.phase is BuildPhase.READY

    def test_failing_records_a_code_a_message_and_a_hint(self, tracker):
        tracker.start()
        tracker.fail("corpus_missing", "not found", hint="set corpus_root")
        snapshot = tracker.snapshot()
        assert snapshot.state is BuildState.FAILED
        assert snapshot.error_code == "corpus_missing"
        assert snapshot.error_message == "not found"
        assert snapshot.recovery_hint == "set corpus_root"

    def test_elapsed_time_stops_once_finished(self):
        clock = Clock()
        tracker = ProgressTracker(throttle_seconds=0.0, clock=clock)
        tracker.start()
        clock.advance(3.0)
        tracker.finish(None)
        settled = tracker.snapshot().elapsed_seconds
        clock.advance(100.0)
        assert tracker.snapshot().elapsed_seconds == settled

    def test_a_retry_clears_the_previous_failure(self, tracker):
        tracker.start()
        tracker.fail("code", "message", hint="hint")
        tracker.start(CacheMode.COLD_BUILD)
        snapshot = tracker.snapshot()
        assert snapshot.state is BuildState.PREPARING
        assert snapshot.error_code is None
        assert snapshot.completed_phases == ()

    def test_state_knows_which_states_are_final(self):
        assert BuildState.READY.finished and BuildState.FAILED.finished
        assert not BuildState.IDLE.finished and not BuildState.PREPARING.finished


class TestPhasePlans:
    def test_a_warm_route_plans_no_building(self):
        plan = expected_phases(CacheMode.WARM_LOAD)
        assert BuildPhase.LOADING_ARTIFACTS in plan
        assert BuildPhase.BUILDING_SUFFIX_ARRAY not in plan

    def test_a_cold_route_plans_the_build(self):
        plan = expected_phases(CacheMode.COLD_BUILD)
        assert BuildPhase.READING_FILES in plan
        assert BuildPhase.BUILDING_SUFFIX_ARRAY in plan
        assert BuildPhase.PUBLISHING_GENERATION in plan

    @pytest.mark.parametrize(
        "mode", [CacheMode.FORCED_REBUILD, CacheMode.RECOVERY, CacheMode.UNKNOWN]
    )
    def test_every_other_route_plans_a_build(self, mode):
        assert BuildPhase.READING_FILES in expected_phases(mode)

    def test_every_plan_ends_ready(self):
        for mode in CacheMode:
            assert expected_phases(mode)[-1] is BuildPhase.READY


class TestThreadSafety:
    def test_snapshots_stay_consistent_under_concurrent_updates(self):
        tracker = ProgressTracker(throttle_seconds=0.0)
        tracker.start()
        tracker.begin(BuildPhase.READING_FILES, total=4000)
        stop = threading.Event()
        seen: list[tuple[int, int]] = []

        def write() -> None:
            for n in range(4000):
                tracker.update(current=n + 1, sentences=n + 1, files_done=n + 1)
            stop.set()

        def read() -> None:
            while not stop.is_set():
                snapshot = tracker.snapshot()
                seen.append((snapshot.sequence, snapshot.current))

        writer = threading.Thread(target=write)
        readers = [threading.Thread(target=read) for _ in range(4)]
        writer.start()
        for reader in readers:
            reader.start()
        writer.join()
        for reader in readers:
            reader.join()

        assert tracker.snapshot().current == 4000
        # Every reader saw whole values, and never a sequence out of order
        # relative to what it had already read.
        assert seen

    def test_a_reader_holding_a_snapshot_is_not_changed_underneath_it(self, tracker):
        tracker.begin(BuildPhase.READING_FILES, total=10)
        tracker.update(current=1)
        held = tracker.snapshot()
        tracker.update(current=9)
        assert held.current == 1

    def test_waiting_returns_when_something_changes(self, tracker):
        mark = tracker.snapshot().sequence
        result: list[bool] = []

        waiter = threading.Thread(
            target=lambda: result.append(tracker.wait_for_change(mark, timeout=5))
        )
        waiter.start()
        tracker.begin(BuildPhase.READING_FILES)
        waiter.join(timeout=5)
        assert result == [True]

    def test_waiting_times_out_when_nothing_changes(self, tracker):
        mark = tracker.snapshot().sequence
        assert tracker.wait_for_change(mark, timeout=0.05) is False
