"""Tests for preparing an index while reporting what is happening.

Every one of these builds its own corpus and its own cache under ``tmp_path``.
Nothing here reads, writes, invalidates or rebuilds the cache the developer
running the tests is using.

What is checked is that the reported progress describes the work that actually
happened: the phases a route really runs, counts that match what was read, and
paths that are relative to the corpus root and to nothing else.
"""

from __future__ import annotations

import pytest

from autocomplete.cache import POINTER_FILE, planned_mode
from autocomplete.config import Config
from autocomplete.preparation import PreparationFailure, describe_failure, prepare
from autocomplete.progress import (
    BuildPhase,
    BuildState,
    CacheMode,
    ProgressTracker,
)

SENTENCES = 40


def write_corpus(root, files: dict[str, str]):
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def small_corpus(root, count: int = 4):
    return write_corpus(
        root,
        {
            f"file{n}.txt": "\n".join(
                f"the quick brown fox number {n} line {i}" for i in range(SENTENCES)
            )
            + "\n"
            for n in range(count)
        },
    )


@pytest.fixture
def workspace(tmp_path):
    """A corpus and an empty cache, both temporary."""
    corpus = small_corpus(tmp_path / "corpus")
    return Config(
        corpus_root=corpus,
        cache_dir=tmp_path / "cache",
        num_results=5,
        use_mmap=False,
    )


def first_reported_mode(tracker: ProgressTracker) -> CacheMode:
    """The route reported once preparation began.

    Not ``since(0)[0]``: that is the snapshot a tracker publishes when it is
    constructed, before anything has been asked of it, and it is idle by design.
    """
    for snapshot in tracker.since(0):
        if snapshot.state is BuildState.PREPARING:
            return snapshot.cache_mode
    raise AssertionError("no preparing snapshot was published")


def phases_of(tracker: ProgressTracker) -> list[BuildPhase]:
    """The phases that were entered, in order, without repeats."""
    order: list[BuildPhase] = []
    for snapshot in tracker.since(0):
        if not order or order[-1] is not snapshot.phase:
            order.append(snapshot.phase)
    return order


