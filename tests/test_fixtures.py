"""The fixture corpus is test input: its bytes must stay stable.

Later milestones hash the corpus for cache invalidation, so a fixture whose
content shifts with the platform (line endings, trailing whitespace) would make
those tests flap. This pins the exact bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def corpus_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.txt"))


def test_expected_files_are_present(mini_corpus):
    relative = [p.relative_to(mini_corpus).as_posix() for p in corpus_files(mini_corpus)]
    assert relative == [
        "example.txt",
        "nested/deep/notes.txt",
        "shakespeare.txt",
    ]


def test_content_hash_is_stable(mini_corpus):
    """Length-delimited hash over sorted (relative path, content) pairs."""
    digest = hashlib.sha256()
    for path in corpus_files(mini_corpus):
        rel = path.relative_to(mini_corpus).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(rel).to_bytes(4, "little"))
        digest.update(rel)
        digest.update(len(data).to_bytes(8, "little"))
        digest.update(data)
    assert digest.hexdigest() == (
        "1793e5aea4d2fc14c87dbb7196b2aa5cfad4ee550993290695ab4d8a8bc3876f"
    )


def test_no_carriage_returns_leaked_in(mini_corpus):
    for path in corpus_files(mini_corpus):
        assert b"\r" not in path.read_bytes(), path


def test_fixture_exercises_the_awkward_cases(mini_corpus):
    notes = (mini_corpus / "nested" / "deep" / "notes.txt").read_bytes()
    assert b"\t" in notes, "a tab is needed for the normalization regression test"
    assert b"   \n" in notes, "a whitespace-only line is needed"
    assert b"\n\n" in notes, "an empty line is needed"
    assert b"\xe2\x80\x94" in notes, "a non-ASCII (em dash) byte sequence is needed"
