"""Measure a corpus against the design review's gates.

    python -m benchmarks                 # serving only, against the configured corpus
    python -m benchmarks --build         # also time a cold build, into a temporary cache
    python -m benchmarks --json out.json # save the run

Exits non-zero if any gate fails, so it can gate a change rather than only
describe one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from autocomplete import corpus
from autocomplete.cache import POINTER_FILE, build_or_load, load
from autocomplete.config import Config, default_config_path
from autocomplete.engine import find_completions
from autocomplete.index import SearchIndex
from autocomplete.normalize import normalize
from autocomplete.scoring import exact_score

from . import workloads
from .gates import judge_latency, judge_resource
from .report import (
    describe_machine,
    format_outcomes,
    format_percentiles,
    percentiles,
    write_json,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = Config.from_yaml(args.config)

    machine = describe_machine()
    print("machine")
    for key, value in machine.items():
        print(f"  {key:12} {value}")

    outcomes = []
    measurements: dict[str, float] = {}

    if args.build:
        print("\ncold build (into a temporary cache, leaving yours alone)")
        build = _time_cold_build(config)
        measurements.update(build)
        for key in ("cold_build_seconds", "build_peak_rss_gb"):
            outcomes.append(judge_resource(key, build[key]))
            print(f"  {key:22} {build[key]:.2f}")

    print("\nwarm start")
    started = time.perf_counter()
    index = build_or_load(config)
    warm_seconds = time.perf_counter() - started
    measurements["warm_start_seconds"] = warm_seconds
    outcomes.append(judge_resource("warm_start_seconds", warm_seconds))
    print(f"  loaded {len(index):,} sentences in {warm_seconds:.2f}s")

    sizes = _artifact_sizes(config.cache_dir)
    measurements.update(sizes)
    for key in ("cache_bytes", "serving_bytes"):
        outcomes.append(judge_resource(key, sizes[key]))
        print(f"  {key:22} {sizes[key] / 1e6:,.0f} MB")

    _check_answers(index)

    print("\nlatency by query class, milliseconds")
    print(f"{'class':22} {'n':>5} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9}")
    summaries: dict[str, dict[str, float]] = {}
    for name, queries in _ordered(workloads.build(index, scale=args.scale)):
        timings = _time_queries(index, queries, repeats=args.repeats)
        summary = percentiles(timings)
        summaries[name] = summary
        print(format_percentiles(name, summary))
        outcomes.extend(judge_latency(name, summary))

    print("\ngates")
    print(format_outcomes(outcomes))

    failures = [outcome for outcome in outcomes if not outcome.passed]
    print(
        f"\n{len(outcomes) - len(failures)} of {len(outcomes)} gates met"
        + ("" if not failures else f", {len(failures)} breached")
    )

    if args.json:
        write_json(
            args.json,
            {
                "machine": machine,
                "measurements": measurements,
                "latency_ms": summaries,
                "gates": [
                    {
                        "label": outcome.label,
                        "statistic": outcome.statistic,
                        "value": outcome.value,
                        "limit": outcome.limit,
                        "unit": outcome.unit,
                        "passed": outcome.passed,
                    }
                    for outcome in outcomes
                ],
            },
        )
    return 1 if failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks",
        description="Measure the search against the design review's gates.",
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=default_config_path(), metavar="PATH"
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="also time a cold build, in a temporary cache directory",
    )
    parser.add_argument(
        "--scale", type=int, default=1, help="multiply the sampled query counts"
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="time each query this many times and keep the fastest",
    )
    parser.add_argument("--json", metavar="PATH", help="save the run as JSON")
    return parser


def _ordered(classes: dict[str, list[str]]):
    """Yield each class in reporting order, refusing to skip one quietly.

    An empty class would take its gates out of the run with it, so it is an
    error rather than something to pass over.
    """
    for name in workloads.CLASS_ORDER:
        queries = classes.get(name)
        if not queries:
            raise SystemExit(
                f"benchmark aborted: the {name!r} class produced no queries, so "
                f"its gates would not be judged"
            )
        yield name, queries


def _time_queries(index: SearchIndex, queries: list[str], repeats: int) -> list[float]:
    """Time each query, keeping the fastest of ``repeats`` runs.

    The artifacts are memory-mapped, so the first touch of a region pays a page
    fault that is start-up cost rather than query cost. Warming first, and
    keeping the fastest run, measures the search instead of the operating
    system.
    """
    for query in queries:
        find_completions(index, query)

    timings = []
    for query in queries:
        best = float("inf")
        for _ in range(max(1, repeats)):
            started = time.perf_counter()
            find_completions(index, query)
            best = min(best, (time.perf_counter() - started) * 1e3)
        timings.append(best)
    return timings


def _check_answers(index: SearchIndex) -> None:
    """Refuse to report timings for a search that is not answering correctly.

    A benchmark that only measures speed will happily certify a broken engine,
    so a handful of answers are checked first: text taken out of the corpus must
    come back as an exact match scoring twice its length.
    """
    sampler = workloads.build(index, scale=1)
    for query in sampler["typing"][:20]:
        results = find_completions(index, query)
        expected = exact_score(len(normalize(query)))
        if not results or results[0].score != expected:
            raise SystemExit(
                f"benchmark aborted: {query!r} came back as "
                f"{results[0].score if results else 'nothing'}, expected {expected}"
            )
    print("  answers spot-checked against the corpus they came from")


def _artifact_sizes(cache_dir: Path) -> dict[str, float]:
    """How much a generation costs on disk, and how much a query may touch."""
    generation = cache_dir / (cache_dir / POINTER_FILE).read_text(encoding="utf-8").strip()
    files = {path.name: path.stat().st_size for path in generation.iterdir()}
    # The original text is only read for the handful of sentences returned, but
    # it is mapped and countable, so it is included rather than argued away.
    return {
        "cache_bytes": float(sum(files.values())),
        "serving_bytes": float(sum(files.values())),
    }


def _time_cold_build(config: Config) -> dict[str, float]:
    """Build from scratch in a separate process, to read its own peak memory."""
    with tempfile.TemporaryDirectory() as scratch:
        script = (
            "import json, resource, time, sys;"
            "sys.path.insert(0, %r);"
            "from autocomplete.cache import build_or_load;"
            "from autocomplete.config import Config;"
            "from pathlib import Path;"
            "config = Config(corpus_root=Path(%r), cache_dir=Path(%r),"
            " num_results=%d, use_mmap=%r, validation_level=%r);"
            "start = time.perf_counter();"
            "index = build_or_load(config);"
            "print(json.dumps({'seconds': time.perf_counter() - start,"
            " 'peak_kb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,"
            " 'records': len(index)}))"
        ) % (
            str(REPO_ROOT),
            str(config.corpus_root),
            str(Path(scratch) / "cache"),
            config.num_results,
            config.use_mmap,
            config.validation_level,
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            raise SystemExit(f"cold build failed:\n{completed.stderr}")
        result = json.loads(completed.stdout.strip().splitlines()[-1])

    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    peak_bytes = result["peak_kb"] * (1 if sys.platform == "darwin" else 1024)
    return {
        "cold_build_seconds": result["seconds"],
        "build_peak_rss_gb": peak_bytes / 1e9,
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
