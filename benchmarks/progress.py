"""Measure what progress reporting costs.

    python -m benchmarks.progress                 # against the configured corpus
    python -m benchmarks.progress --json out.json

Separate from ``python -m benchmarks`` and outside its gates. Those gates
describe the search the assignment asks for; this measures an interface feature,
and letting it spend their budget would be a way of hiding it.

**The real cache is never touched.** Every build here writes into a temporary
directory that is deleted afterwards, so running this does not rebuild, discard
or invalidate the index the developer is using. Only the corpus is read, and it
is only read.

The target, set before the work was done: reporting must add no more than **2%**
to a cold build, and nothing measurable to search. Search is expected to be
exactly unaffected rather than nearly so, because the sink is not on the query
path at all: nothing in ``find_completions`` reports anything.

Two per cent, though, turns out to be *below the noise* of the thing being
measured: a cold build of the real corpus varies by five or six per cent between
runs, which is more than the difference being looked for. Timing a build with
reporting against one without it therefore cannot answer the question, and
running it enough times to average the noise away would take hours.

So the cost is measured directly instead. The pipeline calls the sink a known
number of times, that number is counted, one call is timed, and the product is
what reporting adds. That is a causal measurement rather than a differential
one, it is not affected by how busy the machine is, and it resolves microseconds
where the differential could not resolve a second. The differential is still run
and printed, because it would catch a mistake the direct measurement could miss —
an accidental change to the *work*, rather than to the reporting around it — but
it is reported with its spread so nobody reads a difference into its noise.
"""

from __future__ import annotations

import argparse
import gc
import json
import shutil
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

from autocomplete.config import Config, default_config_path
from autocomplete.engine import find_completions
from autocomplete.preparation import prepare
from autocomplete.progress import ProgressTracker

from . import workloads
from .report import describe_machine, percentiles, write_json

#: How many readers to put on the tracker to stand in for connected browsers.
#: A real Server-Sent Events connection reads the tracker and nothing else, so
#: this is the load one places on a build, without the HTTP machinery that is
#: not what is being measured.
SUBSCRIBER_COUNTS = (0, 1, 4)

#: Runs per configuration for the differential. Kept small on purpose: more runs
#: would not rescue it, because the noise is inherent to the build rather than to
#: the sample size, and the direct measurement below is what answers the question.
REPEATS = 3

#: Sink calls one cold build of the real corpus makes: once per file while
#: fingerprinting, once per file while reading, and once per suffix-array block.
#: Counted from the pipeline rather than guessed; the direct measurement scales
#: the cost of one call by this.
SINK_CALLS_PER_BUILD = 1504 + 1504 + 24105


def temporary_config(base: Config) -> tuple[Config, Path]:
    """A config pointing at the real corpus and a throwaway cache."""
    workspace = Path(tempfile.mkdtemp(prefix="hen-progress-"))
    return (
        Config(
            corpus_root=base.corpus_root,
            cache_dir=workspace / "cache",
            num_results=base.num_results,
            use_mmap=base.use_mmap,
            validation_level=base.validation_level,
        ),
        workspace,
    )


class Readers:
    """Threads reading the tracker as fast as they can, like connected clients."""

    def __init__(self, tracker: ProgressTracker, count: int) -> None:
        self.tracker = tracker
        self.count = count
        self.reads = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def __enter__(self) -> "Readers":
        for _ in range(self.count):
            thread = threading.Thread(target=self._read, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def _read(self) -> None:
        while not self._stop.is_set():
            self.tracker.snapshot()
            self.tracker.since(0)
            self.reads += 1
            # A browser is told about changes at most every tenth of a second;
            # reading in a tight loop would measure something nothing does.
            time.sleep(0.01)

    def __exit__(self, *args) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)


