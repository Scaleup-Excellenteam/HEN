"""Compare the indexed engine against the brute-force reference.

The reference reads every sentence and slides a window over it; the engine
repairs the query and looks the repairs up in a suffix array. They share the
normalizer, the result record and the ordering policy, and nothing else, so
agreement on complete result lists is real evidence that the index returns what
a direct search would.

Comparing two implementations cannot catch a fault in what they share. The
ordering policy is therefore covered by direct assertions in ``test_data.py``
and ``test_reference.py`` rather than here.
"""

from __future__ import annotations

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from autocomplete.engine import find_completions
from autocomplete.index import SearchIndex
from autocomplete.normalize import normalize
from autocomplete.reference import find_best_k, load_records

WIDTH = 5

# A small alphabet makes repeated characters, ambiguous edit positions and score
# ties common, and carries the awkward input: a tab, punctuation, uppercase and
# the space.
CORPUS_CHARACTERS = "aabbc ,.\tAB1"
QUERY_ALPHABET = "abc 1z"

PATHS = ["a.txt", "b.txt", "nested/notes.txt"]


def write_corpus(root, files: dict[str, bytes]):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def agree(root, query: str, limit: int = WIDTH) -> bool:
    index = SearchIndex.build(root, summary_width=limit)
    return find_completions(index, query, limit=limit) == find_best_k(
        query, load_records(root), limit
    )


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
def corpus_and_query(draw: st.DrawFn) -> tuple[dict[str, bytes], str, int]:
    names = draw(st.lists(st.sampled_from(PATHS), min_size=1, max_size=3, unique=True))
    files = {
        name: "\n".join(
            draw(
                st.lists(
                    st.one_of(
                        st.text(alphabet=CORPUS_CHARACTERS, min_size=3, max_size=18),
                        st.sampled_from(["", "   ", "!!", "ab", "AB,", "a b", "aab"]),
                    ),
                    min_size=1,
                    max_size=5,
                )
            )
        ).encode("utf-8")
        + b"\n"
        for name in names
    }

    lines = [
        line
        for data in files.values()
        for line in data.decode("utf-8").split("\n")
        if normalize(line)
    ]
    kind = draw(
        st.sampled_from(
            ["exact", "one_edit", "one_edit", "two_edits", "garbage", "degenerate"]
        )
    )
    if kind == "degenerate" or not lines:
        query = draw(st.sampled_from(["", " ", "!!!", ",.", "\t", "   "]))
    elif kind == "garbage":
        query = draw(st.text(alphabet=QUERY_ALPHABET, min_size=1, max_size=8))
    else:
        source = normalize(draw(st.sampled_from(lines))).decode("ascii")
        length = draw(st.integers(min_value=1, max_value=min(10, len(source))))
        start = draw(st.integers(min_value=0, max_value=len(source) - length))
        query = source[start : start + length]
        for _ in range({"exact": 0, "one_edit": 1, "two_edits": 2}[kind]):
            query = draw(edited(query))

    return files, query, draw(st.integers(min_value=1, max_value=WIDTH))


@given(corpus_and_query())
@settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@example(({"a.txt": b"ab\n"}, "aab", 5))
@example(({"a.txt": b"abc\n"}, "ac", 5))
@example(({"a.txt": b"xbc abx\n"}, "abc", 5))
@example(({"a.txt": b"AB,\na b\n"}, "ab", 5))
@example(({"a.txt": b"aaa\naaaa\n"}, "aaaaa", 5))
def test_engine_matches_the_reference(tmp_path_factory, case):
    files, query, limit = case
    root = write_corpus(tmp_path_factory.mktemp("corpus"), files)
    assert agree(root, query, limit)


@given(
    st.lists(
        st.text(alphabet=CORPUS_CHARACTERS, min_size=0, max_size=14),
        min_size=1,
        max_size=6,
    ),
    st.text(alphabet=QUERY_ALPHABET, min_size=0, max_size=7),
)
@settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_single_file_agreement(tmp_path_factory, lines, query):
    root = write_corpus(
        tmp_path_factory.mktemp("corpus"),
        {"a.txt": "\n".join(lines).encode("utf-8") + b"\n"},
    )
    assert agree(root, query)


