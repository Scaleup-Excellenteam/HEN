"""Tests for canonical normalization."""

from __future__ import annotations

import pytest

from autocomplete.normalize import (
    ALPHABET,
    ALPHABET_SIZE,
    PunctuationPolicy,
    assert_alphabet,
    normalize,
)

SENTENCE = "To be or not to be, that is the question."
SENTENCE_NORMALIZED = b"to be or not to be that is the question"


def test_alphabet_has_the_expected_37_characters():
    assert ALPHABET_SIZE == 37
    assert set(ALPHABET) == set(b"abcdefghijklmnopqrstuvwxyz0123456789 ")


def test_case_is_folded():
    assert normalize("To Be OR nOt") == b"to be or not"


def test_digits_are_kept():
    assert normalize("Digits 123 and 0 stay.") == b"digits 123 and 0 stay"


def test_punctuation_is_removed():
    assert normalize(SENTENCE) == SENTENCE_NORMALIZED
    assert normalize("be, that") == b"be that"
    assert normalize("hello!@#$%^&*()world") == b"helloworld"


def test_repeated_spaces_collapse():
    assert normalize("many     spaces   here") == b"many spaces here"


def test_leading_and_trailing_spaces_are_stripped():
    assert normalize("   padded   ") == b"padded"


@pytest.mark.parametrize(
    "raw",
    ["a\tb", "a\nb", "a\rb", "a\vb", "a\fb", "a \t b"],
)
def test_ascii_whitespace_becomes_a_space_and_never_joins_words(raw):
    """Regression: deleting a tab would invent the word "ab", which is not in
    the corpus, and would lose a character that the score counts."""
    assert normalize(raw) == b"a b"


def test_newline_never_survives():
    """A record separator inside a pattern would let a match span two records."""
    assert b"\n" not in normalize("line one\nline two")
    assert normalize("line one\nline two") == b"line one line two"


def test_mixed_whitespace_run_collapses_to_one_space():
    assert normalize("a \t\r\n  b") == b"a b"


def test_non_ascii_bytes_are_removed():
    assert normalize("Naive cafe — em dash and é accents.") == (
        b"naive cafe em dash and accents"
    )


def test_non_ascii_only_text_normalizes_to_empty():
    assert normalize("שלום") == b""


def test_punctuation_only_text_normalizes_to_empty():
    assert normalize("!!!...???") == b""


def test_empty_input_normalizes_to_empty():
    assert normalize("") == b""
    assert normalize(b"") == b""


@pytest.mark.parametrize(
    "raw",
    [
        SENTENCE,
        "a\tb",
        "   padded   ",
        "MiXeD 123 !!!",
        "Naive cafe — dash",
        "",
        "!!!",
    ],
)
def test_str_and_utf8_bytes_agree(raw):
    assert normalize(raw) == normalize(raw.encode("utf-8"))


@pytest.mark.parametrize(
    "raw",
    [
        SENTENCE,
        "a\tb",
        "   padded   ",
        "MiXeD 123 !!!",
        "Naive cafe — dash",
        "",
    ],
)
def test_normalization_is_idempotent(raw):
    once = normalize(raw)
    assert normalize(once) == once


@pytest.mark.parametrize(
    "raw",
    [
        SENTENCE,
        "a\tb",
        "MiXeD 123 !!!",
        "Naive cafe — dash",
        "line\nbreak",
    ],
)
def test_output_stays_inside_the_alphabet(raw):
    assert set(normalize(raw)) <= set(ALPHABET)
    assert_alphabet(normalize(raw))


def test_bytearray_and_memoryview_are_accepted():
    assert normalize(bytearray(b"Hello, World")) == b"hello world"
    assert normalize(memoryview(b"Hello, World")) == b"hello world"


def test_rejects_other_types():
    with pytest.raises(TypeError):
        normalize(42)  # type: ignore[arg-type]


def test_spacing_variants_of_the_same_text_are_equivalent():
    """The assignment requires punctuation and spacing variants of one query to
    be treated as the same text, and so to score identically."""
    variants = ["be that,", "be, that", "be     that", "  Be That  ", "be that"]
    assert {normalize(v) for v in variants} == {b"be that"}


def test_punctuation_between_words_with_no_space_joins_them():
    """Consequence of TA-DECISION D1 (delete rather than replace with a space).

    Pinned deliberately: if the TA answers that punctuation should become a
    space, this expectation is what changes.
    """
    assert normalize("To Be,That") == b"to bethat"
    assert normalize("e-mail") == b"email"


class TestSpacePunctuationPolicy:
    """TA-DECISION D1 alternative: punctuation becomes a word boundary."""

    def test_punctuation_becomes_a_space(self):
        assert normalize("e-mail", PunctuationPolicy.SPACE) == b"e mail"

    def test_default_policy_deletes_instead(self):
        assert normalize("e-mail") == b"email"

    def test_multi_byte_character_collapses_to_one_space(self):
        assert normalize("word—word", PunctuationPolicy.SPACE) == b"word word"

    def test_still_obeys_the_alphabet_and_is_idempotent(self):
        once = normalize("Mixed, e-mail\ttext!", PunctuationPolicy.SPACE)
        assert_alphabet(once)
        assert normalize(once, PunctuationPolicy.SPACE) == once


class TestAssertAlphabet:
    def test_accepts_normalized_text(self):
        assert_alphabet(normalize(SENTENCE))

    def test_accepts_empty(self):
        assert_alphabet(b"")

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError, match="outside the 37-character alphabet"):
            assert_alphabet(b"Uppercase")

    def test_rejects_newline(self):
        with pytest.raises(ValueError):
            assert_alphabet(b"two\nrecords")

    def test_message_names_the_context(self):
        with pytest.raises(ValueError, match="record 7"):
            assert_alphabet(b"bad!", context="record 7")
