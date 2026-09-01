"""A few ``autocomplete.corpus`` edges ``test_corpus.py`` does not cover: the
documented "symlinked directories are not followed" guarantee, the exact
(case-sensitive) suffix match, and that ``iter_files`` is a generator whose
missing-root check fires only once consumed."""

from __future__ import annotations

import sys

import pytest

from autocomplete.corpus import CorpusNotFoundError, fingerprint, iter_files


def build_tree(root, files: dict[str, bytes]):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


class TestSymlinkedDirectoriesAreNotFollowed:
    requires_symlinks = pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlink creation needs elevated privileges on Windows",
    )

    @requires_symlinks
    def test_a_cyclic_symlink_does_not_hang_the_walk(self, tmp_path):
        build_tree(tmp_path, {"real.txt": b"hello"})
        loop = tmp_path / "loop"
        loop.symlink_to(tmp_path, target_is_directory=True)

        # A test timeout is the real safety net; this assertion just documents
        # what a completed, non-hanging walk must have found.
        assert [f.source_text for f in iter_files(tmp_path)] == ["real.txt"]

    @requires_symlinks
    def test_files_reached_only_through_the_symlink_are_excluded(self, tmp_path):
        target = tmp_path / "elsewhere"
        build_tree(target, {"only_here.txt": b"x"})
        build_tree(tmp_path, {"kept.txt": b"y"})
        (tmp_path / "corpus" ).mkdir()
        (tmp_path / "corpus" / "via_link").symlink_to(target, target_is_directory=True)

        found = [f.source_text for f in iter_files(tmp_path / "corpus")]
        assert found == []


class TestExtensionMatchIsExact:
    def test_uppercase_extension_is_not_treated_as_txt(self, tmp_path):
        build_tree(tmp_path, {"keep.txt": b"a", "skip.TXT": b"b"})
        assert [f.source_text for f in iter_files(tmp_path)] == ["keep.txt"]


class TestIterFilesIsLazilyEvaluated:
    def test_calling_it_on_a_missing_root_does_not_raise_by_itself(self, tmp_path):
        """iter_files is a generator: building it must not do any work. The
        error is documented on the function, but only advancing the iterator
        can actually raise it."""
        generator = iter_files(tmp_path / "absent")
        with pytest.raises(CorpusNotFoundError):
            next(generator)

    def test_fingerprint_of_a_missing_root_reports_it_as_a_missing_corpus(self, tmp_path):
        """fingerprint delegates its file listing to iter_files, so it must
        surface the same, specific error rather than some low-level one."""
        with pytest.raises(CorpusNotFoundError):
            fingerprint(tmp_path / "absent")
