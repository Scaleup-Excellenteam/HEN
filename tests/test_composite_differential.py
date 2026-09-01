"""Compare the composite search against a brute-force ranker over the union.

The overlay architecture answers a query by searching two indexes separately and
merging. What it must produce is what ranking every record of both corpora
together would produce, and the only honest way to check that is to compute the
second answer a different way. So every case here builds a small base corpus and
a small imported one, runs the composite search, and compares it against
:func:`autocomplete.reference.find_best_k` over the concatenated records.

The reference slides a window over each sentence and shares no reasoning with
either the tier walk or the merge, so agreement is evidence rather than a
tautology. The composite engine is never used as its own oracle.

Comparing two implementations cannot catch a fault in what they share, so the
merge's own tie-breaking is asserted directly in ``test_composite.py`` instead
of being inferred from agreement here.
"""

from __future__ import annotations

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from autocomplete import composite
from autocomplete.index import SearchIndex
from autocomplete.normalize import normalize
from autocomplete.reference import find_best_k, load_records

WIDTH = 5

# The same small alphabet the engine's differential suite uses: it makes score
# ties, repeated characters and ambiguous edit positions common rather than rare,
# and carries a tab, punctuation, uppercase and the space.
CORPUS_CHARACTERS = "aabbc ,.\tAB1"
QUERY_ALPHABET = "abc 1z"

BASE_PATHS = ["corpus.txt", "nested/more.txt"]
DRIVE_PATHS = ["Google Drive/notes.txt", "Google Drive/second.txt"]

#: Lines placed in both corpora on purpose, so duplicate sentence text across
#: sources, and ties that cross the index boundary, are generated rather than
#: waited for.
SHARED_LINES = ["aab c", "ab ab", "abc", "a b c", "AB, c"]


def write(root, files: dict[str, bytes]):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def fields(results):
    """Every field of every result, in order, for a field-by-field comparison."""
    return [
        (item.completed_sentence, item.source_text, item.offset, item.score)
        for item in results
    ]


def agree(workspace, base_files, drive_files, query: str, limit: int) -> None:
    base_root = write(workspace / "base", base_files)
    drive_root = write(workspace / "drive", drive_files)

    base = SearchIndex.build(base_root, summary_width=WIDTH)
    overlay = SearchIndex.build(drive_root, summary_width=WIDTH) if drive_files else None

    actual = composite.search(base, overlay, query, limit)
    expected = find_best_k(
        query,
        load_records(base_root) + (load_records(drive_root) if drive_files else []),
        limit,
    )
    assert fields(actual) == fields(expected), query


@st.composite
def edited(draw: st.DrawFn, text: str) -> str:
    """One substitution, one extra character, or one omission."""
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
def lines(draw: st.DrawFn) -> list[str]:
    return draw(
        st.lists(
            st.one_of(
                st.text(alphabet=CORPUS_CHARACTERS, min_size=3, max_size=18),
                st.sampled_from(SHARED_LINES),
                st.sampled_from(["", "   ", "!!", "ab", "AB,", "a b", "aab"]),
            ),
            min_size=1,
            max_size=5,
        )
    )


@st.composite
def files_for(draw: st.DrawFn, paths: list[str], *, allow_empty: bool) -> dict[str, bytes]:
    chosen = draw(
        st.lists(
            st.sampled_from(paths),
            min_size=0 if allow_empty else 1,
            max_size=len(paths),
            unique=True,
        )
    )
    return {
        name: "\n".join(draw(lines())).encode("utf-8") + b"\n" for name in chosen
    }


@st.composite
def corpora_and_query(draw: st.DrawFn):
    base_files = draw(files_for(BASE_PATHS, allow_empty=False))
    # An empty imported corpus is a real state: it is what the server serves
    # before anything is imported and after the last document is removed.
    drive_files = draw(files_for(DRIVE_PATHS, allow_empty=True))

    available = [
        line
        for data in list(base_files.values()) + list(drive_files.values())
        for line in data.decode("utf-8").split("\n")
        if normalize(line)
    ]

    kind = draw(
        st.sampled_from(
            ["exact", "one_edit", "one_edit", "two_edits", "garbage", "degenerate"]
        )
    )
    if kind == "degenerate" or not available:
        query = draw(st.sampled_from(["", " ", "!!!", ",.", "\t", "   "]))
    elif kind == "garbage":
        query = draw(st.text(alphabet=QUERY_ALPHABET, min_size=1, max_size=8))
    else:
        source = normalize(draw(st.sampled_from(available))).decode("ascii")
        length = draw(st.integers(min_value=1, max_value=min(10, len(source))))
        start = draw(st.integers(min_value=0, max_value=len(source) - length))
        query = source[start : start + length]
        for _ in range({"exact": 0, "one_edit": 1, "two_edits": 2}[kind]):
            query = draw(edited(query))

    return base_files, drive_files, query, draw(st.integers(min_value=1, max_value=WIDTH))


@given(corpora_and_query())
@settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
# Every winner from the base corpus.
@example(({"corpus.txt": b"ab\nab c\nab d\n"}, {"Google Drive/notes.txt": b"zzz\n"}, "ab", 5))
# Every winner from the imported one.
@example(({"corpus.txt": b"zzz\n"}, {"Google Drive/notes.txt": b"ab\nab c\nab d\n"}, "ab", 5))
# The same sentence in both corpora: two results, not one.
@example(({"corpus.txt": b"aab c\n"}, {"Google Drive/notes.txt": b"aab c\n"}, "aab c", 5))
# A tie on score that crosses the boundary between the indexes.
@example(({"corpus.txt": b"ab b\nab d\n"}, {"Google Drive/notes.txt": b"ab a\nab c\n"}, "ab", 5))
# A record reachable through more than one repair.
@example(({"corpus.txt": b"ab\n"}, {"Google Drive/notes.txt": b"aab\n"}, "aab", 5))
@example(({"corpus.txt": b"abc\n"}, {"Google Drive/notes.txt": b"ac\n"}, "ac", 5))
# Fewer than K results in total.
@example(({"corpus.txt": b"ab\n"}, {"Google Drive/notes.txt": b"ab c\n"}, "ab", 5))
# Nothing imported at all.
@example(({"corpus.txt": b"ab\nab c\n"}, {}, "ab", 5))
# K itself lowered, so the merge has to truncate a longer combined answer.
@example(({"corpus.txt": b"ab a\nab c\n"}, {"Google Drive/notes.txt": b"ab b\nab d\n"}, "ab", 2))
def test_the_composite_search_matches_a_brute_force_union(tmp_path_factory, case):
    base_files, drive_files, query, limit = case
    # A fresh directory per example: reusing one would leave a previous
    # example's files behind and compare against a corpus nobody generated.
    agree(tmp_path_factory.mktemp("union"), base_files, drive_files, query, limit)
