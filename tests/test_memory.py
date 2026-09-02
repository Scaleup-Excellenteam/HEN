"""Tests for reading this process's memory use.

The numbers themselves depend on the machine, so what is checked here is that
each platform is either supported and returns something believable, or reports
``None`` instead of a wrong number, and that the rendering never lies about a
missing reading.
"""

from __future__ import annotations

import sys

import pytest

from autocomplete import memory

#: Platforms with a resident-size reader in memory.py. Anywhere else the module
#: is expected to say it does not know.
SUPPORTED = sys.platform.startswith("linux") or sys.platform == "darwin"


class TestReadings:
    def test_resident_is_a_positive_size_where_supported(self):
        resident = memory.resident_bytes()
        if SUPPORTED:
            assert resident is not None and resident > 0
        else:
            assert resident is None

    def test_peak_is_a_positive_size(self):
        peak = memory.peak_bytes()
        assert peak is None or peak > 0

    def test_peak_is_at_least_the_interpreter_footprint(self):
        """A unit mix-up between kilobytes and bytes is a factor of 1000.

        CPython with numpy and the test suite loaded cannot be under 8 MB, so a
        peak below that means ru_maxrss was read in the wrong unit.
        """
        peak = memory.peak_bytes()
        if peak is not None:
            assert peak > 8_000_000

    def test_resident_grows_when_memory_is_held(self):
        if not SUPPORTED:
            pytest.skip("no resident-size reader on this platform")

        before = memory.resident_bytes()
        # Written to, because an untouched allocation need not be resident.
        held = bytearray(120_000_000)
        held[::4096] = b"\x01" * len(held[::4096])
        after = memory.resident_bytes()
        del held

        assert after - before > 60_000_000


class TestFormatting:
    def test_renders_gigabytes_to_two_places(self):
        assert memory.format_gb(1_240_000_000) == "1.24 GB"

    def test_small_sizes_stay_in_gigabytes(self):
        assert memory.format_gb(50_000_000) == "0.05 GB"

    def test_an_unavailable_reading_is_named_not_zeroed(self):
        assert memory.format_gb(None) == "unknown"

    def test_describe_reports_both_readings(self, monkeypatch):
        monkeypatch.setattr(memory, "resident_bytes", lambda: 1_240_000_000)
        monkeypatch.setattr(memory, "peak_bytes", lambda: 1_310_000_000)
        assert memory.describe() == "1.24 GB resident (peak 1.31 GB)"

    def test_describe_drops_the_half_it_cannot_read(self, monkeypatch):
        monkeypatch.setattr(memory, "resident_bytes", lambda: None)
        monkeypatch.setattr(memory, "peak_bytes", lambda: 1_310_000_000)
        assert memory.describe() == "peak 1.31 GB"

        monkeypatch.setattr(memory, "peak_bytes", lambda: None)
        assert memory.describe() == "unknown on this platform"

    def test_describe_on_this_machine_names_a_size(self):
        if SUPPORTED:
            assert "GB" in memory.describe()
