"""Canonical text normalization.

One pipeline normalizes both corpus lines and user input, so that matching is
insensitive to case, punctuation and spacing exactly as the assignment requires.
Everything downstream (the record blob, the suffix array, every search pattern)
is expressed in the normalized alphabet, which is what makes a search hit
impossible to misinterpret.

Guarantees, relied on by the engine and its correctness proof:

* the output contains only bytes from :data:`ALPHABET` (37 distinct values);
* the output never contains a newline, so a suffix-array match can never span
  two corpus records;
* normalization is idempotent;
* normalizing ``str`` and normalizing its UTF-8 encoding give the same result.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "ALPHABET",
    "ALPHABET_SIZE",
    "DEFAULT_PUNCTUATION_POLICY",
    "PunctuationPolicy",
    "assert_alphabet",
    "normalize",
]


class PunctuationPolicy(Enum):
    """What to do with bytes that are neither letters, digits nor spaces.

    TA-DECISION D1: the assignment says "remove punctuation" without saying
    whether the removed character leaves a word boundary behind. We delete it,
    the literal reading, which also reproduces the appendix example
    ``"be, that"`` -> ``"be that"`` (the space after the comma survives on its
    own). The consequence is that ``"e-mail"`` normalizes to ``"email"`` and
    ``"word-word"`` to ``"wordword"``.

    :data:`SPACE` implements the alternative reading. Switching the default below
    is the whole change; a rebuilt index is required afterwards because the
    normalized corpus differs.
    """

    DELETE = "delete"
    SPACE = "space"


DEFAULT_PUNCTUATION_POLICY = PunctuationPolicy.DELETE

#: The normalized alphabet: lowercase letters, digits and the space.
ALPHABET = b"abcdefghijklmnopqrstuvwxyz0123456789 "
ALPHABET_SIZE = len(ALPHABET)

_ALPHABET_SET = frozenset(ALPHABET)

# ASCII whitespace becomes a space rather than being deleted. Deleting it would
# join the words on either side ("a\tb" -> "ab"), inventing text that is not in
# the corpus and losing a character the score counts.
_WHITESPACE_BYTES = (0x09, 0x0A, 0x0B, 0x0C, 0x0D)  # tab, LF, VT, FF, CR
_SPACE = 0x20


def _build_translation(policy: PunctuationPolicy) -> tuple[bytes, bytes]:
    """Return the ``(table, delete_set)`` pair for :meth:`bytes.translate`."""
    table = bytearray(range(256))
    for code in range(ord("A"), ord("Z") + 1):
        table[code] = code + 32
    for code in _WHITESPACE_BYTES:
        table[code] = _SPACE

    if policy is PunctuationPolicy.SPACE:
        for code in range(256):
            if table[code] not in _ALPHABET_SET:
                table[code] = _SPACE
        return bytes(table), b""

    # bytes.translate deletes first and translates afterwards, so the delete set
    # is expressed in terms of the original byte values.
    delete = bytes(
        code for code in range(256) if table[code] not in _ALPHABET_SET
    )
    return bytes(table), delete


_TRANSLATIONS = {policy: _build_translation(policy) for policy in PunctuationPolicy}


def normalize(
    text: str | bytes | bytearray | memoryview,
    policy: PunctuationPolicy | None = None,
) -> bytes:
    """Reduce text to its canonical comparison form.

    Lowercases ASCII letters, turns every ASCII whitespace character into a
    space, applies the punctuation policy, collapses runs of spaces and strips
    the ends.

    Args:
        text: Input as ``str`` or raw bytes. ``str`` is encoded as UTF-8 first,
            so both forms of the same text normalize identically.
        policy: Punctuation handling; defaults to
            :data:`DEFAULT_PUNCTUATION_POLICY`.

    Returns:
        The normalized text as ``bytes`` over :data:`ALPHABET`, possibly empty.
    """
    if isinstance(text, str):
        raw = text.encode("utf-8")
    elif isinstance(text, (bytes, bytearray, memoryview)):
        raw = bytes(text)
    else:
        raise TypeError(f"expected str or bytes, got {type(text).__name__}")

    table, delete = _TRANSLATIONS[policy or DEFAULT_PUNCTUATION_POLICY]
    # After translation the only whitespace left is the space itself, so split()
    # collapses runs and strips both ends in one step.
    return b" ".join(raw.translate(table, delete).split())


def assert_alphabet(data: bytes, context: str = "") -> None:
    """Raise if ``data`` contains a byte outside :data:`ALPHABET`.

    Used while building the index: the search proof assumes every stored record
    and every search pattern is over this alphabet, so the invariant is checked
    rather than trusted.
    """
    invalid = sorted(frozenset(data) - _ALPHABET_SET)
    if invalid:
        where = f" in {context}" if context else ""
        preview = ", ".join(f"0x{value:02x}" for value in invalid[:8])
        raise ValueError(
            f"normalized text{where} contains {len(invalid)} byte value(s) "
            f"outside the {ALPHABET_SIZE}-character alphabet: {preview}"
        )
