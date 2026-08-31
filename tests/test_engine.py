"""Tests for the completion engine: the tier walk and the public API."""

from __future__ import annotations

from typing import List

import pytest

import autocomplete
from autocomplete import engine
from autocomplete.config import Config
from autocomplete.data import AutoCompleteData
from autocomplete.engine import find_completions
from autocomplete.index import SearchIndex
from autocomplete.normalize import normalize
from autocomplete.reference import find_best_k, load_records
from autocomplete.scoring import (
    deletion_score,
    exact_score,
    insertion_score,
    substitution_score,
)

WIDTH = 5


def write_corpus(root, files: dict[str, bytes]):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def index_for(root, width: int = WIDTH) -> SearchIndex:
    return SearchIndex.build(root, summary_width=width)


def corpus_index(tmp_path, files: dict[str, bytes], width: int = WIDTH):
    """A corpus directory and an index over it."""
    root = write_corpus(tmp_path, files)
    return root, index_for(root, width)


def reference_answer(root, query: str, k: int = WIDTH) -> list[AutoCompleteData]:
    """What the independent brute-force engine returns."""
    return find_best_k(query, load_records(root), k)


@pytest.fixture(scope="module")
def fixture_root(pytestconfig):
    return pytestconfig.rootpath / "tests" / "fixtures" / "mini_corpus"


@pytest.fixture(scope="module")
def fixture_index(fixture_root):
    return index_for(fixture_root)


class TestExactTier:
    def test_exact_matches_only(self, fixture_index, fixture_root):
        assert find_completions(fixture_index, "this is") == reference_answer(
            fixture_root, "this is"
        )

    def test_a_full_exact_tier_settles_the_answer(self, fixture_index):
        results = find_completions(fixture_index, "this is")
        assert len(results) == WIDTH
        assert {r.score for r in results} == {exact_score(len(normalize("this is")))}

    def test_more_than_k_matches_in_the_top_tier(self, tmp_path):
        files = {"a.txt": b"\n".join(f"line {i} match".encode() for i in range(20))}
        root, index = corpus_index(tmp_path, files)
        results = find_completions(index, "match")
        assert len(results) == WIDTH
        assert results == reference_answer(root, "match")

    def test_one_record_matching_many_times_appears_once(self, tmp_path):
        root, index = corpus_index(
            tmp_path, {"a.txt": b"the cat the hat the bat the mat\n"}
        )
        results = find_completions(index, "the")
        assert len(results) == 1
        assert results[0].score == exact_score(3)


class TestFuzzyTiers:
    def test_no_exact_match_but_a_substitution_matches(self, tmp_path):
        root, index = corpus_index(tmp_path, {"a.txt": b"alpha bravo charlie\n"})
        results = find_completions(index, "alpha brxvo")
        assert results == reference_answer(root, "alpha brxvo")
        assert results[0].score == substitution_score(11, 9)

    def test_an_extra_typed_character_is_dropped(self, tmp_path):
        root, index = corpus_index(tmp_path, {"a.txt": b"alpha bravo charlie\n"})
        results = find_completions(index, "alpha brravo")
        assert results == reference_answer(root, "alpha brravo")
        assert results[0].score == deletion_score(12, 8)

    def test_a_missing_character_is_restored(self, tmp_path):
        root, index = corpus_index(tmp_path, {"a.txt": b"alpha bravo charlie\n"})
        results = find_completions(index, "alpha bavo")
        assert results == reference_answer(root, "alpha bavo")
        assert results[0].score == insertion_score(10, 8)

    def test_results_spread_across_several_tiers(self, tmp_path):
        """One sentence matches exactly, others only after edits of differing
        cost, so the answer is assembled from several tiers."""
        root, index = corpus_index(
            tmp_path,
            {
                "a.txt": (
                    b"query text here\n"  # exact
                    b"qxery text here\n"  # substitution at position 2
                    b"qzzery text here\n"  # unrelated
                    b"uery text here\n"  # missing first character
                )
            },
        )
        results = find_completions(index, "query text")
        assert results == reference_answer(root, "query text")
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)
        assert len(set(scores)) > 1, "the answer should span more than one tier"

    def test_a_lower_tier_never_displaces_a_higher_one(self, tmp_path):
        """"zzy" sorts before "zzz" alphabetically but only matches after a
        substitution, so it must still rank below the exact match."""
        root, index = corpus_index(
            tmp_path, {"a.txt": b"zzy target here\nzzz target here\n"}
        )
        results = find_completions(index, "zzz target")
        assert [(r.completed_sentence, r.score) for r in results] == [
            ("zzz target here", 20),
            ("zzy target here", 15),
        ]
        assert results == reference_answer(root, "zzz target")

    def test_the_same_record_reached_by_several_patterns_appears_once(self, tmp_path):
        """"aab" reaches "ab" by deleting either of the first two characters."""
        root, index = corpus_index(tmp_path, {"a.txt": b"ab\n"})
        results = find_completions(index, "aab")
        assert len(results) == 1
        assert results == reference_answer(root, "aab")

    def test_overlapping_pattern_ranges_within_one_tier(self, tmp_path):
        """Several patterns of one tier match overlapping sets of sentences; the
        winners must come from the merged set, not from the first pattern."""
        files = {
            "a.txt": b"\n".join(
                [b"xbc here", b"axc here", b"abx here", b"abc here", b"abd here"]
            )
            + b"\n"
        }
        root, index = corpus_index(tmp_path, files)
        for query in ["abc here", "abz here", "abc herx"]:
            assert find_completions(index, query) == reference_answer(root, query)

    def test_a_record_only_reachable_by_a_later_pattern_in_the_tier(self, tmp_path):
        """The best record in a tier need not come from the first pattern tried;
        patterns are sorted, so "zz..." is examined last."""
        root, index = corpus_index(
            tmp_path, {"a.txt": b"aaa zebra\nzzz zebra\n"}
        )
        # "zebrx" needs a substitution; both sentences match, and the merged
        # answer must be ordered by sentence, not by which pattern found it.
        assert find_completions(index, "zebrx") == reference_answer(root, "zebrx")

    def test_higher_tier_records_are_excluded_from_later_tiers(self, tmp_path):
        """A sentence matching both exactly and after an edit keeps the better
        score and is not offered twice."""
        root, index = corpus_index(tmp_path, {"a.txt": b"abc abd\n"})
        results = find_completions(index, "abd")
        assert len(results) == 1
        assert results[0].score == exact_score(3)
        assert results == reference_answer(root, "abd")


