"""Tests for the scoring table and for scoring end to end against a sentence.

The "golden" tests reproduce every scored example printed in the assignment: the
eight in the English appendix and the five in the Hebrew body.
"""

from __future__ import annotations

import itertools

import pytest

from autocomplete.normalize import normalize
from autocomplete.scoring import (
    best_scores_by_pattern,
    deletion_score,
    exact_score,
    indel_penalty,
    insertion_score,
    iter_repairs,
    substitution_penalty,
    substitution_score,
)
from autocomplete.reference import best_alignment_score

APPENDIX_SENTENCE = "To be or not to be, that is the question."


def best_score_against(query: str, sentence: str) -> int | None:
    """Best score for a query against one sentence, using the production pieces.

    Mirrors what the engine will do with the index: check for an exact match,
    otherwise take the best-scoring repaired pattern that occurs in the sentence.
    """
    normalized_query = normalize(query)
    normalized_sentence = normalize(sentence)
    if not normalized_query:
        return None
    if normalized_query in normalized_sentence:
        return exact_score(len(normalized_query))

    best: int | None = None
    scores = best_scores_by_pattern(iter_repairs(normalized_query))
    for pattern, score in scores.items():
        if pattern in normalized_sentence and (best is None or score > best):
            best = score
    return best


class TestPenaltyTable:
    @pytest.mark.parametrize(
        "position,expected",
        [(1, 5), (2, 4), (3, 3), (4, 2), (5, 1), (6, 1), (99, 1)],
    )
    def test_substitution_penalties(self, position, expected):
        assert substitution_penalty(position) == expected

    @pytest.mark.parametrize(
        "position,expected",
        [(1, 10), (2, 8), (3, 6), (4, 4), (5, 2), (6, 2), (99, 2)],
    )
    def test_indel_penalties(self, position, expected):
        assert indel_penalty(position) == expected

    @pytest.mark.parametrize("position", [0, -1])
    def test_positions_are_one_based(self, position):
        with pytest.raises(ValueError, match="1-based"):
            substitution_penalty(position)
        with pytest.raises(ValueError, match="1-based"):
            indel_penalty(position)


class TestScoreFormulas:
    def test_exact_score_is_twice_the_length(self):
        assert exact_score(0) == 0
        assert exact_score(5) == 10
        assert exact_score(11) == 22

    def test_substitution_loses_the_edited_character(self):
        # 5 typed, 4 match, penalty 5 at position 1.
        assert substitution_score(5, 1) == 3

    def test_deletion_loses_the_extra_character(self):
        # 7 typed, 6 match, penalty 4 at position 4.
        assert deletion_score(7, 4) == 8

    def test_insertion_keeps_every_typed_character(self):
        # 5 typed and all match, penalty 2 at position 5.
        assert insertion_score(5, 5) == 8

    def test_insertion_may_land_just_past_the_end(self):
        assert insertion_score(3, 4) == 2 * 3 - indel_penalty(4)

    @pytest.mark.parametrize("scorer", [substitution_score, deletion_score])
    def test_position_must_lie_inside_the_query(self, scorer):
        with pytest.raises(ValueError):
            scorer(3, 4)
        with pytest.raises(ValueError):
            scorer(3, 0)

    def test_insertion_position_must_lie_inside_the_repaired_string(self):
        with pytest.raises(ValueError):
            insertion_score(3, 5)

    def test_scores_can_be_negative(self):
        """TA-DECISION D9: legal, and simply ranked last."""
        assert substitution_score(1, 1) == -5
        assert insertion_score(1, 1) == -8

    def test_exact_beats_every_repair_of_the_same_query(self):
        """The property that lets the engine stop once five exact matches exist."""
        for length in range(1, 12):
            best_repair = max(
                [substitution_score(length, p) for p in range(1, length + 1)]
                + [deletion_score(length, p) for p in range(1, length + 1)]
                + [insertion_score(length, p) for p in range(1, length + 2)]
            )
            assert exact_score(length) > best_repair


class TestAppendixExamples:
    """The eight examples from the English appendix, scored end to end."""

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("To be", 10),
            ("or Not", 12),
            ("be, that", 14),
            ("2o be", 3),
            ("to pe", 6),
            ("or knot", 8),
            ("or nt", 8),
        ],
    )
    def test_scored_examples(self, query, expected):
        assert best_score_against(query, APPENDIX_SENTENCE) == expected

    def test_not_be_does_not_match(self):
        """More than one edit away, so no score is assigned."""
        assert best_score_against("not be", APPENDIX_SENTENCE) is None

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("To be", 10),
            ("or Not", 12),
            ("be, that", 14),
            ("2o be", 3),
            ("to pe", 6),
            ("or knot", 8),
            ("or nt", 8),
            ("not be", None),
        ],
    )
    def test_independent_reference_agrees(self, query, expected):
        assert (
            best_alignment_score(normalize(query), normalize(APPENDIX_SENTENCE))
            == expected
        )


class TestHebrewExamples:
    """The five examples from the Hebrew body of the assignment.

    The sentence is "להיות או לא להיות, זאת השאלה" and the queries are Hebrew, which
    our normalizer strips (the corpus is English). What carries over, and what
    these examples pin down, is the arithmetic: query length, edit type, edit
    position and the resulting score.
    """

    def test_exact_11_characters(self):
        # "להיות או לא" - matches in full.
        assert exact_score(11) == 22

    def test_substitution_at_the_last_position(self):
        # "להיות או לו" - final character wrong; position 11 is in the 5+ bucket.
        assert substitution_score(11, 11) == 19

    def test_substitution_at_position_four(self):
        # "להיןת או לא" - fourth character wrong.
        assert substitution_score(11, 4) == 18

    def test_extra_character_at_position_four(self):
        # "להייות או לא" - 12 typed, one too many at position 4.
        assert deletion_score(12, 4) == 18

    def test_missing_character_at_position_three(self):
        # "להות או לא" - 10 typed and all match, one missing at position 3.
        assert insertion_score(10, 3) == 14


class TestAgainstIndependentReference:
    """Exhaustive comparison on a tiny alphabet.

    Every sentence over {a,b} up to length 6 against every query over {a,b,c} up
    to length 4, scored two entirely different ways: by enumerating repaired
    patterns (production) and by sliding a window (reference).
    """

    def test_exhaustive_agreement(self):
        sentences = [
            bytes(s)
            for length in range(1, 7)
            for s in itertools.product(b"ab", repeat=length)
        ]
        queries = [
            bytes(q)
            for length in range(1, 5)
            for q in itertools.product(b"abc", repeat=length)
        ]
        alphabet = b"abc"

        mismatches = []
        for sentence in sentences:
            for query in queries:
                if query in sentence:
                    produced = exact_score(len(query))
                else:
                    produced = None
                    scores = best_scores_by_pattern(iter_repairs(query, alphabet))
                    for pattern, score in scores.items():
                        if pattern in sentence and (
                            produced is None or score > produced
                        ):
                            produced = score
                expected = best_alignment_score(query, sentence)
                if produced != expected:
                    mismatches.append((query, sentence, produced, expected))

        assert not mismatches, f"{len(mismatches)} mismatches, first: {mismatches[0]}"

    def test_the_comparison_actually_covers_many_cases(self):
        sentences = sum(1 for length in range(1, 7) for _ in range(2**length))
        queries = sum(1 for length in range(1, 5) for _ in range(3**length))
        assert sentences * queries >= 15_000
