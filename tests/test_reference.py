"""Tests for the brute-force reference engine."""

from __future__ import annotations

import pytest

from autocomplete import scoring
from autocomplete.data import AutoCompleteData
from autocomplete.normalize import normalize
from autocomplete.reference import (
    SourceRecord,
    best_alignment_score,
    find_best_k,
    load_records,
    records_from_lines,
)

SHAKESPEARE = "To be or not to be, that is the question."


def score_of(query: str, sentence: str) -> int | None:
    """Best score for a raw query against a raw sentence."""
    return best_alignment_score(normalize(query), normalize(sentence))


def one_line(sentence: str) -> list[SourceRecord]:
    return records_from_lines({"only.txt": [sentence]})


class TestExactMatches:
    def test_whole_sentence(self):
        assert score_of(SHAKESPEARE, SHAKESPEARE) == 2 * len(normalize(SHAKESPEARE))

    def test_at_the_start(self):
        assert score_of("To be", SHAKESPEARE) == 10

    def test_in_the_middle(self):
        assert score_of("or not", SHAKESPEARE) == 12

    def test_at_the_end(self):
        assert score_of("question", SHAKESPEARE) == 16

    def test_single_character(self):
        assert score_of("q", SHAKESPEARE) == 2

    def test_case_and_punctuation_are_ignored(self):
        assert score_of("be, THAT", SHAKESPEARE) == 14

    def test_repeated_spaces_and_tabs_are_ignored(self):
        assert score_of("be \t  that", SHAKESPEARE) == 14


class TestSubstitution:
    @pytest.mark.parametrize(
        "query,position,matching,expected",
        [
            ("2o be", 1, 4, 3),
            ("tX be", 2, 4, 4),
            ("toXbe", 3, 4, 5),
            ("to pe", 4, 4, 6),
            ("to bX", 5, 4, 7),
            ("or nox", 6, 5, 9),
        ],
    )
    def test_position_sets_the_penalty(self, query, position, matching, expected):
        penalty = scoring.substitution_penalty(position)
        assert 2 * matching - penalty == expected
        assert score_of(query, SHAKESPEARE) == expected

    def test_beyond_position_five_the_penalty_is_flat(self):
        """Positions 5 and later all cost one point."""
        assert score_of("questiXn", "question") == 13  # position 7
        assert score_of("questioX", "question") == 13  # position 8


class TestExtraCharacter:
    def test_appendix_example(self):
        assert score_of("or knot", SHAKESPEARE) == 8

    def test_extra_character_at_the_first_position(self):
        """Dropping the first character is the most expensive repair, 10 points.

        Checked against a sentence short enough that no other alignment fits, so
        the deletion really is the only explanation.
        """
        assert score_of("xto be", "to be") == 2 * 5 - 10 == 0

    def test_extra_character_late_in_the_query(self):
        # "the questionx" -> drop the trailing "x" at position 13.
        assert score_of("the questionx", SHAKESPEARE) == 2 * 12 - 2

    def test_a_cheaper_explanation_wins_when_one_exists(self):
        """"xto be" against the full sentence is better explained by turning the
        "x" into a space, since " to be" occurs inside "not to be": that costs 5
        rather than the 10 of dropping a first character."""
        assert score_of("xto be", SHAKESPEARE) == 2 * 5 - 5 == 5


class TestMissingCharacter:
    def test_appendix_example(self):
        assert score_of("or nt", SHAKESPEARE) == 8

    def test_missing_character_in_the_middle(self):
        # "questin" is "question" without the "o" at position 7.
        assert score_of("questin", SHAKESPEARE) == 2 * 7 - 2

    def test_missing_character_at_position_three(self):
        """Synthetic text where putting the character back is the only
        explanation: no window is one substitution away and no deletion fits."""
        assert score_of("abdefgh", "abcdefgh") == 2 * 7 - 6 == 8

    def test_boundary_insertions_are_always_beaten_by_an_exact_match(self):
        """Adding a character at either end can never be the best explanation.

        If ``query + c`` occurs in the sentence then so does ``query`` itself, as
        a prefix of that window, and an exact match scores higher than any
        repair. The same holds for ``c + query``. So a query that is one trailing
        character short of a longer word still scores as the exact match it is.
        """
        assert score_of("questio", SHAKESPEARE) == 2 * 7
        assert score_of("uestio", "question") == 2 * 6


class TestMaximumOverAlignments:
    def test_later_occurrence_may_score_better(self):
        """The first alignment found must not win by default.

        In "xbc abx" the query "abc" differs from the first window at position 1
        (a 5 point penalty) and from the second at position 3 (3 points). Scanning
        left to right meets the worse one first.
        """
        assert score_of("abc", "xbc abx") == 2 * 2 - 3

    def test_repeated_characters_offer_several_edit_positions(self):
        """Dropping either of the first two characters of "aab" gives "ab"; the
        later position is cheaper and must win."""
        assert score_of("aab", "ab") == 2 * 2 - 8

    def test_several_repairs_reach_the_same_sentence(self):
        """"ac" matches "abc" two ways: put the missing "b" back at position 2
        (2x2 - 8 = -4), or change the "c" to a "b" so that "ab" matches
        (2x1 - 4 = -2). The better one must be reported."""
        assert score_of("ac", "abc") == -2

    def test_exact_match_wins_over_a_fuzzy_one_in_the_same_sentence(self):
        assert score_of("abd", "abc abd") == 2 * 3

    def test_several_occurrences_of_an_exact_match_score_once(self):
        assert score_of("to be", SHAKESPEARE) == 10


