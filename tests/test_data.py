"""Tests for the mandated result dataclass."""

from __future__ import annotations

import dataclasses

from autocomplete import AutoCompleteData
from autocomplete.data import tie_break_key


def test_dataclass_fields_match_the_assignment_exactly():
    """Field names, order and types are fixed by the assignment."""
    spec = [
        ("completed_sentence", "str"),
        ("source_text", "str"),
        ("offset", "int"),
        ("score", "int"),
    ]
    actual = [(f.name, f.type) for f in dataclasses.fields(AutoCompleteData)]
    assert actual == spec


def test_is_a_dataclass_constructible_positionally():
    item = AutoCompleteData("Alpha: this is a demo.", "example.txt", 1, 14)
    assert dataclasses.is_dataclass(item)
    assert item.completed_sentence == "Alpha: this is a demo."
    assert item.source_text == "example.txt"
    assert item.offset == 1
    assert item.score == 14


def test_equality_is_field_wise():
    a = AutoCompleteData("s", "f.txt", 1, 10)
    b = AutoCompleteData("s", "f.txt", 1, 10)
    c = AutoCompleteData("s", "f.txt", 2, 10)
    assert a == b
    assert a != c


def test_str_matches_the_format_in_the_assignment_example():
    item = AutoCompleteData("Alpha: this is a demo.", "example.txt", 1, 14)
    assert str(item) == "Alpha: this is a demo. (example.txt:1, score=14)"


def test_ranking_key_sorts_by_score_descending_then_alphabetically():
    low = AutoCompleteData("aaa", "f.txt", 1, 10)
    high = AutoCompleteData("zzz", "f.txt", 1, 20)
    assert sorted([low, high], key=lambda x: x.ranking_key) == [high, low]


def test_ranking_key_breaks_score_ties_on_the_original_sentence():
    """TA-DECISION D7': ties order by the original sentence, codepoint ascending."""
    gamma = AutoCompleteData("Gamma: this is a demo.", "example.txt", 4, 14)
    delta = AutoCompleteData("Delta: this is a demo.", "example.txt", 3, 14)
    assert sorted([gamma, delta], key=lambda x: x.ranking_key) == [delta, gamma]


def test_ranking_key_breaks_full_ties_on_source_then_offset():
    first = AutoCompleteData("same", "a.txt", 1, 14)
    second = AutoCompleteData("same", "a.txt", 2, 14)
    third = AutoCompleteData("same", "b.txt", 1, 14)
    shuffled = [third, second, first]
    assert sorted(shuffled, key=lambda x: x.ranking_key) == [first, second, third]


def test_tie_break_key_is_the_single_policy_point():
    assert tie_break_key("s", "f.txt", 3) == ("s", "f.txt", 3)
