"""Command-line entry point.

Runs the offline phase: read the corpus and prepare it for serving, reusing a
cached index when the corpus has not changed. The suffix array (M4) and the
interactive completion loop (M6) attach here.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from autocomplete import __version__
from autocomplete.cache import POINTER_FILE, build_or_load
from autocomplete.config import DEFAULT_CONFIG_FILENAME, Config, ConfigError
from autocomplete.corpus import CorpusNotFoundError
from autocomplete.suffix_index import SuffixIndexError

REPO_ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Autocomplete over a corpus of sentences, tolerating at most one "
            "typing error."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=REPO_ROOT / DEFAULT_CONFIG_FILENAME,
        metavar="PATH",
        help="configuration file to load (default: %(default)s)",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="prepare the index, then exit",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="rebuild the index even when the cached one is still valid",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = Config.from_yaml(args.config)
    except ConfigError as exc:
        print(f"error: {exc}")
        return 2

    print(f"autocomplete {__version__}")
    print(f"config file      : {args.config}")
    print(f"corpus root      : {config.corpus_root}")
    print(f"cache directory  : {config.cache_dir}")
    print(f"results per query: {config.num_results}")
    print(f"memory-mapped    : {config.use_mmap}")
    print(f"cache validation : {config.validation_level}")
    print()

    started = time.perf_counter()
    try:
        index = build_or_load(
            config,
            force_rebuild=args.rebuild,
            log=lambda message: print(f"  {message}"),
        )
    except (CorpusNotFoundError, SuffixIndexError) as exc:
        print(f"error: {exc}")
        return 2
    elapsed = time.perf_counter() - started

    records = index.records
    print()
    print(f"sentences        : {len(records):,} from {len(records.paths):,} files")
    print(f"longest sentence : {records.max_record_length} characters")
    print(f"searchable text  : {len(records.norm_blob) / 1e6:.1f} MB")
    print(
        f"suffix array     : {len(index.suffix):,} positions "
        f"({index.suffix.positions.dtype})"
    )
    print(
        f"block summaries  : {index.blocks.summaries.shape[0]:,} blocks of "
        f"{index.block_size}, {index.summary_width} records each"
    )
    print(f"cache directory  : {_describe_cache(config.cache_dir)}")
    print(f"ready in         : {elapsed:.1f}s")

    if not (args.build or args.rebuild):
        print()
        print("Completions are not available yet: the fuzzy search that fills in")
        print("results for a mistyped query arrives in M5, and the interactive")
        print("loop in M6. See README.md for status.")
    return 0


def _describe_cache(cache_dir: Path) -> str:
    """Report the current generation and how much room it takes."""
    try:
        generation = (cache_dir / POINTER_FILE).read_text(encoding="utf-8").strip()
        size = sum(
            path.stat().st_size for path in (cache_dir / generation).iterdir()
        )
    except OSError:
        return str(cache_dir)
    return f"{generation} ({size / 1e6:.0f} MB)"


if __name__ == "__main__":
    raise SystemExit(main())
