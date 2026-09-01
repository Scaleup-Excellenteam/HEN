"""Tests for searching a base index alongside an overlay.

The claim under test is the one the whole overlay architecture rests on: asking
each index for K and merging gives exactly what ranking every record of both
together would give. Most of these therefore compare the composite search
against :func:`autocomplete.reference.find_best_k` over the *union*, which is
brute force and shares no reasoning with the engine.
"""

from __future__ import annotations

import pytest

from autocomplete import composite
from autocomplete.data import AutoCompleteData
from autocomplete.engine import find_completions
from autocomplete.index import SearchIndex
from autocomplete.reference import find_best_k, records_from_lines

K = 5


def build(tmp_path, name: str, files: dict[str, list[str]]) -> SearchIndex:
    root = tmp_path / name
    for relative, lines in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return SearchIndex.build(root, summary_width=K)


def oracle(query: str, *file_sets: dict[str, list[str]], k: int = K):
    """The true answer over the union, computed by the brute-force ranker."""
    records = []
    for files in file_sets:
        records.extend(records_from_lines(files))
    return find_best_k(query, records, k)


def same(actual, expected) -> None:
    """Compare complete result lists field by field, in order."""
    assert [
        (
            item.completed_sentence,
            item.source_text,
            item.offset,
            item.score,
        )
        for item in actual
    ] == [
        (
            item.completed_sentence,
            item.source_text,
            item.offset,
            item.score,
        )
        for item in expected
    ]


BASE_FILES = {
    "corpus.txt": [
        "the quick brown fox",
        "the quick brown dog",
        "a sentence about indexing",
        "another line entirely",
    ]
}
DRIVE_FILES = {
    "Google Drive/notes.txt": [
        "the quick brown cat",
        "the quick brown bird",
        "imported notes about indexing",
    ]
}


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> SearchIndex:
    return build(tmp_path_factory.mktemp("base"), "corpus", BASE_FILES)


@pytest.fixture(scope="module")
def overlay(tmp_path_factory) -> SearchIndex:
    return build(tmp_path_factory.mktemp("overlay"), "sources", DRIVE_FILES)


class TestWithoutAnOverlay:
    def test_it_is_the_engine_and_nothing_else(self, base):
        for query in ["the quick", "indexing", "fox", "zzz", "the quik brown"]:
            same(composite.search(base, None, query), find_completions(base, query))

    def test_the_limit_is_passed_through(self, base):
        assert len(composite.search(base, None, "the quick", 2)) == 2

    def test_it_returns_the_engine_s_own_list_object_shape(self, base):
        results = composite.search(base, None, "the quick")
        assert all(isinstance(item, AutoCompleteData) for item in results)


class TestGlobalRanking:
    @pytest.mark.parametrize(
        "query",
        [
            "the quick brown",
            "indexing",
            "the quick brown fox",
            "imported notes",
            "another line",
            "the quik brown",
            "brown",
            "a",
            "nothing matches this at all",
        ],
    )
    def test_it_matches_a_brute_force_ranker_over_the_union(self, base, overlay, query):
        same(
            composite.search(base, overlay, query),
            oracle(query, BASE_FILES, DRIVE_FILES),
        )

    def test_results_are_ordered_by_the_project_s_ranking_key(self, base, overlay):
        results = composite.search(base, overlay, "the quick brown")
        keys = [result.ranking_key for result in results]
        assert keys == sorted(keys)

    def test_at_most_k_results_come_back(self, base, overlay):
        assert len(composite.search(base, overlay, "the quick brown")) <= K

    def test_the_answer_does_not_depend_on_the_run(self, base, overlay):
        first = composite.search(base, overlay, "the quick brown")
        for _ in range(5):
            same(composite.search(base, overlay, "the quick brown"), first)

    def test_imported_results_do_not_automatically_outrank_the_corpus(
        self, base, overlay
    ):
        """"fox" only exists in the corpus, so nothing imported may displace
        it."""
        results = composite.search(base, overlay, "quick brown fox")
        assert results[0].source_text == "corpus.txt"

    def test_results_are_not_grouped_by_corpus(self, base, overlay):
        sources = [
            result.source_text.startswith("Google Drive/")
            for result in composite.search(base, overlay, "the quick brown")
        ]
        # The four "the quick brown ..." lines tie on score, so alphabetical
        # order of the sentence interleaves the two corpora: bird, cat, dog, fox.
        assert sources == [True, True, False, False]


