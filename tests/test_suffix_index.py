"""Tests for suffix-array construction and exact range lookup.

Everything here is checked against a directly computed answer: suffix order
against Python's own sort of the suffixes, and ranges against a scan for the
pattern. Neither uses the suffix array.
"""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from autocomplete.normalize import ALPHABET
from autocomplete.records import RECORD_SEPARATOR
from autocomplete.suffix_index import SuffixIndex, SuffixIndexError, verify_builder

LONG_ENOUGH = 10_000


def naive_order(text: bytes) -> list[int]:
    """Suffix positions in sorted order, computed the obvious way."""
    return sorted(range(len(text)), key=lambda start: text[start:])


def naive_occurrences(text: bytes, pattern: bytes) -> list[int]:
    """Every position where the pattern occurs, found by scanning."""
    if not pattern:
        return []
    return [
        start
        for start in range(len(text) - len(pattern) + 1)
        if text[start : start + len(pattern)] == pattern
    ]


def index_for(text: bytes, max_pattern_length: int = LONG_ENOUGH) -> SuffixIndex:
    return SuffixIndex.build(text, max_pattern_length)


def small_texts() -> list[bytes]:
    """Short strings over a tiny alphabet, including separators."""
    alphabet = b"ab\n"
    texts = [b"", b"a", b"\n", b"aa", b"a\n"]
    for length in range(3, 7):
        texts.extend(bytes(combo) for combo in itertools.product(alphabet, repeat=length))
    return texts


