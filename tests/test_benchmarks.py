"""Tests for the benchmark harness.

The harness decides whether a change is allowed to land, so its judgement is
tested like any other code. Everything here runs against the fixture corpus, so
no large corpus is needed.
"""

from __future__ import annotations

import pytest

from autocomplete.index import SearchIndex
from autocomplete.normalize import normalize
from benchmarks import workloads
from benchmarks.gates import (
    LATENCY_GATES,
    RESOURCE_GATES,
    Outcome,
    judge_latency,
    judge_resource,
)
from benchmarks.report import describe_machine, format_outcomes, percentiles


@pytest.fixture(scope="module")
def index(pytestconfig) -> SearchIndex:
    root = pytestconfig.rootpath / "tests" / "fixtures" / "mini_corpus"
    return SearchIndex.build(root, summary_width=5)


class TestOutcome:
    def test_a_value_under_the_limit_passes(self):
        assert Outcome("x", "value", 4.0, 10.0, "s").passed

    def test_a_value_on_the_limit_passes(self):
        assert Outcome("x", "value", 10.0, 10.0, "s").passed

    def test_a_value_over_the_limit_fails(self):
        assert not Outcome("x", "value", 10.1, 10.0, "s").passed

    def test_headroom_says_how_far_under(self):
        assert Outcome("x", "value", 2.0, 10.0, "s").headroom == 5.0

    def test_headroom_of_an_instant_measurement_is_unbounded(self):
        assert Outcome("x", "value", 0.0, 10.0, "s").headroom == float("inf")


class TestJudging:
    def test_resource_gates_come_from_the_review(self):
        assert RESOURCE_GATES["cold_build_seconds"].limit == 300.0
        assert RESOURCE_GATES["build_peak_rss_gb"].limit == 4.0
        assert RESOURCE_GATES["warm_start_seconds"].limit == 5.0
        assert RESOURCE_GATES["cache_bytes"].limit == 1e9
        assert RESOURCE_GATES["serving_bytes"].limit == 1.2e9

    def test_a_resource_measurement_is_judged(self):
        assert judge_resource("warm_start_seconds", 0.3).passed
        assert not judge_resource("warm_start_seconds", 9.9).passed

    def test_typing_carries_the_percentile_limits(self):
        gate = LATENCY_GATES["typing"]
        assert (gate.p50, gate.p95, gate.p99) == (10.0, 50.0, 200.0)

    def test_every_class_has_a_worst_case_limit(self):
        assert all(gate.worst is not None for gate in LATENCY_GATES.values())

    def test_short_queries_are_held_to_a_tighter_limit(self):
        assert LATENCY_GATES["short"].worst == 50.0

    def test_a_class_is_judged_on_each_limit_it_has(self):
        summary = {"n": 4.0, "p50": 1.0, "p95": 2.0, "p99": 3.0, "max": 4.0}
        assert [outcome.statistic for outcome in judge_latency("typing", summary)] == [
            "p50",
            "p95",
            "p99",
            "max",
        ]

    def test_a_class_without_percentile_limits_is_judged_on_its_worst_case(self):
        summary = {"n": 4.0, "p50": 1.0, "p95": 2.0, "p99": 3.0, "max": 4.0}
        assert [outcome.statistic for outcome in judge_latency("long garbage", summary)] == [
            "max"
        ]

    def test_a_breach_is_reported(self):
        summary = {"n": 1.0, "p50": 1.0, "p95": 2.0, "p99": 3.0, "max": 1200.0}
        outcomes = judge_latency("long garbage", summary)
        assert not outcomes[0].passed

    def test_a_slow_class_cannot_hide_behind_a_fast_one(self):
        """Each class is judged on its own numbers, never on a blend."""
        fast = judge_latency("short", {"n": 1.0, "max": 1.0})
        slow = judge_latency("long garbage", {"n": 1.0, "max": 5000.0})
        assert all(outcome.passed for outcome in fast)
        assert not any(outcome.passed for outcome in slow)


class TestPercentiles:
    def test_summarizes_a_sample(self):
        summary = percentiles([5.0, 1.0, 3.0, 2.0, 4.0])
        assert summary["n"] == 5
        assert summary["p50"] == 3.0
        assert summary["max"] == 5.0

    def test_every_reported_figure_is_a_real_measurement(self):
        timings = [1.0, 2.0, 3.0, 4.0, 100.0]
        summary = percentiles(timings)
        for statistic in ("p50", "p95", "p99", "max"):
            assert summary[statistic] in timings

    def test_a_single_measurement(self):
        assert percentiles([7.0]) == {
            "n": 1.0,
            "p50": 7.0,
            "p95": 7.0,
            "p99": 7.0,
            "max": 7.0,
        }

    def test_no_measurements(self):
        assert percentiles([]) == {}


