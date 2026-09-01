"""Structured progress reporting for corpus preparation.

Preparing the index takes about seventeen seconds on the real corpus the first
time. That is long enough that somebody watching deserves to be told what is
happening, and the only honest way to tell them is to have the code doing the
work say so as it goes.

Two rules shape everything here.

**Nothing is invented.** Every number a caller can read came from work that
actually happened: a file that was opened, a sentence that was kept, a block that
was summarized. Where the underlying work cannot report its own progress — the
suffix array is built by one call into a C library — the phase says so by being
*indeterminate* rather than by moving a bar on a timer. There is no estimated
time remaining anywhere in this module, because nothing here can honestly compute
one.

**The pipeline stays independent.** This module imports nothing but the standard
library. The code that reads the corpus and builds the index takes a
:class:`ProgressSink` and calls it; it does not know whether anything is
listening, and :data:`NULL_SINK` makes "nothing is listening" free. The command
line keeps its plain text logger and gains no dependency on any of this.

Threading
---------

A build runs in one thread and is watched from others. :class:`ProgressTracker`
is the boundary: the building thread calls its sink methods, readers call
:meth:`ProgressTracker.snapshot`, and one lock covers both. Snapshots are frozen
dataclasses, so a reader holds a value that cannot change underneath it and the
lock is never held across anything slow.

Cost
----

The file loop calls the sink 1,504 times on the real corpus and the block loop
24,105 times, so the sink itself has to be cheap: it updates counters under a
lock and returns. Turning those counters into a snapshot is the expensive part,
and it happens at most every :data:`DEFAULT_THROTTLE_SECONDS`, plus whenever the
phase or the state changes, plus on the final event. Watchers therefore see every
transition and a bounded number of within-phase updates, and the build pays for a
few hundred snapshots rather than twenty-five thousand.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

__all__ = [
    "DEFAULT_HISTORY",
    "DEFAULT_THROTTLE_SECONDS",
    "PHASE_LABELS",
    "PHASE_ORDER",
    "expected_phases",
    "BuildPhase",
    "BuildState",
    "CacheMode",
    "CompletedPhase",
    "IndexStats",
    "NULL_SINK",
    "ProgressSink",
    "ProgressSnapshot",
    "ProgressTracker",
]


class BuildState(str, Enum):
    """The lifecycle of one preparation."""

    IDLE = "idle"
    PREPARING = "preparing"
    READY = "ready"
    FAILED = "failed"

    @property
    def finished(self) -> bool:
        return self in (BuildState.READY, BuildState.FAILED)


class BuildPhase(str, Enum):
    """A step the preparation actually performs.

    Every one of these corresponds to real work in
    :mod:`autocomplete.cache`, :mod:`autocomplete.records`,
    :mod:`autocomplete.suffix_index` or :mod:`autocomplete.topk`. There is no
    phase here for work the implementation does not do.
    """

    #: Reading and validating settings. Effectively instant.
    LOADING_CONFIGURATION = "loading_configuration"
    #: Checking that the suffix array builder is present and correct, before a
    #: large corpus is read rather than part-way through indexing it.
    VERIFYING_SUFFIX_BUILDER = "verifying_suffix_builder"
    #: Walking the corpus tree. The file count is unknown until it finishes.
    DISCOVERING_CORPUS = "discovering_corpus"
    #: Hashing the corpus to decide whether a cached index is still valid.
    VALIDATING_CORPUS = "validating_corpus"
    #: Checking a cached generation's manifest and artifacts.
    VALIDATING_ARTIFACTS = "validating_artifacts"
    #: Reading, or memory-mapping, the artifacts of a valid cache.
    LOADING_ARTIFACTS = "loading_artifacts"
    #: Reading every corpus file and normalizing its lines.
    READING_FILES = "reading_files"
    #: Sorting the records into tie-break order and laying out the blobs.
    NORMALIZING_RECORDS = "normalizing_records"
    #: One call into the suffix array builder. It reports nothing part-way.
    BUILDING_SUFFIX_ARRAY = "building_suffix_array"
    #: Summarizing the suffix array block by block.
    BUILDING_BLOCK_SUMMARIES = "building_block_summaries"
    #: Writing a new generation's artifacts.
    WRITING_ARTIFACTS = "writing_artifacts"
    #: Checksumming what was written, for the manifest.
    CHECKSUMMING_ARTIFACTS = "checksumming_artifacts"
    #: Flushing the generation and renaming the pointer onto it.
    PUBLISHING_GENERATION = "publishing_generation"
    #: Nothing left to do.
    READY = "ready"


class CacheMode(str, Enum):
    """Which route through preparation is being taken."""

    UNKNOWN = "unknown"
    #: No cache was present, so everything is being built.
    COLD_BUILD = "cold_build"
    #: A cache is present and the corpus is being hashed to check it.
    WARM_VALIDATION = "warm_validation"
    #: The cache was good and is being read.
    WARM_LOAD = "warm_load"
    #: A rebuild was asked for explicitly.
    FORCED_REBUILD = "forced_rebuild"
    #: A cache was present but could not be used, so it is being rebuilt.
    RECOVERY = "recovery"


#: What each phase is called for a reader. Kept beside the enum so the interface
#: has wording to show without inventing its own, and so the two cannot drift.
PHASE_LABELS: dict[BuildPhase, str] = {
    BuildPhase.LOADING_CONFIGURATION: "Loading configuration",
    BuildPhase.VERIFYING_SUFFIX_BUILDER: "Verifying the suffix array builder",
    BuildPhase.DISCOVERING_CORPUS: "Discovering corpus files",
    # Neutral on purpose: this same hashing decides whether a cache is still
    # good *and* names a new generation, so "validating" would be a lie on a
    # first build, where there is nothing to validate against.
    BuildPhase.VALIDATING_CORPUS: "Fingerprinting the corpus",
    BuildPhase.VALIDATING_ARTIFACTS: "Validating cached artifacts",
    BuildPhase.LOADING_ARTIFACTS: "Loading the cached index",
    BuildPhase.READING_FILES: "Reading corpus files",
    BuildPhase.NORMALIZING_RECORDS: "Ordering sentences",
    BuildPhase.BUILDING_SUFFIX_ARRAY: "Building the suffix array",
    BuildPhase.BUILDING_BLOCK_SUMMARIES: "Summarizing suffix blocks",
    BuildPhase.WRITING_ARTIFACTS: "Writing index artifacts",
    BuildPhase.CHECKSUMMING_ARTIFACTS: "Checksumming artifacts",
    BuildPhase.PUBLISHING_GENERATION: "Publishing the index",
    BuildPhase.READY: "Ready",
}

#: Every phase, in the order they are declared. Rarely what an interface wants:
#: :func:`expected_phases` gives the ones a particular route will actually run.
PHASE_ORDER: tuple[BuildPhase, ...] = tuple(PHASE_LABELS)

_WARM_PLAN: tuple[BuildPhase, ...] = (
    BuildPhase.LOADING_CONFIGURATION,
    BuildPhase.VERIFYING_SUFFIX_BUILDER,
    BuildPhase.VALIDATING_CORPUS,
    BuildPhase.VALIDATING_ARTIFACTS,
    BuildPhase.LOADING_ARTIFACTS,
    BuildPhase.READY,
)

_BUILD_PLAN: tuple[BuildPhase, ...] = (
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
)


def expected_phases(mode: CacheMode) -> tuple[BuildPhase, ...]:
    """The phases a run in this mode is expected to perform, in order.

    A plan, not a promise: a warm validation that finds the cache unusable
    becomes a recovery, and the plan it is measured against changes with it.
    That is honest, and it is why an interface should re-read this whenever the
    mode changes rather than deciding once. A run can also skip a planned phase
    outright, as a ``structural`` validation level skips fingerprinting the
    corpus.
    """
    if mode in (CacheMode.WARM_VALIDATION, CacheMode.WARM_LOAD):
        return _WARM_PLAN
    return _BUILD_PLAN

#: Longest a within-phase update may wait before watchers can see it. Chosen to
#: be well under the interval at which a person notices a number is stale, and
#: far above the cost of building a snapshot.
DEFAULT_THROTTLE_SECONDS = 0.12

#: How many recent snapshots are kept for a watcher that arrives or reconnects
#: mid-build. Bounded on purpose: a build emits a few hundred, and replaying all
#: of them to a new connection would be neither useful nor safe.
DEFAULT_HISTORY = 64


@dataclass(frozen=True)
class CompletedPhase:
    """A phase that finished, and how long it took."""

    phase: BuildPhase
    label: str
    seconds: float


@dataclass(frozen=True)
class IndexStats:
    """What the finished index turned out to be.

    Only facts the index already knows about itself. Nothing here is a path.
    """

    sentences: int
    files: int
    searchable_bytes: int
    longest_sentence: int
    suffix_positions: int
    block_count: int
    block_size: int
    summary_width: int


@dataclass(frozen=True)
class ProgressSnapshot:
    """Everything known about a preparation at one instant.

    Frozen, so a reader can hold one while the build carries on. Every field is
    safe to send to a browser: see :meth:`ProgressTracker.update` for why
    ``current_file`` can only ever be a corpus-relative path.
    """

    #: Increases by one per published snapshot, for a reader to discard anything
    #: it has already seen and to recognise a gap.
    sequence: int
    state: BuildState
    phase: BuildPhase
    phase_label: str
    detail: str
    #: Whether ``current``/``total`` mean anything for this phase. False means
    #: the work cannot report its own progress, and the interface must show that
    #: rather than a number.
    determinate: bool
    current: int
    total: int | None
    #: Path of the file being read, relative to the corpus root, POSIX style.
    current_file: str | None
    files_done: int
    files_total: int | None
    sentences: int
    bytes_done: int
    bytes_total: int | None
    completed_phases: tuple[CompletedPhase, ...]
    phase_elapsed_seconds: float
    elapsed_seconds: float
    cache_mode: CacheMode
    index: IndexStats | None
    #: A stable identifier for a failure, for the interface to branch on.
    error_code: str | None
    #: One sentence a person can act on. Never an exception's own text.
    error_message: str | None
    #: What to try, when there is something sensible to try.
    recovery_hint: str | None

    @property
    def ready(self) -> bool:
        return self.state is BuildState.READY


class ProgressSink(Protocol):
    """What the preparation pipeline calls as it works.

    Deliberately small. The pipeline reports what it is doing and what it has
    done; deciding what to keep, when to publish it and who to tell is the
    tracker's job, not the corpus reader's.
    """

    def begin(
        self,
        phase: BuildPhase,
        *,
        detail: str = "",
        total: int | None = None,
        determinate: bool | None = None,
    ) -> None:
        """Start a phase, closing the previous one."""

    def update(
        self,
        *,
        current: int | None = None,
        advance: int = 0,
        current_file: str | None = None,
        files_done: int | None = None,
        files_total: int | None = None,
        sentences: int | None = None,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
        detail: str | None = None,
        total: int | None = None,
    ) -> None:
        """Report work done inside the current phase."""

    def note_cache_mode(self, mode: CacheMode) -> None:
        """Say which route through preparation is being taken."""


class _NullSink:
    """A sink that does nothing, for every caller that is not being watched.

    Three empty methods rather than a conditional at each call site, so the
    pipeline reads the same whether or not anyone is listening and the cost of
    not being listened to is one call that returns.
    """

    def begin(self, phase, *, detail="", total=None, determinate=None) -> None:
        return None

    def update(self, **fields) -> None:
        return None

    def note_cache_mode(self, mode) -> None:
        return None


#: The sink used when nobody is watching. Shared: it holds nothing.
NULL_SINK: ProgressSink = _NullSink()


class ProgressTracker:
    """Collects progress from a building thread and publishes it to watchers.

    Implements :class:`ProgressSink`, so it is what the pipeline is given.

    Args:
        throttle_seconds: Shortest interval between published within-phase
            snapshots. Phase changes, failures and completion ignore it.
        history: How many recent snapshots to keep for a watcher that connects
            or reconnects part-way through.
        clock: The time source, so tests need no sleeping.
    """

    def __init__(
        self,
        *,
        throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
        history: int = DEFAULT_HISTORY,
        clock=time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._throttle = max(0.0, throttle_seconds)
        self._clock = clock
        self._history: deque[ProgressSnapshot] = deque(maxlen=max(1, history))
        #: Woken whenever a snapshot is published, so a watcher can wait rather
        #: than poll tightly. Watchers that miss a wake still see the sequence.
        self._changed = threading.Condition(self._lock)

        self._sequence = 0
        self._state = BuildState.IDLE
        self._phase = BuildPhase.LOADING_CONFIGURATION
        self._detail = ""
        self._determinate = False
        self._current = 0
        self._total: int | None = None
        self._current_file: str | None = None
        self._files_done = 0
        self._files_total: int | None = None
        self._sentences = 0
        self._bytes_done = 0
        self._bytes_total: int | None = None
        self._completed: list[CompletedPhase] = []
        self._cache_mode = CacheMode.UNKNOWN
        self._index: IndexStats | None = None
        self._error_code: str | None = None
        self._error_message: str | None = None
        self._recovery_hint: str | None = None

        self._started_at: float | None = None
        self._phase_started_at = self._clock()
        self._last_published = float("-inf")
        self._finished_elapsed: float | None = None

        # There is always exactly one snapshot to read, from construction
        # onwards, so no reader ever has to handle an empty history.
        with self._lock:
            self._publish_locked(force=True)

    # ------------------------------------------------------------- the sink ---

    def start(self, mode: CacheMode = CacheMode.UNKNOWN) -> None:
        """Mark a preparation as begun, resetting anything a retry should drop."""
        with self._lock:
            self._state = BuildState.PREPARING
            self._started_at = self._clock()
            self._phase_started_at = self._started_at
            self._finished_elapsed = None
            self._completed = []
            self._cache_mode = mode
            self._index = None
            self._error_code = None
            self._error_message = None
            self._recovery_hint = None
            self._reset_phase_counters_locked()
            self._phase = BuildPhase.LOADING_CONFIGURATION
            self._detail = ""
            self._determinate = False
            self._publish_locked()

    def begin(
        self,
        phase: BuildPhase,
        *,
        detail: str = "",
        total: int | None = None,
        determinate: bool | None = None,
    ) -> None:
        with self._lock:
            if self._state is not BuildState.PREPARING:
                # A phase reported outside a preparation still moves the state,
                # so a caller that forgot to call start() is not silently mute.
                self._state = BuildState.PREPARING
                if self._started_at is None:
                    self._started_at = self._clock()

            now = self._clock()
            if self._phase is not phase:
                self._close_phase_locked(now)
            self._phase = phase
            self._phase_started_at = now
            self._detail = detail
            self._total = total
            self._determinate = (total is not None) if determinate is None else determinate
            self._current = 0
            self._current_file = None
            self._publish_locked(force=True)

    def update(
        self,
        *,
        current: int | None = None,
        advance: int = 0,
        current_file: str | None = None,
        files_done: int | None = None,
        files_total: int | None = None,
        sentences: int | None = None,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
        detail: str | None = None,
        total: int | None = None,
    ) -> None:
        """Record work done. Cheap: counters under a lock, no snapshot unless due.

        Counters only ever move forward. A caller that reports a smaller value
        than the one held is ignored rather than believed, so a watcher never
        sees a count go backwards, whatever order updates arrive in.
        """
        with self._lock:
            if current is not None:
                self._current = max(self._current, current)
            if advance:
                self._current += advance
            if current_file is not None:
                self._current_file = current_file
            if files_done is not None:
                self._files_done = max(self._files_done, files_done)
            if files_total is not None:
                self._files_total = files_total
            if sentences is not None:
                self._sentences = max(self._sentences, sentences)
            if bytes_done is not None:
                self._bytes_done = max(self._bytes_done, bytes_done)
            if bytes_total is not None:
                self._bytes_total = bytes_total
            if detail is not None:
                self._detail = detail
            if total is not None:
                self._total = total
                self._determinate = True
            self._publish_locked()

    def note_cache_mode(self, mode: CacheMode) -> None:
        with self._lock:
            self._cache_mode = mode
            self._publish_locked(force=True)

    # -------------------------------------------------------------- finishing ---

    def finish(self, index: IndexStats | None = None, *, detail: str = "") -> None:
        """Record that preparation succeeded."""
        with self._lock:
            now = self._clock()
            self._close_phase_locked(now)
            self._phase = BuildPhase.READY
            self._phase_started_at = now
            self._state = BuildState.READY
            self._index = index
            self._detail = detail or "Ready to search."
            self._determinate = False
            self._total = None
            self._current = 0
            self._current_file = None
            self._finished_elapsed = self._elapsed_locked(now)
            self._publish_locked(force=True)

    def fail(self, code: str, message: str, *, hint: str | None = None) -> None:
        """Record that preparation failed, with something a person can act on."""
        with self._lock:
            now = self._clock()
            self._state = BuildState.FAILED
            self._error_code = code
            self._error_message = message
            self._recovery_hint = hint
            self._detail = message
            self._determinate = False
            self._current_file = None
            self._finished_elapsed = self._elapsed_locked(now)
            self._publish_locked(force=True)

    # --------------------------------------------------------------- reading ---

    def snapshot(self) -> ProgressSnapshot:
        """The latest published state.

        Held even when nothing is watching, so a browser that opens after a
        build has finished still learns how it went.
        """
        with self._lock:
            return self._history[-1]

    def since(self, sequence: int) -> list[ProgressSnapshot]:
        """Snapshots newer than ``sequence``, oldest first.

        Bounded by the retained history: a watcher that has fallen further
        behind than that gets what is retained, which always ends with the
        current state. Nothing is replayed twice and nothing unbounded is kept.
        """
        with self._lock:
            return [item for item in self._history if item.sequence > sequence]

    def wait_for_change(self, sequence: int, timeout: float) -> bool:
        """Block until a snapshot newer than ``sequence`` exists, or time out.

        Lets a watcher wait instead of polling tightly, without the building
        thread ever having to know a watcher exists.
        """
        deadline = self._clock() + timeout
        with self._lock:
            while self._history[-1].sequence <= sequence:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True

    # ---------------------------------------------------------------- private ---

    def _reset_phase_counters_locked(self) -> None:
        self._current = 0
        self._total = None
        self._current_file = None
        self._files_done = 0
        self._files_total = None
        self._sentences = 0
        self._bytes_done = 0
        self._bytes_total = None

    def _close_phase_locked(self, now: float) -> None:
        if self._state is not BuildState.PREPARING:
            return
        if self._phase is BuildPhase.READY:
            return
        self._completed.append(
            CompletedPhase(
                phase=self._phase,
                label=PHASE_LABELS[self._phase],
                seconds=max(0.0, now - self._phase_started_at),
            )
        )

    def _elapsed_locked(self, now: float) -> float:
        if self._finished_elapsed is not None:
            return self._finished_elapsed
        if self._started_at is None:
            return 0.0
        return max(0.0, now - self._started_at)

    def _publish_locked(self, *, force: bool = False) -> None:
        """Turn the counters into a snapshot, if one is due.

        The throttle is what keeps a twenty-four-thousand iteration loop from
        producing twenty-four thousand snapshots. Anything a watcher must not
        miss — a phase change, a failure, completion — passes ``force``.
        """
        now = self._clock()
        if not force and (now - self._last_published) < self._throttle:
            return
        self._last_published = now
        self._sequence += 1

        self._history.append(
            ProgressSnapshot(
                sequence=self._sequence,
                state=self._state,
                phase=self._phase,
                phase_label=PHASE_LABELS[self._phase],
                detail=self._detail,
                determinate=self._determinate,
                current=self._current,
                total=self._total,
                current_file=self._current_file,
                files_done=self._files_done,
                files_total=self._files_total,
                sentences=self._sentences,
                bytes_done=self._bytes_done,
                bytes_total=self._bytes_total,
                completed_phases=tuple(self._completed),
                phase_elapsed_seconds=max(0.0, now - self._phase_started_at),
                elapsed_seconds=self._elapsed_locked(now),
                cache_mode=self._cache_mode,
                index=self._index,
                error_code=self._error_code,
                error_message=self._error_message,
                recovery_hint=self._recovery_hint,
            )
        )
        self._changed.notify_all()