class TestAdversarialCases:
    """Cases chosen because a plausible shortcut would get them wrong."""

    def test_the_best_record_is_not_in_the_first_pattern_examined(self, tmp_path):
        """Patterns are examined in sorted order, so the pattern reaching the
        alphabetically first sentence is examined last. Taking winners from the
        first pattern with any hit would answer differently."""
        root = write_corpus(
            tmp_path,
            {"a.txt": b"aaa zzz\nbbb zzz\nccc zzz\nzzz zzz\n"},
        )
        assert agree(root, "xaa zzz")

    def test_tier_winners_are_taken_in_record_order_not_discovery_order(
        self, tmp_path
    ):
        """Candidates are merged into a set, and a set of record numbers does not
        iterate in ascending order once the numbers are spread out. This corpus
        scatters the matching sentences among enough filler that the tier's
        candidates are {11, 18, 73, 101, 118}, which a set yields as
        [101, 73, 11, 18, 118]. Taking winners in that order rather than sorting
        would answer with the wrong five sentences.

        Small corpora hide this, because sets of small integers happen to
        iterate in ascending order.
        """
        letters = "abcdefghijklmnopqrstuvwxyz"
        lines = [f"{letters[(i * 7) % 26]}{i:04d} filler words" for i in range(142)]
        lines += [f"{c}bcdef target" for c in "bcmru"]
        root = write_corpus(tmp_path, {"a.txt": "\n".join(lines).encode() + b"\n"})
        assert agree(root, "zbcdef target")

    def test_many_patterns_in_one_tier_reach_overlapping_sentences(self, tmp_path):
        root = write_corpus(
            tmp_path,
            {"a.txt": b"\n".join(f"{c}bc target".encode() for c in "abcdefgh") + b"\n"},
        )
        assert agree(root, "zbc target")

    def test_a_sentence_matching_by_several_repairs_keeps_its_best_score(self, tmp_path):
        root = write_corpus(tmp_path, {"a.txt": b"aab\nab\naaab\n"})
        for query in ["aab", "ab", "aaab", "aaaab"]:
            assert agree(root, query)

    def test_heavy_duplication_of_one_sentence(self, tmp_path):
        root = write_corpus(
            tmp_path,
            {
                "a.txt": b"repeated line\n" * 40,
                "b.txt": b"repeated line\n" * 40,
            },
        )
        for query in ["repeated", "repeatd", "repeatted", "repexted"]:
            assert agree(root, query)

    def test_score_ties_across_files_and_lines(self, tmp_path):
        root = write_corpus(
            tmp_path,
            {"b.txt": b"same text\nsame text\n", "a.txt": b"same text\n"},
        )
        for query in ["same text", "same txt", "same tekst"]:
            assert agree(root, query)

    def test_case_and_punctuation_variants_of_one_sentence(self, tmp_path):
        root = write_corpus(
            tmp_path, {"a.txt": "Ab, cd.\nab cd\nAB CD!\nab  cd\n".encode()}
        )
        for query in ["ab cd", "ab c", "ab cx", "abcd"]:
            assert agree(root, query)

    def test_a_query_longer_than_every_sentence(self, tmp_path):
        root = write_corpus(tmp_path, {"a.txt": b"short\n"})
        for query in ["shorter", "shortest", "s" * 40]:
            assert agree(root, query)

    def test_every_repair_type_on_one_corpus(self, tmp_path):
        root = write_corpus(
            tmp_path, {"a.txt": b"the quick brown fox\njumps over the lazy dog\n"}
        )
        for query in [
            "quick brown",  # exact
            "quick brawn",  # substitution
            "quick browwn",  # extra character
            "quick brwn",  # missing character
            "quick brawwn",  # two edits, no match
        ]:
            assert agree(root, query)

    def test_repeated_character_families(self, tmp_path):
        root = write_corpus(tmp_path, {"a.txt": b"aaaa\naaa\naa\na\nbaaa\n"})
        for query in ["a", "aa", "aaa", "aaaa", "aaaaa", "aab", "baa"]:
            assert agree(root, query)

    def test_digits_and_mixed_alphabet(self, tmp_path):
        root = write_corpus(tmp_path, {"a.txt": b"rfc 1234 says\nrfc 1235 says\n"})
        for query in ["rfc 1234", "rfc 1244", "rfc 134", "rfc 12345"]:
            assert agree(root, query)

    def test_fewer_results_requested_than_available(self, tmp_path):
        root = write_corpus(
            tmp_path,
            {"a.txt": b"\n".join(f"row {i} target".encode() for i in range(9)) + b"\n"},
        )
        for limit in (1, 2, 3):
            assert agree(root, "target", limit)
            assert agree(root, "targt", limit)
