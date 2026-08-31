"""Describing the machine a run happened on, and printing what it found.

A latency figure means nothing without the machine that produced it, so every
run records one. The grading environment is not this one, which is why the
review calls its gates provisional.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Sequence

from .gates import Outcome

__all__ = ["describe_machine", "format_outcomes", "format_percentiles", "percentiles"]


def describe_machine() -> dict[str, str]:
    """What a reader needs to know to compare this run against another."""
    details = {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": str(os.cpu_count() or "unknown"),
        "python": platform.python_version(),
    }
    try:
        import numpy

        details["numpy"] = numpy.__version__
    except ImportError:  # pragma: no cover - numpy is a hard requirement
        details["numpy"] = "missing"

    total_memory = _total_memory_gb()
    if total_memory is not None:
        details["memory_gb"] = f"{total_memory:.0f}"
    return details


def _total_memory_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (AttributeError, ValueError, OSError):
        return None


def percentiles(timings_ms: Sequence[float]) -> dict[str, float]:
    """Summarize a class's timings.

    Percentiles are taken by position in the sorted sample rather than by
    interpolation, so a reported figure is a query that actually happened.
    """
    if not timings_ms:
        return {}
    ordered = sorted(timings_ms)
    count = len(ordered)

    def at(fraction: float) -> float:
        return ordered[min(count - 1, int(count * fraction))]

    return {
        "n": float(count),
        "p50": at(0.50),
        "p95": at(0.95),
        "p99": at(0.99),
        "max": ordered[-1],
    }


def format_percentiles(name: str, summary: dict[str, float]) -> str:
    return (
        f"{name:22} {int(summary['n']):>5} {summary['p50']:9.2f} "
        f"{summary['p95']:9.2f} {summary['p99']:9.2f} {summary['max']:9.2f}"
    )


def format_outcomes(outcomes: Sequence[Outcome]) -> str:
    """A pass or fail line per limit, worst headroom last."""
    lines = []
    for outcome in outcomes:
        mark = "PASS" if outcome.passed else "FAIL"
        value = _quantity(outcome.value, outcome.unit)
        limit = _quantity(outcome.limit, outcome.unit)
        margin = (
            f"{outcome.headroom:>6.1f}x under"
            if outcome.passed
            else f"{1 / outcome.headroom:>6.1f}x over"
        )
        label = f"{outcome.label} {outcome.statistic}".strip()
        lines.append(f"  {mark}  {label:32} {value:>12} of {limit:>12}  {margin}")
    return "\n".join(lines)


def _quantity(value: float, unit: str) -> str:
    if unit == "B":
        return f"{value / 1e6:.0f} MB"
    if unit == "ms":
        return f"{value:.2f} ms"
    if unit == "GB":
        return f"{value:.2f} GB"
    return f"{value:.2f} {unit}"


def write_json(path: str, payload: dict) -> None:
    """Save a run so two of them can be compared later."""
    import json
    from pathlib import Path

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {destination}", file=sys.stderr)
