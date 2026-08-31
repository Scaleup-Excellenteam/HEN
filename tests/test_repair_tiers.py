"""Tests for repair generation, pattern deduplication and score tiers."""

from __future__ import annotations

import pytest

from autocomplete.normalize import ALPHABET, ALPHABET_SIZE
from autocomplete.scoring import (
    ALLOW_EMPTY_REPAIR,
    EditType,
    ScoreTier,
    best_scores_by_pattern,
    deletion_score,
    group_into_tiers,
    insertion_score,
    iter_repairs,
    repair_tiers,
    substitution_score,
)
from tests.support.alignment_reference import best_alignment_score


def raw_repairs(query: bytes, alphabet: bytes = ALPHABET):
    return list(iter_repairs(query, alphabet))


class TestGeneratedCounts:
    @pytest.mark.parametrize("length", [2, 3, 5, 10])
    def test_total_is_74m_plus_37(self, length):
        query = bytes(b"ab"[i % 2] for i in range(length))
        assert len(raw_repairs(query)) == 74 * length + 37

    def test_one_character_query_has_no_deletion(self):
        """TA-DECISION D9: deleting the only character would match everything."""
        repairs = raw_repairs(b"a")
        assert not [r for r in repairs if r.edit_type is EditType.DELETION]
        assert len(repairs) == 36 + 2 * 37

    def test_counts_per_edit_type(self):
        query = b"abc"
        repairs = raw_repairs(query)
        counts = {
            edit_type: sum(1 for r in repairs if r.edit_type is edit_type)
            for edit_type in EditType
        }
        assert counts[EditType.SUBSTITUTION] == len(query) * (ALPHABET_SIZE - 1)
        assert counts[EditType.DELETION] == len(query)
        assert counts[EditType.INSERTION] == (len(query) + 1) * ALPHABET_SIZE

    def test_empty_query_generates_nothing(self):
        assert raw_repairs(b"") == []
        assert repair_tiers(b"") == []

    def test_policy_flag_is_off_by_default(self):
        assert ALLOW_EMPTY_REPAIR is False


class TestGeneratedPatterns:
    def test_every_pattern_is_over_the_alphabet(self):
        for repair in raw_repairs(b"cat"):
            assert set(repair.pattern) <= set(ALPHABET)

    def test_no_repair_reproduces_the_query(self):
        """Substitution always changes a character; indels change the length. The
        exact match is therefore never confused with a repaired one."""
        query = b"cat"
        assert all(r.pattern != query for r in raw_repairs(query))

    def test_edit_type_determines_pattern_length(self):
        query = b"cat"
        for repair in raw_repairs(query):
            expected = {
                EditType.SUBSTITUTION: len(query),
                EditType.DELETION: len(query) - 1,
                EditType.INSERTION: len(query) + 1,
            }[repair.edit_type]
            assert len(repair.pattern) == expected

    def test_substitution_covers_every_other_character(self):
        produced = {
            r.pattern
            for r in raw_repairs(b"a")
            if r.edit_type is EditType.SUBSTITUTION
        }
        assert produced == {bytes([c]) for c in ALPHABET if c != ord("a")}

    def test_insertion_reaches_the_position_past_the_end(self):
        appended = {
            r.pattern
            for r in raw_repairs(b"ab")
            if r.edit_type is EditType.INSERTION and r.position == 3
        }
        assert b"abx" in appended

    def test_insertion_reaches_the_first_position(self):
        prepended = {
            r.pattern
            for r in raw_repairs(b"ab")
            if r.edit_type is EditType.INSERTION and r.position == 1
        }
        assert b"xab" in prepended

    def test_deletion_removes_each_position(self):
        produced = {
            r.pattern for r in raw_repairs(b"abc") if r.edit_type is EditType.DELETION
        }
        assert produced == {b"bc", b"ac", b"ab"}

    def test_scores_match_the_formulas(self):
        for repair in raw_repairs(b"abcd"):
            expected = {
                EditType.SUBSTITUTION: substitution_score,
                EditType.DELETION: deletion_score,
                EditType.INSERTION: insertion_score,
            }[repair.edit_type](4, repair.position)
            assert repair.score == expected