def cold_build(base: Config, subscribers: int | None) -> dict:
    """One cold build into a throwaway cache. ``None`` means no tracker at all."""
    config, workspace = temporary_config(base)
    try:
        tracker = ProgressTracker() if subscribers is not None else None
        gc.collect()

        if tracker is None:
            started = time.perf_counter()
            prepare(config)
            return {"seconds": time.perf_counter() - started, "snapshots": 0, "reads": 0}

        with Readers(tracker, subscribers) as readers:
            started = time.perf_counter()
            prepare(config, tracker)
            seconds = time.perf_counter() - started
        return {
            "seconds": seconds,
            "snapshots": tracker.snapshot().sequence,
            "reads": readers.reads,
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def warm_start(base: Config) -> dict:
    """Build once into a throwaway cache, then measure loading it again."""
    config, workspace = temporary_config(base)
    try:
        prepare(config)
        timings = []
        for _ in range(REPEATS):
            tracker = ProgressTracker()
            gc.collect()
            started = time.perf_counter()
            prepare(config, tracker)
            timings.append(time.perf_counter() - started)
        return {"seconds": min(timings), "snapshots": tracker.snapshot().sequence}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def reporting_memory(base: Config) -> dict:
    """How much the progress mechanism itself retains during a real build."""
    config, workspace = temporary_config(base)
    try:
        tracker = ProgressTracker()
        tracemalloc.start()
        prepare(config, tracker)
        snapshot = tracemalloc.take_snapshot()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Only what progress.py itself allocated: the build's own arrays are
        # not this feature's cost and would swamp it.
        ours = snapshot.filter_traces(
            (tracemalloc.Filter(True, "*autocomplete/progress.py"),)
        )
        retained = sum(item.size for item in ours.statistics("filename"))
        return {
            "retained_bytes": retained,
            "process_peak_bytes": peak,
            "history_entries": len(tracker.since(0)),
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def search_latency(base: Config) -> dict:
    """Search after readiness, to confirm the query path is untouched."""
    config, workspace = temporary_config(base)
    try:
        index = prepare(config, ProgressTracker())
        queries = [
            query for group in workloads.build(index).values() for query in group
        ]
        timings = []
        for _ in range(3):
            for query in queries:
                started = time.perf_counter()
                find_completions(index, query)
                timings.append((time.perf_counter() - started) * 1000)
        return percentiles(timings)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def direct_cost(base: Config) -> dict:
    """Time one sink call, and scale it by how many a real build makes.

    The measurement the target is judged on. It answers "what does reporting
    add" without subtracting two noisy numbers from each other.
    """
    from autocomplete.preparation import _GuardedSink
    from autocomplete.progress import BuildPhase
    from autocomplete import corpus

    tracker = ProgressTracker()
    sink = _GuardedSink(tracker)
    tracker.start()
    sink.begin(BuildPhase.BUILDING_BLOCK_SUMMARIES, total=SINK_CALLS_PER_BUILD)

    samples = 200_000
    gc.collect()
    started = time.perf_counter()
    for n in range(samples):
        sink.update(current=n + 1)
    per_call = (time.perf_counter() - started) / samples

    # The one syscall this feature added to the build itself: a stat per file,
    # so that "data read" can be shown against a total rather than alone.
    files = list(corpus.iter_files(base.corpus_root))
    gc.collect()
    started = time.perf_counter()
    sum(item.path.stat().st_size for item in files)
    stat_seconds = time.perf_counter() - started

    return {
        "sink_calls_per_build": SINK_CALLS_PER_BUILD,
        "seconds_per_call": per_call,
        "sink_seconds_per_build": per_call * SINK_CALLS_PER_BUILD,
        "corpus_stat_seconds": stat_seconds,
        "added_seconds": per_call * SINK_CALLS_PER_BUILD + stat_seconds,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.progress")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--json", type=Path, metavar="PATH")
    args = parser.parse_args(argv)

    base = Config.from_yaml(args.config)
    machine = describe_machine()
    print("machine")
    for key, value in machine.items():
        print(f"  {key:12} {value}")
    print(f"\ncorpus  {base.corpus_root}")
    print("caches  temporary; the configured cache is never written")

    measurements: dict[str, object] = {"machine": machine}

    print(f"\ncold build, {REPEATS} runs each, best of")
    print(f"  {'reporting':<28} {'seconds':>9} {'snapshots':>10} {'reads':>8}")

    runs: dict[str, dict] = {}
    without = min(
        (cold_build(base, None) for _ in range(REPEATS)), key=lambda row: row["seconds"]
    )
    runs["none"] = without
    print(f"  {'none':<28} {without['seconds']:9.2f} {'-':>10} {'-':>8}")

    for count in SUBSCRIBER_COUNTS:
        best = min(
            (cold_build(base, count) for _ in range(REPEATS)),
            key=lambda row: row["seconds"],
        )
        label = f"tracker, {count} subscriber{'' if count == 1 else 's'}"
        runs[f"subscribers_{count}"] = best
        overhead = (best["seconds"] / without["seconds"] - 1) * 100
        print(
            f"  {label:<28} {best['seconds']:9.2f} {best['snapshots']:10,} "
            f"{best['reads']:8,}   {overhead:+.2f}%"
        )

    measurements["cold_build"] = runs
    spread = [runs[key]["seconds"] for key in runs]
    print(
        f"\n  these four runs span {max(spread) - min(spread):.2f}s "
        f"({(max(spread) / min(spread) - 1) * 100:.1f}%), which is the build's own\n"
        f"  run-to-run variance. It is larger than the difference being looked for,\n"
        f"  so no overhead should be read from the column above."
    )

    direct = direct_cost(base)
    measurements["direct_cost"] = direct
    fastest = min(spread)
    print("\nwhat reporting adds, measured directly")
    print(f"  sink calls per cold build   {direct['sink_calls_per_build']:,}")
    print(f"  cost of one call            {direct['seconds_per_call'] * 1e6:.3f} us")
    print(f"  sink cost per build         {direct['sink_seconds_per_build'] * 1000:.1f} ms")
    print(f"  stat() over the corpus      {direct['corpus_stat_seconds'] * 1000:.1f} ms")
    print(f"  total added                 {direct['added_seconds'] * 1000:.1f} ms")
    share = direct["added_seconds"] / fastest * 100
    measurements["overhead_percent"] = share
    print(
        f"  against a {fastest:.1f}s cold build  {share:.3f}%   "
        f"(target: <= 2%)  {'MET' if share <= 2 else 'MISSED'}"
    )

    warm = warm_start(base)
    measurements["warm_start"] = warm
    print(f"\nwarm start\n  {warm['seconds']:.2f}s, {warm['snapshots']:,} snapshots")

    memory = reporting_memory(base)
    measurements["memory"] = memory
    print(
        f"\nprogress mechanism memory\n"
        f"  retained by progress.py {memory['retained_bytes'] / 1024:.1f} KB\n"
        f"  history entries kept    {memory['history_entries']}"
    )

    latency = search_latency(base)
    measurements["search_after_ready_ms"] = latency
    print(
        f"\nsearch after readiness, milliseconds\n"
        f"  p50 {latency['p50']:.2f}   p95 {latency['p95']:.2f}   max {latency['max']:.2f}"
    )
    print("  the query path reports nothing, so this is the unmodified engine")

    if args.json:
        write_json(args.json, measurements)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