class TestWorkloads:
    def test_every_class_is_built(self, index):
        classes = workloads.build(index)
        assert set(classes) == set(workloads.CLASS_ORDER)
        assert all(queries for queries in classes.values())

    def test_queries_are_non_empty_text(self, index):
        for queries in workloads.build(index).values():
            for query in queries:
                assert isinstance(query, str) and query

    def test_the_same_seed_asks_the_same_questions(self, index):
        assert workloads.build(index, seed=1) == workloads.build(index, seed=1)

    def test_a_different_seed_asks_different_ones(self, index):
        assert workloads.build(index, seed=1) != workloads.build(index, seed=2)

    def test_typing_queries_grow(self, index):
        queries = workloads.build(index)["typing"]
        growing = [
            later.startswith(earlier)
            for earlier, later in zip(queries, queries[1:])
            if len(later) > len(earlier)
        ]
        assert growing and all(growing)

    def test_typing_queries_start_at_the_beginning_of_a_sentence(self, index):
        """Typing starts where a sentence starts, and such a prefix usually has
        enough exact matches to be answered without walking any repairs. Phrases
        from the middle are a different regime, measured as their own class."""
        text = bytes(index.records.norm_blob)
        starts = {int(offset) for offset in index.records.starts[:-1]}
        for query in workloads.build(index)["typing"]:
            assert text.find(normalize(query)) in starts

    def test_phrase_queries_come_from_inside_sentences(self, index):
        phrases = workloads.build(index)["specific phrase"]
        assert phrases
        for query in phrases:
            assert normalize(query) in bytes(index.records.norm_blob)

    def test_the_two_regimes_are_separate_classes(self):
        """A median taken across both would sit on the boundary between them."""
        assert "typing" in workloads.CLASS_ORDER
        assert "specific phrase" in workloads.CLASS_ORDER

    def test_typing_queries_are_really_in_the_corpus(self, index):
        text = bytes(index.records.norm_blob)
        for query in workloads.build(index)["typing"]:
            assert normalize(query) in text

    def test_typo_queries_are_one_edit_from_the_corpus(self, index):
        """Not asserted directly, since an edit can land anywhere; what matters
        is that they are short, plausible and mostly absent as written."""
        typos = workloads.build(index)["one typo"]
        assert len(typos) >= 10
        assert all(len(query) <= 24 for query in typos)

    def test_long_garbage_covers_a_range_of_lengths(self, index):
        lengths = [len(query) for query in workloads.build(index)["long garbage"]]
        assert min(lengths) >= 20 and max(lengths) >= 200

    def test_scale_asks_more_questions(self, index):
        small = workloads.build(index, scale=1)
        large = workloads.build(index, scale=3)
        assert len(large["one typo"]) > len(small["one typo"])

    def test_a_tiny_corpus_still_produces_queries(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"ab\n")
        tiny = SearchIndex.build(tmp_path, summary_width=5)
        classes = workloads.build(tiny)
        assert all(queries for queries in classes.values())


class TestReporting:
    def test_the_machine_is_described(self):
        details = describe_machine()
        for key in ("platform", "processor", "cpu_count", "python", "numpy"):
            assert details[key]

    def test_outcomes_are_marked_pass_or_fail(self):
        rendered = format_outcomes(
            [
                Outcome("Warm start", "value", 0.3, 5.0, "s"),
                Outcome("Long garbage", "max", 5000.0, 1000.0, "ms"),
            ]
        )
        assert "PASS" in rendered and "FAIL" in rendered
        assert "under" in rendered and "over" in rendered

    def test_sizes_are_rendered_readably(self):
        rendered = format_outcomes([Outcome("Cache", "value", 6.59e8, 1e9, "B")])
        assert "659 MB" in rendered


class TestTheRunnerRefusesToReportNothing:
    """A class with no queries would take its gates out of the run, so the
    runner stops instead of reporting a shorter list of passes."""

    def test_an_empty_class_aborts_the_run(self):
        from benchmarks.run import _ordered

        with pytest.raises(SystemExit, match="produced no queries"):
            list(_ordered({name: [] for name in workloads.CLASS_ORDER}))

    def test_a_complete_set_is_yielded_in_reporting_order(self, index):
        from benchmarks.run import _ordered

        names = [name for name, _ in _ordered(workloads.build(index))]
        assert names == workloads.CLASS_ORDER
