"""A few ``autocomplete.scoring`` edges that ``test_scoring.py`` does not
cover: the empty-query and single-character shapes of repair generation, tier
grouping of an empty input, and the guard on ``exact_score``."""

from __future__ import annotations

import pytest

from autocomplete.scoring import (
    EditType,
    exact_score,
    group_into_tiers,
    iter_repairs,
)


class TestExactScoreGuard:
    def test_rejects_a_negative_length(self):
        with pytest.raises(ValueError, match="query_length"):
            exact_score(-1)


class TestIterRepairsShape:
    def test_empty_query_yields_no_repairs(self):
        assert list(iter_repairs(b"")) == []

    def test_one_character_query_excludes_deletion_repairs(self):
        """TA-DECISION D9 (ALLOW_EMPTY_REPAIR=False): deleting the only
        character of a one-character query would repair it to the empty
        pattern, which matches every sentence, so no deletion repair is
        generated for a length-1 query at all."""
        repairs = list(iter_repairs(b"a"))
        assert not any(repair.edit_type is EditType.DELETION for repair in repairs)
        assert any(repair.edit_type is EditType.SUBSTITUTION for repair in repairs)
        assert any(repair.edit_type is EditType.INSERTION for repair in repairs)

    def test_two_character_query_does_include_deletion_repairs(self):
        """The one-character exclusion must not overreach: a two-character
        query has a legal, non-empty deletion repair for each position."""
        repairs = [r for r in iter_repairs(b"ab") if r.edit_type is EditType.DELETION]
        assert {r.pattern for r in repairs} == {b"b", b"a"}


class TestGroupIntoTiers:
    def test_no_patterns_produces_no_tiers(self):
        assert group_into_tiers({}) == []

    def test_tiers_stay_ordered_highest_score_first_with_ties_grouped(self):
        tiers = group_into_tiers({b"lo": 4, b"hi": 9, b"mid": 6, b"hi2": 9})
        assert [tier.score for tier in tiers] == [9, 6, 4]
        assert tiers[0].patterns == (b"hi", b"hi2")