class TestResultShape:
    def test_fewer_than_k_results(self, tmp_path):
        root, index = corpus_index(tmp_path, {"a.txt": b"only one match\n"})
        results = find_completions(index, "only one")
        assert len(results) == 1
        assert results == reference_answer(root, "only one")

    def test_no_results(self, fixture_index, fixture_root):
        assert find_completions(fixture_index, "zzzzqqqq") == []
        assert reference_answer(fixture_root, "zzzzqqqq") == []

    @pytest.mark.parametrize("query", ["", "   ", "!!!", ",.;", "\t"])
    def test_degenerate_input_returns_nothing(self, fixture_index, query):
        assert find_completions(fixture_index, query) == []

    def test_every_field_is_populated(self, fixture_index):
        result = find_completions(fixture_index, "perchance")[0]
        assert result == AutoCompleteData(
            "To sleep, perchance to dream.", "shakespeare.txt", 3, 18
        )

    def test_no_duplicate_records(self, fixture_index):
        for query in ["this is", "thi is", "demo", "e", "a"]:
            results = find_completions(fixture_index, query)
            seen = [(r.source_text, r.offset) for r in results]
            assert len(seen) == len(set(seen))

    def test_results_are_ordered_by_score_then_tie_break(self, fixture_index):
        for query in ["this is", "thi is", "demo", "to be"]:
            results = find_completions(fixture_index, query)
            assert results == sorted(results, key=lambda item: item.ranking_key)

    def test_limit_is_respected(self, fixture_index):
        assert len(find_completions(fixture_index, "demo", limit=2)) == 2
        assert find_completions(fixture_index, "demo", limit=0) == []

    def test_asking_beyond_the_summary_width_is_refused(self, fixture_index):
        with pytest.raises(ValueError, match="built to answer"):
            find_completions(fixture_index, "demo", limit=WIDTH + 1)

    @pytest.mark.parametrize("width", [1, 2, 3, 7])
    def test_other_result_counts(self, tmp_path, width):
        files = {"a.txt": b"\n".join(f"row {i} target".encode() for i in range(12))}
        root, index = corpus_index(tmp_path, files, width=width)
        results = find_completions(index, "target")
        assert len(results) == width
        assert results == reference_answer(root, "target", k=width)


class TestNormalizationInQueries:
    def test_case_punctuation_and_spacing_are_ignored(self, fixture_index):
        expected = find_completions(fixture_index, "this is a demo")
        for variant in ["THIS IS A DEMO", "this  is a  demo", "This is, a demo!"]:
            assert find_completions(fixture_index, variant) == expected
        assert expected

    def test_tabs_behave_as_spaces(self, fixture_index):
        assert find_completions(fixture_index, "this\tis") == find_completions(
            fixture_index, "this is"
        )

    def test_digits_are_searchable(self, fixture_index, fixture_root):
        assert find_completions(fixture_index, "123") == reference_answer(
            fixture_root, "123"
        )

    def test_bytes_and_str_agree(self, fixture_index):
        assert find_completions(fixture_index, "this is") == find_completions(
            fixture_index, b"this is"
        )


