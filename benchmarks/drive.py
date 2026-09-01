"""Measure what importing documents costs.

    python -m benchmarks.drive              # against the configured corpus
    python -m benchmarks.drive --json out.json

Kept out of ``python -m benchmarks`` and out of its gates on purpose. The gates
describe the search the assignment asks for, and adding an optional feature's
numbers to them would let this feature's cost hide inside the corpus's budget,
or move a limit that was set for something else.

**No network is involved.** Downloading is timed against a stand-in that hands
over bytes already in memory, so what is reported is the cost this project
controls: reading, validating, indexing, publishing and merging. Google's own
latency is not measurable here, is not this project's to promise, and is
reported separately in the implementation notes.

Three sizes are measured, taken from the shipped defaults: a small import, a
middling one, and the largest the configured limits allow.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from autocomplete import composite
from autocomplete.cache import build_or_load
from autocomplete.config import Config, default_config_path
from autocomplete.drive.client import DriveFile
from autocomplete.drive.jobs import DriveService
from autocomplete.drive.settings import DriveSettings
from autocomplete.engine import find_completions
from autocomplete.index import SearchIndex

from . import workloads
from .gates import LATENCY_GATES
from .report import describe_machine, percentiles, write_json

REPO_ROOT = Path(__file__).resolve().parent.parent

#: One document of this many lines is the unit the sizes below are built from.
_LINE = "the quick brown fox jumps over the lazy dog while indexing documents"

SIZES = (
    ("small", 1, 200),
    ("medium", 5, 4_000),
    ("maximum", 10, 40_000),
)

#: How many times the whole workload is replayed for each figure. Three was not
#: enough: it put the p95 of the typing class within a few milliseconds of its
#: own noise, which is the difference between reporting that class as inside its
#: gate and outside it.
_REPEATS = 8


@dataclass
class _Stub:
    """Stands in for Drive, handing over bytes that are already in memory.

    Deliberately not a network client: the point is to time this project's own
    work, and a real download would put Google's latency inside a number this
    project would then be judged on.
    """

    files: dict[str, tuple[DriveFile, bytes]]

    def metadata(self, file_id: str) -> DriveFile:
        return self.files[file_id][0]

    def download(self, file_id: str, *, max_bytes: int) -> bytes:
        return self.files[file_id][1][: max_bytes + 1]

    def export_text(self, file_id: str, *, max_bytes: int) -> bytes:
        return self.download(file_id, max_bytes=max_bytes)


def _documents(count: int, lines_each: int) -> dict[str, tuple[DriveFile, bytes]]:
    files = {}
    for number in range(count):
        text = "\n".join(
            f"{_LINE} {number}-{line}" for line in range(lines_each)
        ).encode("utf-8") + b"\n"
        file_id = f"file-{number}"
        files[file_id] = (
            DriveFile(
                file_id=file_id,
                name=f"document-{number}.txt",
                mime_type="text/plain",
                size=len(text),
                modified_time="2026-09-01T09:00:00.000Z",
                revision_id=f"rev-{number}",
            ),
            text,
        )
    return files


def _service(data_dir: Path, config: Config, stub: _Stub) -> DriveService:
    return DriveService(
        DriveSettings(
            enabled=True,
            client_id="benchmark",
            api_key="benchmark",
            app_id="benchmark",
            data_dir=data_dir,
            max_files=50,
            max_file_bytes=64 * 1024 * 1024,
            max_total_bytes=256 * 1024 * 1024,
        ),
        config,
        client_factory=lambda _token: stub,
        worker=lambda work: work(),
    )


def _latency(
    index: SearchIndex, overlay, workload: dict[str, list[str]]
) -> dict[str, dict[str, float]]:
    """Milliseconds per query, per query class.

    Per class rather than blended, for the same reason the gated run does it:
    a mixed percentile lets one slow class hide behind a fast one, and the
    classes here differ by two orders of magnitude.
    """
    measured: dict[str, dict[str, float]] = {}
    for label, queries in workload.items():
        timings: list[float] = []
        for _ in range(_REPEATS):
            for query in queries:
                started = time.perf_counter()
                if overlay is None:
                    find_completions(index, query)
                else:
                    composite.search(index, overlay, query)
                timings.append((time.perf_counter() - started) * 1000)
        measured[label] = percentiles(timings)
    return measured


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.drive")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--json", type=Path, metavar="PATH", help="save the run")
    args = parser.parse_args(argv)

    config = Config.from_yaml(args.config)

    machine = describe_machine()
    print("machine")
    for key, value in machine.items():
        print(f"  {key:12} {value}")

    print("\nbase corpus")
    started = time.perf_counter()
    index = build_or_load(config)
    print(f"  loaded {len(index):,} sentences in {time.perf_counter() - started:.2f}s")

    # The whole workload the gated run measures, not a slice of it: the classes
    # are not internally uniform, and taking the first few queries of one would
    # produce a figure that cannot be read against the gate for that class.
    workload = {
        label: queries for label, queries in workloads.build(index).items() if queries
    }

    measurements: dict[str, object] = {"machine": machine, "sizes": {}}

    baseline = _latency(index, None, workload)
    measurements["search_without_imports_ms"] = baseline
    print("\nsearch latency with no imported corpus, milliseconds")
    for label, summary in baseline.items():
        print(f"  {label:22} p50 {summary['p50']:8.2f}   p95 {summary['p95']:8.2f}")

    for label, files, lines_each in SIZES:
        workspace = Path(tempfile.mkdtemp(prefix=f"drive-bench-{label}-"))
        try:
            stub = _Stub(_documents(files, lines_each))
            service = _service(workspace, config, stub)

            # Downloading and indexing are timed apart, because they scale
            # differently and a single number would hide which one grows.
            download_seconds = 0.0
            publish_seconds = 0.0

            begin = time.perf_counter()
            job = service.start_import(list(stub.files), "benchmark-token")
            total_seconds = time.perf_counter() - begin
            if job.state.value != "complete":
                print(f"  {label}: import failed ({job.error_message})")
                continue

            # Re-time the two halves separately on a second, clean workspace.
            second = Path(tempfile.mkdtemp(prefix=f"drive-bench-{label}-split-"))
            try:
                split = _service(second, config, stub)
                from autocomplete.drive.documents import fetch_document

                begin = time.perf_counter()
                prepared = [
                    fetch_document(
                        stub,
                        metadata,
                        split.settings,
                        source_text=f"Google Drive/{metadata.name}",
                    )
                    for metadata, _text in stub.files.values()
                ]
                download_seconds = time.perf_counter() - begin

                begin = time.perf_counter()
                split.store.publish(prepared)
                publish_seconds = time.perf_counter() - begin
            finally:
                shutil.rmtree(second, ignore_errors=True)

            corpus = service.corpus
            assert corpus is not None
            overlay_bytes = _directory_bytes(workspace)
            with_imports = _latency(index, service.overlay, workload)

            # Reloading what was published, as a restarted server would.
            begin = time.perf_counter()
            reopened = _service(workspace, config, stub)
            reopened.load_published_state()
            reload_seconds = time.perf_counter() - begin

            # Removing one document and publishing the state without it.
            begin = time.perf_counter()
            service.start_removal(corpus.documents[0].id)
            removal_seconds = time.perf_counter() - begin

            row = {
                "files": files,
                "sentences": corpus.sentences,
                "imported_bytes": corpus.total_bytes,
                "import_total_seconds": total_seconds,
                "download_seconds": download_seconds,
                "index_build_and_publish_seconds": publish_seconds,
                "overlay_on_disk_bytes": overlay_bytes,
                "reload_seconds": reload_seconds,
                "removal_seconds": removal_seconds,
                "search_with_imports_ms": with_imports,
                "added_ms_by_class": {
                    label: with_imports[label]["p50"] - baseline[label]["p50"]
                    for label in with_imports
                },
            }
            measurements["sizes"][label] = row  # type: ignore[index]

            print(f"\n{label}: {files} document(s), {corpus.sentences:,} sentences")
            print(f"  import, end to end        {total_seconds:8.2f}s")
            print(f"    reading and validating  {download_seconds:8.2f}s")
            print(f"    indexing and publishing {publish_seconds:8.2f}s")
            print(f"  imported text             {corpus.total_bytes / 1e6:8.2f} MB")
            print(f"  overlay on disk           {overlay_bytes / 1e6:8.2f} MB")
            print(f"  reload at start-up        {reload_seconds:8.2f}s")
            print(f"  remove one and republish  {removal_seconds:8.2f}s")
            print("  search latency by class, p50 ms, and what the overlay added")
            for label, summary in with_imports.items():
                added = summary["p50"] - baseline[label]["p50"]
                print(
                    f"    {label:20} {summary['p50']:8.2f}   "
                    f"{added:+7.2f} against no imports"
                )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    _report_against_gates(baseline, measurements["sizes"])  # type: ignore[arg-type]

    if args.json:
        write_json(args.json, measurements)
        print(f"\nwrote {args.json}")
    return 0


def _report_against_gates(
    baseline: dict[str, dict[str, float]], sizes: dict[str, dict]
) -> None:
    """Show the imported path beside the limits set for the corpus search.

    Those limits govern the corpus search, which this feature does not touch and
    which the gated run still measures on its own. They are printed here because
    they are the only numbers anyone has agreed are acceptable, so they are the
    right yardstick for asking whether importing has cost too much, even though
    failing one here does not fail the project's gates.
    """
    print("\nthe imported path beside the limits set for the corpus search")
    print(f"  {'class':22} {'stat':5} {'no imports':>11} {'largest':>9} {'limit':>9}")
    for label, gate in LATENCY_GATES.items():
        if label not in baseline:
            continue
        for statistic in ("p50", "p95", "p99", "max"):
            limit = getattr(gate, "worst" if statistic == "max" else statistic)
            if limit is None:
                continue
            worst = max(
                row["search_with_imports_ms"][label][statistic] for row in sizes.values()
            )
            flag = "" if worst <= limit else "   <- above it"
            print(
                f"  {label:22} {statistic:5} {baseline[label][statistic]:11.2f} "
                f"{worst:9.2f} {limit:9.1f}{flag}"
            )
    print(
        "  The corpus search itself is unchanged, and python -m benchmarks still\n"
        "  judges it against these limits on its own."
    )


if __name__ == "__main__":
    raise SystemExit(main())