class TestColdBuild:
    def test_it_runs_the_phases_a_first_build_really_performs(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)

        assert phases_of(tracker) == [
            BuildPhase.LOADING_CONFIGURATION,
            BuildPhase.VERIFYING_SUFFIX_BUILDER,
            BuildPhase.VALIDATING_CORPUS,
            BuildPhase.DISCOVERING_CORPUS,
            BuildPhase.READING_FILES,
            BuildPhase.NORMALIZING_RECORDS,
            BuildPhase.BUILDING_SUFFIX_ARRAY,
            BuildPhase.BUILDING_BLOCK_SUMMARIES,
            BuildPhase.WRITING_ARTIFACTS,
            BuildPhase.CHECKSUMMING_ARTIFACTS,
            BuildPhase.PUBLISHING_GENERATION,
            BuildPhase.READY,
        ]

    def test_it_reports_a_cold_build(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        assert first_reported_mode(tracker) is CacheMode.COLD_BUILD

    def test_it_ends_ready_with_the_index_it_built(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        index = prepare(workspace, tracker)
        snapshot = tracker.snapshot()
        assert snapshot.state is BuildState.READY
        assert snapshot.index is not None
        assert snapshot.index.sentences == len(index) == 4 * SENTENCES
        assert snapshot.index.files == 4

    def test_the_counts_match_what_was_actually_read(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        reading = [s for s in tracker.since(0) if s.phase is BuildPhase.READING_FILES]
        assert reading[-1].files_done == 4
        assert reading[-1].files_total == 4
        assert reading[-1].sentences == 4 * SENTENCES

        on_disk = sum(
            path.stat().st_size for path in workspace.corpus_root.rglob("*.txt")
        )
        assert reading[-1].bytes_done == on_disk
        assert reading[-1].bytes_total == on_disk

    def test_reading_is_determinate_and_block_summaries_are_too(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        by_phase = {s.phase: s for s in tracker.since(0)}
        assert by_phase[BuildPhase.READING_FILES].determinate is True
        assert by_phase[BuildPhase.BUILDING_BLOCK_SUMMARIES].determinate is True

    def test_the_suffix_array_is_honestly_indeterminate(self, workspace):
        """It is one call into a C library. Nothing can report its progress, so
        nothing pretends to."""
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        building = [
            s for s in tracker.since(0) if s.phase is BuildPhase.BUILDING_SUFFIX_ARRAY
        ]
        assert building
        assert all(s.determinate is False for s in building)
        assert all(s.total is None for s in building)
        # What it can honestly say is how much text it is ordering.
        assert building[-1].bytes_total > 0

    def test_discovery_is_indeterminate_until_the_count_is_known(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        discovery = [
            s for s in tracker.since(0) if s.phase is BuildPhase.DISCOVERING_CORPUS
        ]
        assert discovery
        assert all(s.determinate is False for s in discovery)

    def test_every_phase_that_ran_is_recorded_as_completed(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        completed = {item.phase for item in tracker.snapshot().completed_phases}
        assert BuildPhase.READING_FILES in completed
        assert BuildPhase.PUBLISHING_GENERATION in completed
        assert all(item.seconds >= 0 for item in tracker.snapshot().completed_phases)


class TestWarmStart:
    def test_a_second_run_validates_and_loads_rather_than_building(self, workspace):
        prepare(workspace, ProgressTracker(throttle_seconds=0.0))

        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        assert phases_of(tracker) == [
            BuildPhase.LOADING_CONFIGURATION,
            BuildPhase.VERIFYING_SUFFIX_BUILDER,
            BuildPhase.VALIDATING_CORPUS,
            BuildPhase.VALIDATING_ARTIFACTS,
            BuildPhase.LOADING_ARTIFACTS,
            BuildPhase.READY,
        ]

    def test_it_ends_reported_as_a_warm_load(self, workspace):
        prepare(workspace, ProgressTracker(throttle_seconds=0.0))
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        assert tracker.snapshot().cache_mode is CacheMode.WARM_LOAD

    def test_it_starts_reported_as_a_warm_validation(self, workspace):
        prepare(workspace, ProgressTracker(throttle_seconds=0.0))
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        assert first_reported_mode(tracker) is CacheMode.WARM_VALIDATION

    def test_artifact_validation_is_determinate(self, workspace):
        prepare(workspace, ProgressTracker(throttle_seconds=0.0))
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        validating = [
            s for s in tracker.since(0) if s.phase is BuildPhase.VALIDATING_ARTIFACTS
        ]
        assert validating
        assert validating[-1].determinate is True
        assert validating[-1].current == validating[-1].total

    def test_a_structural_level_skips_fingerprinting_the_corpus(self, tmp_path):
        corpus = small_corpus(tmp_path / "corpus")
        config = Config(
            corpus_root=corpus,
            cache_dir=tmp_path / "cache",
            num_results=5,
            use_mmap=False,
            validation_level="structural",
        )
        prepare(config, ProgressTracker(throttle_seconds=0.0))

        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(config, tracker)
        assert BuildPhase.VALIDATING_CORPUS not in phases_of(tracker)


class TestRebuildAndRecovery:
    def test_a_forced_rebuild_is_reported_as_one(self, workspace):
        prepare(workspace, ProgressTracker(throttle_seconds=0.0))
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker, force_rebuild=True)
        assert first_reported_mode(tracker) is CacheMode.FORCED_REBUILD
        assert BuildPhase.READING_FILES in phases_of(tracker)

    def test_a_damaged_cache_is_reported_as_a_recovery_and_rebuilt(self, workspace):
        prepare(workspace, ProgressTracker(throttle_seconds=0.0))
        generation = (
            workspace.cache_dir
            / (workspace.cache_dir / POINTER_FILE).read_text(encoding="utf-8").strip()
        )
        (generation / "manifest.json").write_text("{ broken", encoding="utf-8")

        tracker = ProgressTracker(throttle_seconds=0.0)
        index = prepare(workspace, tracker)

        assert tracker.snapshot().state is BuildState.READY
        assert len(index) == 4 * SENTENCES
        modes = [s.cache_mode for s in tracker.since(0)]
        assert CacheMode.RECOVERY in modes
        assert BuildPhase.READING_FILES in phases_of(tracker)

    def test_a_changed_corpus_rebuilds(self, workspace):
        prepare(workspace, ProgressTracker(throttle_seconds=0.0))
        (workspace.corpus_root / "file0.txt").write_text("changed now\n", encoding="utf-8")

        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        assert BuildPhase.READING_FILES in phases_of(tracker)
        assert tracker.snapshot().state is BuildState.READY

    def test_planned_mode_reads_only_whether_a_pointer_exists(self, workspace):
        assert planned_mode(workspace) is CacheMode.COLD_BUILD
        prepare(workspace, ProgressTracker(throttle_seconds=0.0))
        assert planned_mode(workspace) is CacheMode.WARM_VALIDATION
        assert planned_mode(workspace, force_rebuild=True) is CacheMode.FORCED_REBUILD


class TestPathsThatReachAWatcher:
    def test_the_reported_file_is_relative_to_the_corpus_root(self, tmp_path):
        corpus = write_corpus(
            tmp_path / "corpus",
            {"top.txt": "a line\n", "deep/inner/nested.txt": "another line\n"},
        )
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(config, tracker)

        seen = {s.current_file for s in tracker.since(0) if s.current_file}
        assert "top.txt" in seen
        assert "deep/inner/nested.txt" in seen

    def test_no_absolute_path_ever_appears_in_any_snapshot(self, tmp_path):
        corpus = small_corpus(tmp_path / "corpus")
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(config, tracker)

        forbidden = [str(tmp_path), str(corpus), str(config.cache_dir), str(Path_home())]
        for snapshot in tracker.since(0):
            blob = repr(snapshot)
            for secret in forbidden:
                assert secret not in blob, snapshot.phase

    def test_a_unicode_filename_survives_intact(self, tmp_path):
        corpus = write_corpus(
            tmp_path / "corpus", {"מסמך עברית.txt": "a line of text\n"}
        )
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(config, tracker)
        assert "מסמך עברית.txt" in {
            s.current_file for s in tracker.since(0) if s.current_file
        }

    def test_a_very_long_relative_path_is_reported_whole(self, tmp_path):
        deep = "/".join(f"level{n}" for n in range(12)) + "/" + "n" * 80 + ".txt"
        corpus = write_corpus(tmp_path / "corpus", {deep: "a line\n"})
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(config, tracker)
        assert deep in {s.current_file for s in tracker.since(0) if s.current_file}

    def test_a_filename_full_of_awkward_characters_is_passed_through_as_data(
        self, tmp_path
    ):
        awkward = "<script>alert(1)</script> & 'quotes' \"here\".txt"
        corpus = write_corpus(tmp_path / "corpus", {awkward: "a line\n"})
        config = Config(
            corpus_root=corpus, cache_dir=tmp_path / "cache", num_results=5, use_mmap=False
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(config, tracker)
        # Passed through unchanged: escaping is the renderer's job, and doing it
        # here would corrupt the name for every other consumer.
        assert awkward in {s.current_file for s in tracker.since(0) if s.current_file}

    def test_only_artifact_names_are_reported_while_publishing(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        during = {
            s.current_file
            for s in tracker.since(0)
            if s.phase is BuildPhase.CHECKSUMMING_ARTIFACTS and s.current_file
        }
        assert during
        assert all("/" not in name and "gen-" not in name for name in during)


def Path_home():
    from pathlib import Path

    return Path.home()


class TestFailures:
    def test_a_missing_corpus_fails_safely(self, tmp_path):
        config = Config(
            corpus_root=tmp_path / "absent",
            cache_dir=tmp_path / "cache",
            num_results=5,
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        with pytest.raises(PreparationFailure) as raised:
            prepare(config, tracker)

        assert raised.value.code == "corpus_missing"
        snapshot = tracker.snapshot()
        assert snapshot.state is BuildState.FAILED
        assert snapshot.error_code == "corpus_missing"
        assert "corpus_root" in (snapshot.recovery_hint or "")

    def test_a_failure_message_never_carries_the_path_it_looked_at(self, tmp_path):
        config = Config(
            corpus_root=tmp_path / "absent-corpus-name",
            cache_dir=tmp_path / "cache",
            num_results=5,
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        with pytest.raises(PreparationFailure):
            prepare(config, tracker)
        snapshot = tracker.snapshot()
        text = f"{snapshot.error_message} {snapshot.recovery_hint} {snapshot.detail}"
        assert "absent-corpus-name" not in text
        assert str(tmp_path) not in text

    def test_a_broken_suffix_builder_is_reported_actionably(self, workspace, monkeypatch):
        from autocomplete.suffix_index import SuffixIndexError

        monkeypatch.setattr(
            "autocomplete.preparation.verify_builder",
            lambda: (_ for _ in ()).throw(SuffixIndexError("pydivsufsort is broken")),
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        with pytest.raises(PreparationFailure):
            prepare(workspace, tracker)

        snapshot = tracker.snapshot()
        assert snapshot.error_code == "suffix_builder_unavailable"
        assert "requirements.txt" in (snapshot.recovery_hint or "")
        assert snapshot.phase is BuildPhase.VERIFYING_SUFFIX_BUILDER

    def test_the_builder_is_checked_before_the_corpus_is_read(self, workspace, monkeypatch):
        from autocomplete.suffix_index import SuffixIndexError

        monkeypatch.setattr(
            "autocomplete.preparation.verify_builder",
            lambda: (_ for _ in ()).throw(SuffixIndexError("broken")),
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        with pytest.raises(PreparationFailure):
            prepare(workspace, tracker)
        assert BuildPhase.READING_FILES not in phases_of(tracker)

    def test_a_failure_part_way_through_keeps_what_completed(self, workspace, monkeypatch):
        monkeypatch.setattr(
            "autocomplete.index.SuffixIndex.build",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(MemoryError())),
        )
        tracker = ProgressTracker(throttle_seconds=0.0)
        with pytest.raises(PreparationFailure):
            prepare(workspace, tracker)

        snapshot = tracker.snapshot()
        assert snapshot.error_code == "out_of_memory"
        completed = {item.phase for item in snapshot.completed_phases}
        assert BuildPhase.READING_FILES in completed

    def test_a_failed_build_publishes_nothing(self, workspace, monkeypatch):
        monkeypatch.setattr(
            "autocomplete.index.SuffixIndex.build",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))),
        )
        with pytest.raises(PreparationFailure):
            prepare(workspace, ProgressTracker(throttle_seconds=0.0))
        assert not (workspace.cache_dir / POINTER_FILE).exists()

    @pytest.mark.parametrize(
        "error,code",
        [
            (PermissionError(13, "denied"), "permission_denied"),
            (OSError(28, "No space left on device"), "disk_full"),
            (OSError(5, "I/O error"), "storage_error"),
            (MemoryError(), "out_of_memory"),
            (RuntimeError("anything at all"), "internal_error"),
        ],
    )
    def test_every_failure_maps_to_a_stable_code(self, error, code):
        assert describe_failure(error)[0] == code

    def test_an_unrecognised_failure_does_not_repeat_its_message(self):
        code, message, hint = describe_failure(RuntimeError("/home/someone/secret"))
        assert code == "internal_error"
        assert "/home/someone/secret" not in message
        assert "RuntimeError" in message

    def test_every_failure_offers_something_to_do(self):
        for error in (
            PermissionError(13, "x"),
            OSError(28, "x"),
            MemoryError(),
            RuntimeError("x"),
        ):
            assert describe_failure(error)[2]


class TestProgressCannotBreakABuild:
    def test_a_sink_that_raises_does_not_abandon_a_valid_build(self, workspace):
        """Progress reporting is an accessory. A watcher that throws must not be
        able to lose a build that was going to succeed."""

        class Hostile(ProgressTracker):
            def update(self, **fields):
                raise RuntimeError("the watcher exploded")

            def begin(self, *args, **kwargs):
                raise RuntimeError("the watcher exploded")

        index = prepare(workspace, Hostile(throttle_seconds=0.0))
        assert len(index) == 4 * SENTENCES
        assert (workspace.cache_dir / POINTER_FILE).is_file()

    def test_preparation_without_a_tracker_still_works(self, workspace):
        index = prepare(workspace)
        assert len(index) == 4 * SENTENCES


class TestTheRealCacheIsNeverTouched:
    def test_these_tests_only_use_temporary_directories(self, workspace, tmp_path):
        """The guard that matters most: everything above writes under tmp_path,
        so a developer's own prepared index is never rebuilt by a test run."""
        assert str(workspace.cache_dir).startswith(str(tmp_path))
        assert str(workspace.corpus_root).startswith(str(tmp_path))


class TestTheSnapshotBeforeAnythingStarts:
    def test_a_tracker_reports_idle_before_it_is_asked_for_anything(self):
        """There is always something to answer with, so a browser connecting
        before preparation begins is not left with nothing."""
        tracker = ProgressTracker(throttle_seconds=0.0)
        snapshot = tracker.snapshot()
        assert snapshot.state is BuildState.IDLE
        assert snapshot.cache_mode is CacheMode.UNKNOWN
        assert snapshot.index is None
        assert snapshot.error_code is None


class TestCountersMeanOneThing:
    """The file, sentence and byte counters describe corpus *ingestion*.

    Fingerprinting reads every file too, but reading a file to hash it is not
    reading it into the index. Reporting both through the same counters made
    the interface show the whole corpus as read before a single sentence had
    been collected.
    """

    def test_ingestion_counters_start_from_zero_when_reading_starts(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)

        reading = [s for s in tracker.since(0) if s.phase is BuildPhase.READING_FILES]
        assert reading[0].files_done < 4, "the fingerprint's count leaked into reading"
        assert reading[-1].files_done == 4

    def test_files_read_never_exceeds_the_files_there_are(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        for snapshot in tracker.since(0):
            if snapshot.files_total is not None:
                assert snapshot.files_done <= snapshot.files_total, snapshot.phase

    def test_fingerprinting_reports_its_own_bar_and_not_the_counters(self, workspace):
        tracker = ProgressTracker(throttle_seconds=0.0)
        prepare(workspace, tracker)
        hashing = [
            s for s in tracker.since(0) if s.phase is BuildPhase.VALIDATING_CORPUS
        ]
        assert hashing[-1].total == 4
        assert hashing[-1].current == 4
        assert hashing[-1].current_file is not None
        # It reports where it has got to, without claiming anything is indexed.
        assert hashing[-1].sentences == 0
        assert hashing[-1].files_done == 0