class TestNonMatches:
    def test_two_edits_away(self):
        assert score_of("not be", SHAKESPEARE) is None

    def test_query_longer_than_the_sentence(self):
        assert score_of("abcdefgh", "abc") is None

    def test_unrelated_text(self):
        assert score_of("zzzz", SHAKESPEARE) is None

    def test_empty_sentence_matches_nothing(self):
        assert best_alignment_score(b"a", b"") is None

    def test_empty_query_matches_nothing(self):
        assert best_alignment_score(b"", b"anything") is None


class TestShortQueries:
    def test_length_one_exact(self):
        assert score_of("q", "question") == 2

    def test_length_one_substitution_scores_negative(self):
        assert score_of("z", "a") == -5

    def test_length_one_missing_character(self):
        assert score_of("b", "ab") == 2  # "b" is already a substring

    def test_excluded_empty_repair_could_never_have_won(self):
        """Decision D9 excludes deleting the only character of a one-character
        query, which would leave the empty string and match every sentence.

        Excluding it costs nothing: any non-empty sentence already matches a
        one-character query by substituting some character it contains, which
        scores at worst 2x0 - 5 = -5, better than the empty repair's
        2x0 - 10 = -10. So the rule spares the index a lookup that returns the
        whole corpus without changing any result.
        """
        assert scoring.ALLOW_EMPTY_REPAIR is False
        assert score_of("z", "qqq") == -5
        assert score_of("z", "a") == -5

    def test_length_two(self):
        assert score_of("to", SHAKESPEARE) == 4
        assert score_of("tX", SHAKESPEARE) == 2 * 1 - 4


class TestPenaltyTablesAgree:
    """The reference restates the penalty numbers to stay independent of
    autocomplete.scoring; this catches the two copies drifting apart."""

    @pytest.mark.parametrize("position", range(1, 10))
    def test_substitution(self, position):
        from autocomplete.reference import _substitution_penalty

        assert _substitution_penalty(position) == scoring.substitution_penalty(position)

    @pytest.mark.parametrize("position", range(1, 10))
    def test_indel(self, position):
        from autocomplete.reference import _indel_penalty

        assert _indel_penalty(position) == scoring.indel_penalty(position)


class TestRecords:
    def test_from_line_normalizes(self):
        record = SourceRecord.from_line("Hello,  World!", "a.txt", 3)
        assert record.completed_sentence == "Hello,  World!"
        assert record.normalized == b"hello world"
        assert record.source_text == "a.txt"
        assert record.offset == 3

    def test_records_from_lines_numbers_from_one(self):
        records = records_from_lines({"a.txt": ["first", "second"]})
        assert [r.offset for r in records] == [1, 2]

    def test_records_from_lines_skips_empty_and_punctuation_only(self):
        records = records_from_lines({"a.txt": ["keep", "", "   ", "!!!", "also"]})
        assert [r.completed_sentence for r in records] == ["keep", "also"]
        # Offsets still refer to the real line numbers.
        assert [r.offset for r in records] == [1, 5]


class TestLoadRecords:
    def test_finds_files_at_every_depth(self, mini_corpus):
        sources = {r.source_text for r in load_records(mini_corpus)}
        assert sources == {"example.txt", "shakespeare.txt", "nested/deep/notes.txt"}

    def test_paths_are_relative_and_posix(self, mini_corpus):
        for record in load_records(mini_corpus):
            assert not record.source_text.startswith("/")
            assert "\\" not in record.source_text

    def test_offsets_are_one_based_line_numbers(self, mini_corpus):
        records = load_records(mini_corpus)
        alpha = next(r for r in records if r.source_text == "example.txt")
        assert alpha.offset == 1
        assert alpha.completed_sentence == "Alpha: this is a demo."

    def test_blank_lines_are_skipped_but_later_offsets_stay_true(self, mini_corpus):
        notes = [
            r for r in load_records(mini_corpus) if r.source_text.endswith("notes.txt")
        ]
        offsets = [r.offset for r in notes]
        assert 5 not in offsets and 6 not in offsets  # the blank lines
        digits = next(r for r in notes if r.completed_sentence.startswith("Digits"))
        assert digits.offset == 8

    def test_original_text_keeps_punctuation_and_case(self, mini_corpus):
        records = load_records(mini_corpus)
        assert any(r.completed_sentence == SHAKESPEARE for r in records)

    def test_is_deterministic(self, mini_corpus):
        assert load_records(mini_corpus) == load_records(mini_corpus)


