"""Compare two independent rankers over generated corpora.

The reference engine aligns the query against each sentence; the enumeration
ranker builds every repaired form of the query and looks for it. They share no
matching logic, so agreement on complete result lists is real evidence about the
result contract: which records match, what they score, that each appears once,
and in what order.

Hypothesis generates the corpora and the queries. Runs are derandomized so CI
sees the same examples every time; raise ``max_examples`` locally to explore
further.

One limit is worth stating: comparing two implementations cannot detect a fault
in what they share. Both rankers order results with
:func:`~autocomplete.data.tie_break_key`, so breaking that ordering changes both
answers identically and every test here still passes. The ordering policy is
therefore covered by direct assertions in ``test_data.py`` and
``test_reference.py`` instead. Checked by deliberately breaking it: the tests
here stayed green while three direct tests failed.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from autocomplete.normalize import normalize
from autocomplete.reference import (
    SourceRecord,
    find_best_k,
    load_records,
    records_from_lines,
)
from tests.support import enumeration_ranker

# A small alphabet makes collisions, repeated characters and ambiguous edit
# positions common. It also carries the awkward characters: a tab, punctuation,
# uppercase and the space.
RAW_CHARACTERS = "aabbc ,.\tAB1"
QUERY_ALPHABET = "abc 1z"

PATHS = ["a.txt", "b.txt", "nested/notes.txt", "x/y/deep.txt"]

DIFFERENTIAL = settings(
    max_examples=250,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)


def line_text() -> st.SearchStrategy[str]:
    """Corpus lines: mostly short text, sometimes blank or punctuation only.

    Long enough that a query drawn from one can carry a typo and still score
    above zero, which is the interesting middle ground between an exact match
    and the negative scores that very short queries produce.
    """
    return st.one_of(
        st.text(alphabet=RAW_CHARACTERS, min_size=4, max_size=24),
        st.sampled_from(["", "   ", "!!!", "\t", "ab", "AB,", "a b", "a  b"]),
    )


@st.composite
def corpora(draw: st.DrawFn) -> list[SourceRecord]:
    paths = draw(st.lists(st.sampled_from(PATHS), min_size=1, max_size=3, unique=True))
    lines_by_path = {
        path: draw(st.lists(line_text(), min_size=1, max_size=5)) for path in paths
    }
    return records_from_lines(lines_by_path)


@st.composite
def queries(draw: st.DrawFn, records: list[SourceRecord]) -> str:
    """A query, usually related to the corpus so that matches actually happen."""
    kind = draw(
        st.sampled_from(
            [
                "substring",
                "substring",
                "one_edit",
                "one_edit",
                "one_edit",
                "two_edits",
                "garbage",
                "degenerate",
            ]
        )
    )

    if kind == "degenerate":
        return draw(st.sampled_from(["", " ", "!!!", ",.", "\t", "   "]))
    if kind == "garbage" or not records:
        return draw(st.text(alphabet=QUERY_ALPHABET, min_size=1, max_size=8))

    record = draw(st.sampled_from(records))
    sentence = record.normalized.decode("ascii")
    # Prefer substrings long enough to carry an edit and still score positively.
    shortest = 1 if kind == "substring" else min(4, len(sentence))
    start = draw(
        st.integers(min_value=0, max_value=max(0, len(sentence) - shortest))
    )
    longest = min(10, len(sentence) - start)
    length = draw(st.integers(min_value=min(shortest, longest), max_value=longest))
    query = sentence[start : start + length]

    edits = {"substring": 0, "one_edit": 1, "two_edits": 2}[kind]
    for _ in range(edits):
        query = draw(edited(query))
    return query


@st.composite
def edited(draw: st.DrawFn, text: str) -> str:
    """Apply one substitution, one extra character, or one omission."""
    if not text:
        return draw(st.text(alphabet=QUERY_ALPHABET, min_size=1, max_size=2))
    operation = draw(st.sampled_from(["substitute", "insert", "delete"]))
    position = draw(st.integers(min_value=0, max_value=len(text) - 1))
    character = draw(st.sampled_from(QUERY_ALPHABET))
    if operation == "substitute":
        return text[:position] + character + text[position + 1 :]
    if operation == "insert":
        return text[:position] + character + text[position:]
    return text[:position] + text[position + 1 :]


@st.composite
def corpus_and_query(draw: st.DrawFn) -> tuple[list[SourceRecord], str, int]:
    records = draw(corpora())
    query = draw(queries(records))
    k = draw(st.integers(min_value=1, max_value=5))
    return records, query, k


@given(corpus_and_query())
@settings(
    max_examples=250,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
@example(([SourceRecord.from_line("ab", "a.txt", 1)], "aab", 5))
@example(([SourceRecord.from_line("abc", "a.txt", 1)], "ac", 5))
@example(([SourceRecord.from_line("xbc abx", "a.txt", 1)], "abc", 5))
@example(([SourceRecord.from_line("AB,", "a.txt", 1)], "ab", 5))
@example(([SourceRecord.from_line("a  b", "a.txt", 1)], "a b", 5))
def test_rankers_agree_on_the_full_result(case):
    records, query, k = case
    assert find_best_k(query, records, k) == enumeration_ranker.rank(query, records, k)


@given(corpus_and_query())
@settings(
    max_examples=150,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_rankers_agree_beyond_the_top_five(case):
    """Compare every match, not just the ones that fit in the answer, so an
    ordering mistake deeper in the list cannot hide."""
    records, query, _ = case
    everything = len(records) + 1
    assert find_best_k(query, records, everything) == enumeration_ranker.rank(
        query, records, everything
    )


@given(corpus_and_query())
@settings(
    max_examples=150,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_results_obey_the_contract(case):
    """Properties that must hold whatever the two rankers agree on."""
    records, query, k = case
    results = find_best_k(query, records, k)

    assert len(results) <= k
    assert len(results) <= len(records)

    # Each source line appears at most once.
    locations = [(item.source_text, item.offset) for item in results]
    assert len(locations) == len(set(locations))

    # Sorted by the project's order, and every result is a real record.
    assert results == sorted(results, key=lambda item: item.ranking_key)
    known = {
        (record.completed_sentence, record.source_text, record.offset)
        for record in records
    }
    for item in results:
        assert (item.completed_sentence, item.source_text, item.offset) in known

    # A query that normalizes away matches nothing.
    if not normalize(query):
        assert results == []


@given(corpus_and_query())
@settings(
    max_examples=100,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_truncating_to_k_matches_the_full_ranking(case):
    records, query, k = case
    full = find_best_k(query, records, len(records) + 1)
    assert find_best_k(query, records, k) == full[:k]


@given(
    st.text(alphabet=RAW_CHARACTERS, min_size=0, max_size=10),
    st.text(alphabet=QUERY_ALPHABET, min_size=0, max_size=6),
)
@settings(max_examples=300, deadline=None, derandomize=True)
def test_single_sentence_agreement(sentence: str, query: str):
    """The narrowest comparison: one sentence, both rankers."""
    records = records_from_lines({"a.txt": [sentence]})
    assert find_best_k(query, records) == enumeration_ranker.rank(query, records)


FIXTURE_RECORDS = load_records(Path(__file__).parent / "fixtures" / "mini_corpus")


@st.composite
def fixture_queries(draw: st.DrawFn) -> str:
    """Longer, realistic queries taken from the committed fixture corpus."""
    record = draw(st.sampled_from(FIXTURE_RECORDS))
    sentence = record.normalized.decode("ascii")
    length = draw(st.integers(min_value=4, max_value=min(20, len(sentence))))
    start = draw(st.integers(min_value=0, max_value=len(sentence) - length))
    query = sentence[start : start + length]
    for _ in range(draw(st.integers(min_value=0, max_value=2))):
        query = draw(edited(query))
    return query


@given(fixture_queries(), st.integers(min_value=1, max_value=5))
@settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_agreement_on_the_fixture_corpus(query: str, k: int):
    """Real English sentences, so queries are long enough for a typo to still
    score well above zero."""
    assert find_best_k(query, FIXTURE_RECORDS, k) == enumeration_ranker.rank(
        query, FIXTURE_RECORDS, k
    )
