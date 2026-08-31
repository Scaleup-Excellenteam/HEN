"""Tests for finding and reading corpus files."""

from __future__ import annotations

import pytest

from autocomplete.corpus import (
    CorpusNotFoundError,
    fingerprint,
    iter_files,
    iter_lines,
    read_lines,
)


def build_tree(root, files: dict[str, bytes]):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


class TestIterFiles:
    def test_finds_files_at_every_depth(self, mini_corpus):
        assert [f.source_text for f in iter_files(mini_corpus)] == [
            "example.txt",
            "nested/deep/notes.txt",
            "shakespeare.txt",
        ]

    def test_order_is_by_relative_path(self, tmp_path):
        build_tree(
            tmp_path,
            {"b.txt": b"", "a/z.txt": b"", "a/b.txt": b"", "c/d/e.txt": b""},
        )
        assert [f.source_text for f in iter_files(tmp_path)] == [
            "a/b.txt",
            "a/z.txt",
            "b.txt",
            "c/d/e.txt",
        ]

    def test_ignores_other_extensions(self, tmp_path):
        build_tree(tmp_path, {"keep.txt": b"", "skip.md": b"", "skip.txt.bak": b""})
        assert [f.source_text for f in iter_files(tmp_path)] == ["keep.txt"]

    def test_source_text_is_relative_and_posix(self, mini_corpus):
        for corpus_file in iter_files(mini_corpus):
            assert not corpus_file.source_text.startswith("/")
            assert "\\" not in corpus_file.source_text
            assert corpus_file.path.is_file()

    def test_empty_directory_yields_nothing(self, tmp_path):
        assert list(iter_files(tmp_path)) == []

    def test_missing_directory_is_reported_clearly(self, tmp_path):
        with pytest.raises(CorpusNotFoundError, match="corpus directory not found"):
            list(iter_files(tmp_path / "absent"))

    def test_a_file_is_not_a_corpus(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_bytes(b"x")
        with pytest.raises(CorpusNotFoundError):
            list(iter_files(path))

    def test_is_deterministic(self, mini_corpus):
        assert list(iter_files(mini_corpus)) == list(iter_files(mini_corpus))


class TestIterLines:
    def test_numbers_from_one(self):
        assert list(iter_lines(b"a\nb\nc")) == [(1, "a"), (2, "b"), (3, "c")]

    def test_trailing_newline_leaves_an_empty_last_line(self):
        assert list(iter_lines(b"a\n")) == [(1, "a"), (2, "")]

    def test_windows_line_endings_match_unix_ones(self):
        assert [text for _, text in iter_lines(b"a\r\nb\r\n")] == [
            text for _, text in iter_lines(b"a\nb\n")
        ]

    def test_interior_carriage_return_is_kept(self):
        assert list(iter_lines(b"a\rb")) == [(1, "a\rb")]

    def test_empty_input_is_one_empty_line(self):
        assert list(iter_lines(b"")) == [(1, "")]

    def test_utf8_is_decoded(self):
        assert list(iter_lines("café\n".encode("utf-8")))[0] == (1, "café")

    def test_invalid_bytes_do_not_break_the_line(self):
        (number, text) = next(iter(iter_lines(b"caf\xff")))
        assert number == 1
        assert text.startswith("caf")

    def test_read_lines_reads_a_file(self, mini_corpus):
        corpus_file = next(f for f in iter_files(mini_corpus) if f.source_text == "example.txt")
        lines = list(read_lines(corpus_file))
        assert lines[0] == (1, "Alpha: this is a demo.")
        assert lines[4] == (5, "Omega: this is a demo.")


class TestFingerprint:
    def test_is_stable(self, mini_corpus):
        assert fingerprint(mini_corpus) == fingerprint(mini_corpus)

    def test_changes_when_content_changes_without_changing_size(self, tmp_path):
        build_tree(tmp_path, {"a.txt": b"hello"})
        before = fingerprint(tmp_path)
        (tmp_path / "a.txt").write_bytes(b"world")  # same length
        assert fingerprint(tmp_path) != before

    def test_changes_when_a_file_is_added(self, tmp_path):
        build_tree(tmp_path, {"a.txt": b"x"})
        before = fingerprint(tmp_path)
        build_tree(tmp_path, {"b.txt": b""})
        assert fingerprint(tmp_path) != before

    def test_changes_when_a_file_is_renamed(self, tmp_path):
        build_tree(tmp_path, {"a.txt": b"x"})
        before = fingerprint(tmp_path)
        (tmp_path / "a.txt").rename(tmp_path / "b.txt")
        assert fingerprint(tmp_path) != before

    def test_changes_when_a_file_moves_between_directories(self, tmp_path):
        build_tree(tmp_path, {"dir/a.txt": b"x"})
        before = fingerprint(tmp_path)
        (tmp_path / "a.txt").write_bytes(b"x")
        (tmp_path / "dir" / "a.txt").unlink()
        assert fingerprint(tmp_path) != before

    def test_names_and_contents_cannot_be_confused(self, tmp_path):
        """Length-prefixing each field stops a name/content boundary shifting.

        Concatenated naively both of these are "abc"; the fingerprints must
        still differ.
        """
        first = build_tree(tmp_path / "one", {"ab.txt": b"c"})
        second = build_tree(tmp_path / "two", {"a.txt": b"bc"})
        assert fingerprint(first) != fingerprint(second)

    def test_identical_trees_agree(self, tmp_path):
        files = {"a.txt": b"hello", "deep/b.txt": b"world"}
        first = build_tree(tmp_path / "one", files)
        second = build_tree(tmp_path / "two", files)
        assert fingerprint(first) == fingerprint(second)

    def test_empty_corpus_has_a_fingerprint(self, tmp_path):
        assert fingerprint(tmp_path)