class TestRanking:
    def test_reproduces_the_assignment_example_session(self, mini_corpus):
        """The worked example: "this is" against the five demo lines."""
        records = [
            r for r in load_records(mini_corpus) if r.source_text == "example.txt"
        ]
        results = find_best_k("this is", records)
        assert results == [
            AutoCompleteData("Alpha: this is a demo.", "example.txt", 1, 14),
            AutoCompleteData("Beta: this is a demo.", "example.txt", 2, 14),
            AutoCompleteData("Delta: this is a demo.", "example.txt", 3, 14),
            AutoCompleteData("Gamma: this is a demo.", "example.txt", 4, 14),
            AutoCompleteData("Omega: this is a demo.", "example.txt", 5, 14),
        ]

    def test_rendered_lines_match_the_assignment_format(self, mini_corpus):
        records = [
            r for r in load_records(mini_corpus) if r.source_text == "example.txt"
        ]
        rendered = [str(item) for item in find_best_k("this is", records)]
        assert rendered[0] == "Alpha: this is a demo. (example.txt:1, score=14)"

    def test_identical_text_in_different_files_stays_separate(self, mini_corpus):
        """The duplicate of example.txt:1 inside nested/deep/notes.txt is its own
        result, ordered after it because the path sorts later."""
        results = find_best_k("this is", load_records(mini_corpus))
        assert results[0] == AutoCompleteData(
            "Alpha: this is a demo.", "example.txt", 1, 14
        )
        assert results[1] == AutoCompleteData(
            "Alpha: this is a demo.", "nested/deep/notes.txt", 3, 14
        )
        assert len(results) == 5

    def test_ties_break_on_the_sentence_then_path_then_offset(self):
        records = records_from_lines(
            {
                "b.txt": ["same text", "other text"],
                "a.txt": ["same text"],
            }
        )
        results = find_best_k("text", records)
        assert [(r.completed_sentence, r.source_text, r.offset) for r in results] == [
            ("other text", "b.txt", 2),
            ("same text", "a.txt", 1),
            ("same text", "b.txt", 1),
        ]

    def test_higher_score_outranks_alphabetical_order(self):
        """"zzy ..." sorts first alphabetically but matches only with a
        substitution, so the exact match must still be ranked above it."""
        records = records_from_lines({"a.txt": ["zzz exact query", "zzy exact query"]})
        results = find_best_k("zzz exact", records)
        assert [(r.completed_sentence, r.score) for r in results] == [
            ("zzz exact query", 18),
            ("zzy exact query", 13),
        ]

    def test_each_record_appears_once(self):
        records = records_from_lines({"a.txt": ["to be or not to be"]})
        results = find_best_k("to be", records)
        assert len(results) == 1
        assert results[0].score == 10

    def test_fewer_than_five_matches_returns_what_exists(self, mini_corpus):
        results = find_best_k("perchance", load_records(mini_corpus))
        assert len(results) == 1
        assert results[0].completed_sentence == "To sleep, perchance to dream."

    def test_no_matches_returns_empty(self, mini_corpus):
        assert find_best_k("zzzzzzzz", load_records(mini_corpus)) == []

    def test_empty_query_returns_empty(self, mini_corpus):
        assert find_best_k("", load_records(mini_corpus)) == []

    def test_query_that_normalizes_away_returns_empty(self, mini_corpus):
        assert find_best_k("!!! ???", load_records(mini_corpus)) == []

    def test_negative_scores_are_returned_and_rank_last(self):
        records = records_from_lines({"a.txt": ["a", "z"]})
        results = find_best_k("z", records)
        assert results[0] == AutoCompleteData("z", "a.txt", 2, 2)
        assert results[1] == AutoCompleteData("a", "a.txt", 1, -5)

    def test_k_is_respected(self, mini_corpus):
        records = load_records(mini_corpus)
        assert len(find_best_k("this is", records, k=2)) == 2
        assert find_best_k("this is", records, k=0) == []

    def test_k_larger_than_the_match_count(self, mini_corpus):
        results = find_best_k("perchance", load_records(mini_corpus), k=99)
        assert len(results) == 1

    def test_negative_k_is_rejected(self):
        with pytest.raises(ValueError, match="k must be >= 0"):
            find_best_k("a", [], k=-1)

    def test_matches_at_the_very_start_and_end_of_a_sentence(self):
        records = records_from_lines({"a.txt": ["alpha beta gamma"]})
        assert find_best_k("alpha", records)[0].score == 10
        assert find_best_k("gamma", records)[0].score == 10

    def test_case_punctuation_tabs_and_spacing_in_the_query(self, mini_corpus):
        records = load_records(mini_corpus)
        expected = find_best_k("mixed case", records)
        for variant in ["MIXED   case", "Mixed,\tCase", "  mixed case  "]:
            assert find_best_k(variant, records) == expected
        assert expected[0].completed_sentence == "Mixed   CASE and\ttabs inside a line."
