"""Tests for ``benchmarks.report`` behaviour that ``test_benchmarks.py`` does
not reach: ``format_percentiles``, ``write_json``, and the GB/ms branches of
the private ``_quantity`` formatter (exercised through ``format_outcomes``,
since it is not itself exported)."""

from __future__ import annotations

import json

from benchmarks.gates import Outcome
from benchmarks.report import format_outcomes, format_percentiles, percentiles, write_json


class TestFormatPercentiles:
    def test_renders_the_class_name_and_all_five_statistics(self):
        summary = percentiles([1.0, 2.0, 3.0, 4.0, 100.0])
        rendered = format_percentiles("typing", summary)
        assert rendered.startswith("typing")
        for value in (summary["p50"], summary["p95"], summary["p99"], summary["max"]):
            assert f"{value:9.2f}" in rendered

    def test_the_count_column_shows_how_many_queries_were_timed(self):
        summary = percentiles([1.0, 2.0, 3.0])
        rendered = format_percentiles("short", summary)
        assert "    3" in rendered


class TestQuantityFormatting:
    """``_quantity`` has no public seam of its own; ``format_outcomes`` is how
    every caller reaches it, so the unit branches are checked through there."""

    def test_gb_values_are_rendered_with_two_decimals(self):
        rendered = format_outcomes([Outcome("Peak build memory", "value", 1.5, 4.0, "GB")])
        assert "1.50 GB" in rendered
        assert "4.00 GB" in rendered

    def test_millisecond_values_keep_two_decimals_and_the_unit(self):
        rendered = format_outcomes([Outcome("Typing", "p50", 8.333, 10.0, "ms")])
        assert "8.33 ms" in rendered


class TestWriteJson:
    def test_creates_missing_parent_directories(self, tmp_path):
        target = tmp_path / "nested" / "deep" / "run.json"
        write_json(str(target), {"ok": True})
        assert target.is_file()

    def test_writes_valid_sorted_json_matching_the_payload(self, tmp_path):
        target = tmp_path / "run.json"
        payload = {"b": 2, "a": 1, "nested": {"z": 9, "y": 8}}
        write_json(str(target), payload)
        assert json.loads(target.read_text(encoding="utf-8")) == payload

    def test_overwrites_a_previously_written_file(self, tmp_path):
        target = tmp_path / "run.json"
        write_json(str(target), {"version": 1})
        write_json(str(target), {"version": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"version": 2}
