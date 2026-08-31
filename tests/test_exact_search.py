"""Tests for the exact-match search path.

The expected answers come from scanning the sentences for the query, with no
suffix array and no block summaries involved, so agreement is evidence that the
index returns what a direct search would.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from autocomplete.data import AutoCompleteData
from autocomplete.engine import exact_completions
from autocomplete.index import SearchIndex
from autocomplete.normalize import normalize
from autocomplete.reference import load_records
from autocomplete.scoring import exact_score

WIDTH = 5


def write_corpus(root, files: dict[str, bytes]):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def index_for(root, width: int = WIDTH) -> SearchIndex:
    return SearchIndex.build(root, summary_width=width)


def brute_force_exact(root, query: str, limit: int = WIDTH) -> list[AutoCompleteData]:
    """The answer computed by reading the sentences directly."""
    normalized = normalize(query)
    if not normalized:
        return []
    score = exact_score(len(normalized))
    matches = [
        AutoCompleteData(
            record.completed_sentence, record.source_text, record.offset, score
        )
        for record in load_records(root)
        if normalized in record.normalized
    ]
    matches.sort(key=lambda item: item.ranking_key)
    return matches[:limit]


@pytest.fixture(scope="module")
def fixture_index(pytestconfig):
    return index_for(pytestconfig.rootpath / "tests" / "fixtures" / "mini_corpus")


@pytest.fixture(scope="module")
def fixture_root(pytestconfig):
    return pytestconfig.rootpath / "tests" / "fixtures" / "mini_corpus"


class TestAgainstTheFixtureCorpus:
    @pytest.mark.parametrize(
        "query",
        [
            "this is",
            "demo",
            "to be",
            "question",
            "Mixed",
            "digits 123",
            "a",
            "e",
            " ",
            "perchance",
            "zzzznotpresent",
            "",
            "!!!",
            "THIS IS",
            "this   is",
            "this is,",
        ],
    )
    def test_matches_a_direct_scan(self, fixture_index, fixture_root, query):
        assert exact_completions(fixture_index, query) == brute_force_exact(
            fixture_root, query
        )

    def test_reproduces_the_worked_example(self, fixture_index):
        results = exact_completions(fixture_index, "this is")
        assert results[0] == AutoCompleteData(
            "Alpha: this is a demo.", "example.txt", 1, 14
        )
        assert [item.score for item in results] == [14] * 5

    def test_every_field_is_carried_through(self, fixture_index):
        result = exact_completions(fixture_index, "perchance")[0]
        assert result == AutoCompleteData(
            "To sleep, perchance to dream.", "shakespeare.txt", 3, 18
        )

    def test_original_punctuation_and_case_are_preserved(self, fixture_index):
        result = exact_completions(fixture_index, "to be or not")[0]
        assert result.completed_sentence == "To be or not to be, that is the question."

    def test_source_paths_are_relative_and_posix(self, fixture_index):
        for result in exact_completions(fixture_index, "demo"):
            assert not result.source_text.startswith("/")
            assert "\\" not in result.source_text

    def test_offsets_are_one_based(self, fixture_index):
        assert all(
            result.offset >= 1 for result in exact_completions(fixture_index, "demo")
        )


class TestResultShape:
    def test_no_results_for_an_empty_query(self, fixture_index):
        assert exact_completions(fixture_index, "") == []

    def test_no_results_when_the_query_normalizes_away(self, fixture_index):
        assert exact_completions(fixture_index, "!!! ???") == []

    def test_no_results_for_an_absent_query(self, fixture_index):
        assert exact_completions(fixture_index, "nowhere in the corpus") == []

    def test_no_results_when_the_query_is_longer_than_any_sentence(self, fixture_index):
        longest = fixture_index.records.max_record_length
        assert exact_completions(fixture_index, "a" * (longest + 1)) == []

    def test_fewer_matches_than_asked_for(self, fixture_index):
        assert len(exact_completions(fixture_index, "perchance")) == 1

    def test_more_matches_than_asked_for_are_capped(self, fixture_index):
        assert len(exact_completions(fixture_index, "demo")) == WIDTH

    def test_limit_is_respected(self, fixture_index):
        assert len(exact_completions(fixture_index, "demo", limit=2)) == 2

    def test_limit_of_zero_returns_nothing(self, fixture_index):
        assert exact_completions(fixture_index, "demo", limit=0) == []

    def test_a_limit_beyond_the_summary_width_is_refused(self, fixture_index):
        with pytest.raises(ValueError, match="built to answer"):
            exact_completions(fixture_index, "demo", limit=WIDTH + 1)

    def test_defaults_to_the_configured_width(self, tmp_path):
        write_corpus(tmp_path, {"a.txt": b"x one\nx two\nx three\n"})
        assert len(exact_completions(index_for(tmp_path, width=2), "x")) == 2


class TestOrdering:
    def test_identical_text_in_different_files_stays_separate(
        self, fixture_index, fixture_root
    ):
        results = exact_completions(fixture_index, "this is")
        assert results[0].source_text == "example.txt"
        assert results[1].source_text == "nested/deep/notes.txt"
        assert results[0].completed_sentence == results[1].completed_sentence

    def test_alphabetical_ties_follow_the_original_sentence(self, tmp_path):
        write_corpus(
            tmp_path,
            {"a.txt": b"Zebra match here.\nApple match here.\nMango match here.\n"},
        )
        results = exact_completions(index_for(tmp_path), "match here")
        assert [r.completed_sentence for r in results] == [
            "Apple match here.",
            "Mango match here.",
            "Zebra match here.",
        ]

    def test_ties_then_break_on_path_and_line(self, tmp_path):
        write_corpus(
            tmp_path, {"b.txt": b"same line\nsame line\n", "a.txt": b"same line\n"}
        )
        results = exact_completions(index_for(tmp_path), "same")
        assert [(r.source_text, r.offset) for r in results] == [
            ("a.txt", 1),
            ("b.txt", 1),
            ("b.txt", 2),
        ]


class TestMatchingBehaviour:
    def test_several_occurrences_in_one_sentence_yield_one_result(self, tmp_path):
        write_corpus(tmp_path, {"a.txt": b"the cat and the hat and the bat\n"})
        results = exact_completions(index_for(tmp_path), "the")
        assert len(results) == 1
        assert results[0].score == exact_score(3)

    def test_case_punctuation_and_spacing_variants_agree(self, fixture_index):
        expected = exact_completions(fixture_index, "this is a demo")
        for variant in ["THIS IS A DEMO", "this  is a  demo", "This is, a demo!"]:
            assert exact_completions(fixture_index, variant) == expected
        assert expected

    def test_a_match_never_spans_two_sentences(self, tmp_path):
        """"demo beta" only exists if the boundary between lines is ignored."""
        write_corpus(tmp_path, {"a.txt": b"alpha demo\nbeta gamma\n"})
        assert exact_completions(index_for(tmp_path), "demo beta") == []

    def test_score_is_twice_the_normalized_length(self, fixture_index):
        for query in ["demo", "this is", "to be or not"]:
            results = exact_completions(fixture_index, query)
            assert all(r.score == 2 * len(normalize(query)) for r in results)

    def test_matches_at_the_start_and_end_of_a_sentence(self, tmp_path):
        write_corpus(tmp_path, {"a.txt": b"alpha beta gamma\n"})
        index = index_for(tmp_path)
        assert exact_completions(index, "alpha")[0].offset == 1
        assert exact_completions(index, "gamma")[0].offset == 1

    def test_an_empty_corpus_answers_nothing(self, tmp_path):
        write_corpus(tmp_path, {"a.txt": b"\n   \n"})
        assert exact_completions(index_for(tmp_path), "anything") == []


CORPUS_CHARACTERS = "aabb c,.\tAB"


@st.composite
def corpora_and_query(draw: st.DrawFn) -> tuple[dict[str, bytes], str]:
    names = draw(
        st.lists(
            st.sampled_from(["a.txt", "b.txt", "deep/c.txt"]),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    files = {
        name: "\n".join(
            draw(
                st.lists(
                    st.text(alphabet=CORPUS_CHARACTERS, min_size=0, max_size=14),
                    min_size=1,
                    max_size=6,
                )
            )
        ).encode("utf-8")
        + b"\n"
        for name in names
    }
    # Half the queries are lifted out of the corpus, so matches are common
    # rather than rare, and half are arbitrary, so misses are covered too.
    lines = [
        line
        for data in files.values()
        for line in data.decode("utf-8").split("\n")
        if normalize(line)
    ]
    if lines and draw(st.booleans()):
        source = normalize(draw(st.sampled_from(lines))).decode("ascii")
        length = draw(st.integers(min_value=1, max_value=min(8, len(source))))
        start = draw(st.integers(min_value=0, max_value=len(source) - length))
        query = source[start : start + length]
    else:
        query = draw(
            st.one_of(
                st.text(alphabet=CORPUS_CHARACTERS, min_size=0, max_size=6),
                st.sampled_from(["", "  ", "!!", "a", "ab", "a b"]),
            )
        )
    return files, query


class TestAgainstBruteForceOnGeneratedCorpora:
    @given(corpora_and_query(), st.integers(min_value=1, max_value=WIDTH))
    @settings(
        max_examples=150,
        deadline=None,
        derandomize=True,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_matches_a_direct_scan(self, tmp_path_factory, case, limit):
        files, query = case
        root = write_corpus(tmp_path_factory.mktemp("corpus"), files)
        assert exact_completions(index_for(root), query, limit=limit) == (
            brute_force_exact(root, query, limit)
        )