class TestQueryLengths:
    def test_single_character_query(self, fixture_index, fixture_root):
        assert find_completions(fixture_index, "q") == reference_answer(
            fixture_root, "q"
        )

    def test_two_character_query(self, fixture_index, fixture_root):
        assert find_completions(fixture_index, "th") == reference_answer(
            fixture_root, "th"
        )

    def test_repeated_character_query(self, tmp_path):
        root, index = corpus_index(tmp_path, {"a.txt": b"aaaa\naaa\naa\n"})
        for query in ["aaa", "aaaa", "aaaaa", "aab"]:
            assert find_completions(index, query) == reference_answer(root, query)

    def test_a_query_as_long_as_the_longest_sentence(self, tmp_path):
        sentence = b"a" * 40
        root, index = corpus_index(tmp_path, {"a.txt": sentence + b"\n"})
        assert index.records.max_record_length == 40
        assert len(find_completions(index, "a" * 40)) == 1

    def test_a_query_one_longer_than_any_sentence_can_still_match(self, tmp_path):
        """Dropping the extra character leaves something that fits, so this is a
        match rather than an early rejection."""
        root, index = corpus_index(tmp_path, {"a.txt": b"a" * 40 + b"\n"})
        results = find_completions(index, "a" * 41)
        assert len(results) == 1
        assert results == reference_answer(root, "a" * 41)

    def test_a_query_two_longer_than_any_sentence_cannot_match(self, tmp_path):
        root, index = corpus_index(tmp_path, {"a.txt": b"a" * 40 + b"\n"})
        assert find_completions(index, "a" * 42) == []

    def test_a_very_long_query_terminates_quickly(self, fixture_index):
        assert find_completions(fixture_index, "z" * 5000) == []


class TestBoundaryInsertionSkipping:
    """Insertions at either end of the query are skipped as provably useless.

    If ``c + query`` occurs in a sentence then ``query`` occurs there too, so
    that sentence already matched exactly at a higher score. These tests check
    the shortcut changes nothing.
    """

    QUERIES = [
        "this is",
        "demo",
        "thi is",
        "q",
        "ab",
        "aaa",
        "to be or nt",
        "zzzz",
        "1",
        "digits 12",
    ]

    @pytest.mark.parametrize("query", QUERIES)
    def test_results_are_the_same_without_the_shortcut(
        self, fixture_index, monkeypatch, query
    ):
        with_shortcut = find_completions(fixture_index, query)
        monkeypatch.setattr(engine, "_inserts_at_an_end", lambda repair, length: False)
        without_shortcut = find_completions(fixture_index, query)
        assert with_shortcut == without_shortcut

    def test_the_shortcut_actually_removes_patterns(self):
        from autocomplete.scoring import ALPHABET, iter_repairs

        query = b"abc"
        kept = [
            repair
            for repair in iter_repairs(query)
            if not engine._inserts_at_an_end(repair, len(query))
        ]
        assert len(kept) == len(list(iter_repairs(query))) - 2 * len(ALPHABET)

    def test_an_end_insertion_only_ever_names_an_exact_match(self, tmp_path):
        """"bc" is one insertion away from "abc", and every sentence holding
        "abc" holds "bc" already, so the exact tier has it first."""
        root, index = corpus_index(tmp_path, {"a.txt": b"abc\n"})
        results = find_completions(index, "bc")
        assert results[0].score == exact_score(2)
        assert results == reference_answer(root, "bc")


class TestPublicApi:
    @pytest.fixture
    def configured(self, tmp_path):
        root = write_corpus(
            tmp_path / "corpus",
            {"a.txt": b"Alpha: this is a demo.\nBeta: this is a demo.\n"},
        )
        config = Config(corpus_root=root, cache_dir=tmp_path / "cache")
        autocomplete.reset_default_index()
        autocomplete.get_default_index(config)
        yield root
        autocomplete.reset_default_index()

    def test_returns_completions(self, configured):
        results = autocomplete.get_best_k_completions("this is")
        assert [r.completed_sentence for r in results] == [
            "Alpha: this is a demo.",
            "Beta: this is a demo.",
        ]
        assert all(isinstance(r, AutoCompleteData) for r in results)

    def test_corrects_a_typo(self, configured):
        results = autocomplete.get_best_k_completions("thsi is a demo")
        assert results == find_best_k("thsi is a demo", load_records(configured), 5)

    def test_honours_the_configured_result_count(self, tmp_path):
        root = write_corpus(
            tmp_path / "corpus",
            {"a.txt": b"\n".join(f"row {i} demo".encode() for i in range(9))},
        )
        autocomplete.reset_default_index()
        try:
            autocomplete.get_default_index(
                Config(corpus_root=root, cache_dir=tmp_path / "cache", num_results=3)
            )
            assert len(autocomplete.get_best_k_completions("demo")) == 3
        finally:
            autocomplete.reset_default_index()

    def test_empty_query_returns_nothing(self, configured):
        assert autocomplete.get_best_k_completions("") == []

    def test_the_index_is_prepared_once(self, configured):
        first = autocomplete.get_default_index()
        assert autocomplete.get_default_index() is first

    def test_reset_forgets_the_index(self, configured):
        autocomplete.reset_default_index()
        assert autocomplete._default_index is None

    def test_signature_matches_the_assignment(self):
        import inspect

        hints = inspect.get_annotations(
            autocomplete.get_best_k_completions, eval_str=True
        )
        signature = inspect.signature(autocomplete.get_best_k_completions)
        assert list(signature.parameters) == ["prefix"]
        assert hints["prefix"] is str
        assert hints["return"] == List[AutoCompleteData]
