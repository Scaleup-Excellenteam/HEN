"""The performance limits the system is held to, and how a run is judged.

The numbers come from the design review's gate table
(``docs/design/2026-08-31-autocomplete-design-review-v2.md``, section 5). They
are kept here rather than in prose so that a run either passes or fails rather
than being interpreted.

Latency is judged per query class, never on a blended figure. A mixed percentile
lets one slow class hide behind a fast one, so each class carries its own limit
and any class breaching it fails the run.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "LATENCY_GATES",
    "RESOURCE_GATES",
    "LatencyGate",
    "Outcome",
    "ResourceGate",
    "judge_latency",
    "judge_resource",
]


@dataclass(frozen=True)
class ResourceGate:
    """An upper limit on something measured once per run."""

    label: str
    limit: float
    unit: str
    note: str


@dataclass(frozen=True)
class LatencyGate:
    """Upper limits on a query class, in milliseconds.

    ``None`` means that percentile is not judged for this class, which is the
    case for classes with too few queries for a percentile to mean anything.
    """

    label: str
    note: str
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None
    worst: float | None = 1000.0


@dataclass(frozen=True)
class Outcome:
    """One measurement against one limit."""

    label: str
    statistic: str
    value: float
    limit: float
    unit: str

    @property
    def passed(self) -> bool:
        return self.value <= self.limit

    @property
    def headroom(self) -> float:
        """How many times under the limit the measurement came in."""
        return float("inf") if self.value == 0 else self.limit / self.value


RESOURCE_GATES: dict[str, ResourceGate] = {
    "cold_build_seconds": ResourceGate(
        "Cold build", 300.0, "s", "reading the corpus and building every artifact"
    ),
    "build_peak_rss_gb": ResourceGate(
        "Peak build memory", 4.0, "GB", "high-water mark of the build process"
    ),
    "warm_start_seconds": ResourceGate(
        "Warm start", 5.0, "s", "loading a cached index, corpus fingerprint included"
    ),
    "cache_bytes": ResourceGate("Cache on disk", 1e9, "B", "one generation"),
    "serving_bytes": ResourceGate(
        "Serving artifacts", 1.2e9, "B", "what a query may need to touch"
    ),
}

#: One entry per query class. "typing" stands in for the mixed workload the
#: review sets p50/p95/p99 limits on, because M6 measured it as what interactive
#: use actually produces. The rest are adversarial and carry the review's
#: one-second worst-case limit, except short queries, which it holds to 50 ms.
LATENCY_GATES: dict[str, LatencyGate] = {
    "typing": LatencyGate(
        "Typing a sentence",
        "prefixes of real sentences, the interactive path",
        p50=10.0,
        p95=50.0,
        p99=200.0,
    ),
    "specific phrase": LatencyGate(
        "Specific phrase",
        "long enough that the exact tier runs out and repairs are walked",
        p95=100.0,
    ),
    "short": LatencyGate(
        "Short queries", "one and two characters, first hit", worst=50.0
    ),
    "one typo": LatencyGate(
        "One typo", "a real sentence with a single character edited"
    ),
    "common patterns": LatencyGate(
        "Common patterns", "ranges of millions of occurrences"
    ),
    "repeated characters": LatencyGate(
        "Repeated characters", "queries generating many duplicate repairs"
    ),
    "absent short": LatencyGate("Absent, short", "no match, few repairs"),
    "long garbage": LatencyGate(
        "Long garbage", "no match, every repair looked up and none hits"
    ),
}


def judge_resource(name: str, value: float) -> Outcome:
    gate = RESOURCE_GATES[name]
    return Outcome(gate.label, "value", value, gate.limit, gate.unit)


def judge_latency(name: str, percentiles: dict[str, float]) -> list[Outcome]:
    """Compare one class's timings against the limits set for it."""
    gate = LATENCY_GATES[name]
    checks = [
        ("p50", gate.p50),
        ("p95", gate.p95),
        ("p99", gate.p99),
        ("max", gate.worst),
    ]
    return [
        Outcome(gate.label, statistic, percentiles[statistic], limit, "ms")
        for statistic, limit in checks
        if limit is not None and statistic in percentiles
    ]