class TestDeduplication:
    def test_repeated_characters_keep_the_higher_score(self):
        """b"aab" reaches b"ab" by deleting position 1 (penalty 10, generated
        first) or position 2 (penalty 8). The better one must win."""
        scores = best_scores_by_pattern(iter_repairs(b"aab"))
        assert scores[b"ab"] == deletion_score(3, 2) == -4
        assert deletion_score(3, 1) == -6  # the score that must not be kept

    def test_repeated_insertion_keeps_the_higher_score(self):
        """Inserting "a" before or after an existing "a" gives the same string."""
        scores = best_scores_by_pattern(iter_repairs(b"ab"))
        assert scores[b"aab"] == insertion_score(2, 2) == -4
        assert insertion_score(2, 1) == -6

    def test_maximum_is_taken_regardless_of_generation_order(self):
        from autocomplete.scoring import Repair

        low = Repair(b"x", 1, EditType.SUBSTITUTION, 1)
        high = Repair(b"x", 9, EditType.SUBSTITUTION, 2)
        assert best_scores_by_pattern([low, high]) == {b"x": 9}
        assert best_scores_by_pattern([high, low]) == {b"x": 9}

    def test_deduplicated_map_is_smaller_than_the_raw_stream(self):
        query = b"aaa"
        assert len(best_scores_by_pattern(iter_repairs(query))) < len(
            raw_repairs(query)
        )

    def test_every_pattern_appears_in_exactly_one_tier(self):
        query = b"aab"
        scores = best_scores_by_pattern(iter_repairs(query))
        tiers = group_into_tiers(scores)
        flattened = [p for tier in tiers for p in tier.patterns]
        assert len(flattened) == len(set(flattened)) == len(scores)
        assert set(flattened) == set(scores)

    def test_tier_score_matches_the_deduplicated_score(self):
        query = b"aab"
        scores = best_scores_by_pattern(iter_repairs(query))
        for tier in group_into_tiers(scores):
            for pattern in tier.patterns:
                assert scores[pattern] == tier.score


class TestTierOrdering:
    def test_scores_strictly_decrease(self):
        tiers = repair_tiers(b"question")
        scores = [tier.score for tier in tiers]
        assert scores == sorted(scores, reverse=True)
        assert len(scores) == len(set(scores)), "equal scores must share one tier"

    def test_patterns_are_sorted_inside_a_tier(self):
        for tier in repair_tiers(b"cat"):
            assert list(tier.patterns) == sorted(tier.patterns)

    def test_generation_is_deterministic(self):
        assert repair_tiers(b"cat") == repair_tiers(b"cat")

    def test_one_tier_can_mix_all_three_edit_types(self):
        """For a six-character query, substitution at position 2, an extra
        character at position 4 and a missing one at position 3 all score 6, so
        they must land in the same tier."""
        query = b"abcdef"
        assert substitution_score(6, 2) == 6
        assert deletion_score(6, 4) == 6
        assert insertion_score(6, 3) == 6

        tier = next(t for t in repair_tiers(query) if t.score == 6)
        lengths = {len(pattern) for pattern in tier.patterns}
        # One length per edit type: 5 deletion, 6 substitution, 7 insertion.
        assert lengths == {5, 6, 7}

    def test_highest_tier_is_the_best_possible_repair(self):
        tiers = repair_tiers(b"abcdef")
        best_possible = max(
            [substitution_score(6, p) for p in range(1, 7)]
            + [deletion_score(6, p) for p in range(1, 7)]
            + [insertion_score(6, p) for p in range(1, 8)]
        )
        assert tiers[0].score == best_possible

    def test_tier_is_a_value_object(self):
        tier = ScoreTier(4, (b"a", b"b"))
        assert tier == ScoreTier(4, (b"a", b"b"))

    @pytest.mark.parametrize("query", [b"a", b"ab"])
    def test_short_queries_produce_ordered_tiers(self, query):
        tiers = repair_tiers(query)
        assert tiers
        assert [t.score for t in tiers] == sorted(
            (t.score for t in tiers), reverse=True
        )


class TestAgreementWithTheReference:
    """Every generated pattern must really be one edit away, with that score.

    Checked against the independent window-alignment scorer by treating each
    pattern as a sentence that contains it exactly.
    """

    @pytest.mark.parametrize("query", [b"ab", b"abc", b"aab"])
    def test_each_repair_is_structurally_what_it_claims(self, query):
        """The pattern really is the query with that one edit at that position."""
        for repair in iter_repairs(query, b"abc"):
            position = repair.position
            before, after = repair.pattern[: position - 1], repair.pattern[position:]
            if repair.edit_type is EditType.SUBSTITUTION:
                assert before == query[: position - 1]
                assert after == query[position:]
                assert repair.pattern[position - 1] != query[position - 1]
            elif repair.edit_type is EditType.DELETION:
                assert repair.pattern == query[: position - 1] + query[position:]
            else:
                assert before + after == query

    @pytest.mark.parametrize("query", [b"ab", b"abc", b"aab"])
    def test_reference_confirms_every_pattern_is_reachable(self, query):
        """The independent scorer must find a match for each generated pattern,
        and never rate the claimed score as unattainable."""
        for pattern, score in best_scores_by_pattern(
            iter_repairs(query, b"abc")
        ).items():
            reference = best_alignment_score(query, pattern)
            assert reference is not None
            assert reference >= score

    @pytest.mark.parametrize("query", [b"ab", b"abc", b"aab"])
    def test_no_one_edit_match_is_missed(self, query):
        """Any sentence the reference scores must contain a generated pattern."""
        alphabet = b"abc"
        sentences = [
            bytes([a, b, c]) for a in alphabet for b in alphabet for c in alphabet
        ]
        patterns = best_scores_by_pattern(iter_repairs(query, alphabet))
        for sentence in sentences:
            expected = best_alignment_score(query, sentence)
            if expected is None or query in sentence:
                continue
            produced = max(
                (score for pattern, score in patterns.items() if pattern in sentence),
                default=None,
            )
            assert produced == expected, (query, sentence)