class TestBuilderSelfTest:
    def test_passes_with_the_installed_builder(self):
        verify_builder()

    def test_reports_a_missing_dependency_with_an_install_hint(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "pydivsufsort":
                raise ImportError("no module named pydivsufsort")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        with pytest.raises(SuffixIndexError) as raised:
            verify_builder()
        message = str(raised.value)
        assert "pydivsufsort" in message
        assert "pip install" in message

    def test_reports_a_builder_that_returns_the_wrong_order(self, monkeypatch):
        import pydivsufsort

        monkeypatch.setattr(
            pydivsufsort, "divsufsort", lambda data: np.arange(len(data), dtype=np.int32)
        )
        with pytest.raises(SuffixIndexError, match="unexpected suffix order"):
            verify_builder()


class TestConstruction:
    @pytest.mark.parametrize("text", small_texts())
    def test_matches_a_naive_suffix_sort(self, text):
        assert list(index_for(text).positions) == naive_order(text)

    def test_repeated_characters(self):
        text = b"aaaaaaaa"
        assert list(index_for(text).positions) == naive_order(text)

    def test_text_of_many_separators(self):
        text = b"\n\n\n"
        assert list(index_for(text).positions) == naive_order(text)

    def test_one_character_records(self):
        text = b"a\nb\nc\n"
        assert list(index_for(text).positions) == naive_order(text)

    def test_empty_text_gives_an_empty_index(self):
        index = index_for(b"")
        assert len(index) == 0
        assert index.positions.dtype == np.int32

    def test_positions_are_a_permutation(self):
        text = b"the quick brown fox\njumps over\n"
        assert sorted(index_for(text).positions) == list(range(len(text)))

    def test_array_is_one_dimensional_contiguous_int32(self):
        index = index_for(b"banana\nband\n")
        assert index.positions.ndim == 1
        assert index.positions.dtype == np.int32
        assert index.positions.flags["C_CONTIGUOUS"]

    def test_is_deterministic(self):
        text = b"repeat repeat repeat\nrepeated\n"
        assert list(index_for(text).positions) == list(index_for(text).positions)

    def test_over_the_real_alphabet(self):
        text = RECORD_SEPARATOR.join(
            [ALPHABET, ALPHABET[::-1], b"a b c 0 1 2", b"zzz"]
        ) + RECORD_SEPARATOR
        assert list(index_for(text).positions) == naive_order(text)


class TestUpperBoundKey:
    def test_no_text_byte_reaches_the_bound(self):
        """find() relies on 0xff sorting after everything the text can hold."""
        assert max(ALPHABET) < 0xFF
        assert max(RECORD_SEPARATOR) < 0xFF

    def test_the_module_checks_this_on_import(self):
        from autocomplete.suffix_index import _check_sentinel

        _check_sentinel()


class TestFind:
    @pytest.mark.parametrize(
        "text",
        [
            b"banana\nband\n",
            b"aaaa\n",
            b"a\nb\na\n",
            b"abcabcabc\n",
            b"the cat sat\nthe cat ran\n",
        ],
    )
    def test_every_substring_is_found_exactly(self, text):
        index = index_for(text)
        for start in range(len(text)):
            for stop in range(start + 1, len(text) + 1):
                pattern = text[start:stop]
                if RECORD_SEPARATOR in pattern:
                    continue
                low, high = index.find(pattern)
                found = sorted(int(p) for p in index.occurrences(low, high))
                assert found == naive_occurrences(text, pattern), pattern

    @pytest.mark.parametrize("pattern", [b"zz", b"qqq", b"nana!", b"x"])
    def test_absent_patterns_give_an_empty_range(self, pattern):
        index = index_for(b"banana\nband\n")
        low, high = index.find(pattern)
        assert low == high

    def test_finds_a_match_at_the_first_position(self):
        index = index_for(b"banana\nband\n")
        low, high = index.find(b"ban")
        assert 0 in [int(p) for p in index.occurrences(low, high)]

    def test_finds_a_match_at_the_last_possible_position(self):
        text = b"abc\nxyz\n"
        index = index_for(text)
        low, high = index.find(b"z")
        assert [int(p) for p in index.occurrences(low, high)] == [6]

    def test_returns_every_repeated_occurrence(self):
        index = index_for(b"aaaa\n")
        low, high = index.find(b"aa")
        assert sorted(int(p) for p in index.occurrences(low, high)) == [0, 1, 2]

    def test_a_pattern_equal_to_a_whole_record(self):
        index = index_for(b"alpha\nbeta\n")
        low, high = index.find(b"alpha")
        assert [int(p) for p in index.occurrences(low, high)] == [0]

    def test_prefixes_and_suffixes_of_a_record(self):
        index = index_for(b"alpha\nbeta\n")
        for pattern, expected in [(b"al", [0]), (b"ha", [3]), (b"be", [6]), (b"ta", [8])]:
            low, high = index.find(pattern)
            assert [int(p) for p in index.occurrences(low, high)] == expected

    def test_patterns_next_to_a_separator(self):
        text = b"ab\ncd\n"
        index = index_for(text)
        for pattern in (b"b", b"c", b"ab", b"cd"):
            low, high = index.find(pattern)
            found = sorted(int(p) for p in index.occurrences(low, high))
            assert found == naive_occurrences(text, pattern)

    def test_a_pattern_crossing_a_separator_can_never_match(self):
        """The text holds "ab\\ncd", so "bc" only appears if the boundary is
        ignored. Normalized patterns cannot contain the separator, so the
        crossing string is simply absent."""
        index = index_for(b"ab\ncd\n")
        low, high = index.find(b"bc")
        assert low == high

    def test_rejects_a_pattern_holding_a_separator(self):
        index = index_for(b"ab\ncd\n")
        with pytest.raises(ValueError, match="record separator"):
            index.find(b"b\nc")

    def test_rejects_an_empty_pattern(self):
        with pytest.raises(ValueError, match="empty pattern"):
            index_for(b"abc\n").find(b"")

    def test_a_pattern_longer_than_any_record_returns_nothing(self):
        index = SuffixIndex.build(b"abc\ndefgh\n", max_pattern_length=5)
        assert index.find(b"a" * 6) == (0, 0)

    def test_a_pattern_as_long_as_the_longest_record(self):
        index = SuffixIndex.build(b"abc\ndefgh\n", max_pattern_length=5)
        low, high = index.find(b"defgh")
        assert [int(p) for p in index.occurrences(low, high)] == [4]

    def test_searching_an_empty_index(self):
        assert index_for(b"").find(b"a") == (0, 0)

    def test_range_size_is_the_occurrence_count(self):
        text = b"abababab\n"
        index = index_for(text)
        low, high = index.find(b"ab")
        assert high - low == len(naive_occurrences(text, b"ab"))


class TestPersistence:
    @pytest.mark.parametrize("use_mmap", [True, False])
    def test_round_trip(self, tmp_path, use_mmap):
        text = b"banana\nband\n"
        original = index_for(text)
        original.write_to(tmp_path)
        restored = SuffixIndex.read_from(tmp_path, text, LONG_ENOUGH, use_mmap=use_mmap)
        assert list(restored.positions) == list(original.positions)
        assert restored.find(b"an") == original.find(b"an")

    def test_rejects_an_array_that_does_not_match_the_text(self, tmp_path):
        index_for(b"banana\nband\n").write_to(tmp_path)
        with pytest.raises(SuffixIndexError, match="but the text is"):
            SuffixIndex.read_from(tmp_path, b"shorter\n", LONG_ENOUGH)

    def test_rejects_a_non_integer_array(self, tmp_path):
        np.save(tmp_path / "suffix_array.npy", np.zeros(3, dtype=np.float64))
        with pytest.raises(SuffixIndexError, match="must hold integers"):
            SuffixIndex.read_from(tmp_path, b"abc", LONG_ENOUGH)

    def test_rejects_a_two_dimensional_array(self, tmp_path):
        np.save(tmp_path / "suffix_array.npy", np.zeros((2, 2), dtype=np.int32))
        with pytest.raises(SuffixIndexError, match="one-dimensional"):
            SuffixIndex.read_from(tmp_path, b"abcd", LONG_ENOUGH)