class TestWinnerDistribution:
    """Every way the K winners can be split between the two indexes."""

    def test_every_winner_from_the_base(self, tmp_path):
        base_files = {"corpus.txt": [f"alpha result {n}" for n in range(8)]}
        drive_files = {"Google Drive/d.txt": ["something else entirely"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        results = composite.search(base, overlay, "alpha result")
        assert all(item.source_text == "corpus.txt" for item in results)
        same(results, oracle("alpha result", base_files, drive_files))

    def test_every_winner_from_drive(self, tmp_path):
        base_files = {"corpus.txt": ["something else entirely"]}
        drive_files = {"Google Drive/d.txt": [f"alpha result {n}" for n in range(8)]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        results = composite.search(base, overlay, "alpha result")
        assert all(item.source_text.startswith("Google Drive/") for item in results)
        same(results, oracle("alpha result", base_files, drive_files))

    def test_winners_alternate_between_the_two(self, tmp_path):
        base_files = {"corpus.txt": ["match a", "match c", "match e", "match g"]}
        drive_files = {"Google Drive/d.txt": ["match b", "match d", "match f"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        results = composite.search(base, overlay, "match")
        assert [item.completed_sentence for item in results] == [
            "match a",
            "match b",
            "match c",
            "match d",
            "match e",
        ]
        same(results, oracle("match", base_files, drive_files))

    def test_k_split_across_both_indexes(self, tmp_path):
        base_files = {"corpus.txt": ["shared aa", "shared cc"]}
        drive_files = {"Google Drive/d.txt": ["shared bb", "shared dd", "shared ee"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        same(
            composite.search(base, overlay, "shared"),
            oracle("shared", base_files, drive_files),
        )

    def test_fewer_than_k_results_in_total(self, tmp_path):
        base_files = {"corpus.txt": ["only one here"]}
        drive_files = {"Google Drive/d.txt": ["only two here"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        results = composite.search(base, overlay, "only")
        assert len(results) == 2
        same(results, oracle("only", base_files, drive_files))

    def test_more_than_k_candidates_in_each_index(self, tmp_path):
        base_files = {"corpus.txt": [f"common line {n:02d}" for n in range(20)]}
        drive_files = {"Google Drive/d.txt": [f"common line {n:02d}b" for n in range(20)]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        same(
            composite.search(base, overlay, "common line"),
            oracle("common line", base_files, drive_files),
        )


class TestTiesAcrossIndexes:
    def test_a_score_tie_spanning_both_indexes(self, tmp_path):
        base_files = {"corpus.txt": ["tie beta", "tie delta"]}
        drive_files = {"Google Drive/d.txt": ["tie alpha", "tie charlie"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        results = composite.search(base, overlay, "tie")
        assert len({item.score for item in results}) == 1
        assert [item.completed_sentence for item in results] == [
            "tie alpha",
            "tie beta",
            "tie charlie",
            "tie delta",
        ]
        same(results, oracle("tie", base_files, drive_files))

    def test_identical_sentences_in_different_sources_both_survive(self, tmp_path):
        base_files = {"corpus.txt": ["exactly the same line"]}
        drive_files = {"Google Drive/d.txt": ["exactly the same line"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        results = composite.search(base, overlay, "exactly the same")
        assert len(results) == 2
        assert {item.source_text for item in results} == {
            "corpus.txt",
            "Google Drive/d.txt",
        }
        same(results, oracle("exactly the same", base_files, drive_files))

    def test_identical_sentences_inside_one_source_both_survive(self, tmp_path):
        base_files = {"corpus.txt": ["repeated line", "repeated line"]}
        drive_files = {"Google Drive/d.txt": ["repeated line"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        results = composite.search(base, overlay, "repeated")
        assert len(results) == 3
        same(results, oracle("repeated", base_files, drive_files))

    def test_a_record_reachable_through_several_repairs_keeps_its_best_score(
        self, tmp_path
    ):
        base_files = {"corpus.txt": ["aab test"]}
        drive_files = {"Google Drive/d.txt": ["aab test too"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        same(composite.search(base, overlay, "ab test"), oracle("ab test", base_files, drive_files))

    def test_an_exact_tie_on_every_field_is_settled_by_the_answer_order(self):
        """The last tie the ranking key cannot break is decided by which answer
        was listed first, which the caller sets to the base corpus."""
        first = AutoCompleteData("same", "same.txt", 1, 10)
        second = AutoCompleteData("same", "same.txt", 1, 10)
        merged = composite.merge([[first], [second]], limit=2)
        assert merged[0] is first and merged[1] is second


class TestMerge:
    def test_it_keeps_only_the_limit(self):
        answers = [
            [AutoCompleteData(f"a{n}", "x.txt", n, 10) for n in range(5)],
            [AutoCompleteData(f"b{n}", "y.txt", n, 10) for n in range(5)],
        ]
        assert len(composite.merge(answers, limit=5)) == 5

    def test_a_zero_limit_returns_nothing(self):
        assert composite.merge([[AutoCompleteData("a", "x", 1, 2)]], limit=0) == []

    def test_empty_answers_merge_to_nothing(self):
        assert composite.merge([[], []], limit=5) == []

    def test_score_is_the_primary_key(self):
        low = AutoCompleteData("aaa", "x.txt", 1, 4)
        high = AutoCompleteData("zzz", "y.txt", 1, 40)
        assert composite.merge([[low], [high]], limit=2) == [high, low]

    def test_one_answer_alone_is_returned_in_order(self):
        answer = [AutoCompleteData("a", "x.txt", 1, 10), AutoCompleteData("b", "x.txt", 2, 8)]
        assert composite.merge([answer], limit=5) == answer


class TestScoresAreIndexIndependent:
    def test_the_same_sentence_scores_the_same_in_either_index(self, tmp_path):
        """Step 1 of the proof, checked directly: moving a record between
        indexes cannot change what it scores."""
        lines = ["the quick brown fox jumps"]
        as_base = build(tmp_path, "one", {"f.txt": lines})
        as_overlay = build(tmp_path, "two", {"f.txt": lines})
        for query in ["quick brown", "quik brown", "jumps", "the quick brown fox"]:
            left = find_completions(as_base, query)
            right = find_completions(as_overlay, query)
            assert [item.score for item in left] == [item.score for item in right]


class TestTheExactTierShortcut:
    """The shortcut that settles a query without enumerating any repair.

    Its correctness is already implied by the differential suite, which compares
    complete answers against a brute-force ranker. These pin it directly, so a
    fault in it is reported as itself rather than as a mismatch somewhere.
    """

    def test_it_answers_exactly_as_the_full_walk_would(self, tmp_path):
        base_files = {"corpus.txt": [f"the quick brown thing {n}" for n in range(8)]}
        drive_files = {"Google Drive/d.txt": [f"the quick brown other {n}" for n in range(8)]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)

        for query in ["the quick brown", "the quick", "brown", "quick brown other"]:
            shortcut = composite.search(base, overlay, query)
            full = composite.merge(
                [
                    find_completions(base, query, K),
                    find_completions(overlay, query, K),
                ],
                limit=K,
            )
            same(shortcut, full)
            same(shortcut, oracle(query, base_files, drive_files))

    def test_it_takes_the_shortcut_when_the_exact_tier_fills(self, tmp_path, monkeypatch):
        """No repair may be enumerated when K exact matches already exist: that
        is the whole point, and it is what keeps typing as cheap as it was."""
        base_files = {"corpus.txt": [f"exact match {n}" for n in range(8)]}
        drive_files = {"Google Drive/d.txt": ["exact match imported"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)

        import autocomplete.engine as engine

        monkeypatch.setattr(
            engine,
            "_fuzzy_tiers",
            lambda query: pytest.fail("repairs were enumerated for an exact answer"),
        )
        assert len(composite.search(base, overlay, "exact match")) == K

    def test_it_does_not_take_the_shortcut_when_the_exact_tier_cannot_fill(
        self, tmp_path
    ):
        base_files = {"corpus.txt": ["only one exact", "a near mist"]}
        drive_files = {"Google Drive/d.txt": ["another near mise"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        # Neither line contains "near miss", and each is one substitution away
        # from it, so the shortcut cannot fill and the repair walk must run.
        results = composite.search(base, overlay, "near miss")
        assert len(results) == 2
        same(results, oracle("near miss", base_files, drive_files))

    def test_an_exact_match_only_in_the_overlay_still_wins_its_place(self, tmp_path):
        base_files = {"corpus.txt": [f"zzz filler {n}" for n in range(8)]}
        drive_files = {"Google Drive/d.txt": ["shared phrase here"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        results = composite.search(base, overlay, "shared phrase")
        assert results[0].source_text == "Google Drive/d.txt"
        same(results, oracle("shared phrase", base_files, drive_files))

    def test_the_shortcut_respects_a_lowered_limit(self, tmp_path):
        base_files = {"corpus.txt": [f"exact match {n}" for n in range(8)]}
        drive_files = {"Google Drive/d.txt": ["exact match imported"]}
        base = build(tmp_path, "b", base_files)
        overlay = build(tmp_path, "o", drive_files)
        for limit in (1, 2, 3, 4, 5):
            results = composite.search(base, overlay, "exact match", limit)
            assert len(results) == limit
            same(results, oracle("exact match", base_files, drive_files, k=limit))

    def test_a_query_that_normalizes_away_is_still_empty(self, base, overlay):
        for query in ["", "   ", "!!!", "\t"]:
            assert composite.search(base, overlay, query) == []

    def test_no_repair_can_reach_the_exact_score(self):
        """The premise the shortcut rests on, checked against the scoring table
        rather than assumed."""
        from autocomplete.scoring import exact_score, repair_tiers

        for length in range(2, 12):
            query = b"a" * length
            best_repair = max(tier.score for tier in repair_tiers(query))
            assert best_repair < exact_score(length), length
