"""Tests for the top-level package API in ``autocomplete/__init__.py``.

``get_default_index``/``reset_default_index`` manage a module-level cache that
nothing in the rest of the suite exercises directly: every other test builds
or loads a :class:`SearchIndex` explicitly instead of going through the public
entry point a consumer of the package actually calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import autocomplete
from autocomplete.config import Config


def write_corpus(root: Path) -> Path:
    (root).mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_bytes(b"Alpha line one.\nAlpha line two.\n")
    return root


@pytest.fixture(autouse=True)
def _clean_default_index():
    """Every test starts and ends with no cached index, so tests cannot leak
    a prepared index (or its temp corpus) into one another."""
    autocomplete.reset_default_index()
    yield
    autocomplete.reset_default_index()


def make_config(tmp_path: Path, name: str = "corpus") -> Config:
    return Config(corpus_root=write_corpus(tmp_path / name), cache_dir=tmp_path / f"{name}-cache")


class TestGetDefaultIndex:
    def test_is_memoized_across_calls(self, tmp_path):
        config = make_config(tmp_path)
        first = autocomplete.get_default_index(config)
        second = autocomplete.get_default_index()
        assert first is second

    def test_reset_forces_a_fresh_index_on_the_next_call(self, tmp_path):
        config = make_config(tmp_path)
        first = autocomplete.get_default_index(config)
        autocomplete.reset_default_index()
        second = autocomplete.get_default_index(config)
        assert first is not second

    def test_passing_a_new_config_replaces_the_held_index(self, tmp_path):
        first_config = make_config(tmp_path, "one")
        second_config = make_config(tmp_path, "two")
        first = autocomplete.get_default_index(first_config)
        second = autocomplete.get_default_index(second_config)
        assert first is not second
        # And it stays the replacement on a bare call, not reverting to the first.
        assert autocomplete.get_default_index() is second


class TestGetBestKCompletions:
    def test_uses_and_prepares_the_default_index(self, tmp_path):
        config = make_config(tmp_path)
        autocomplete.get_default_index(config)
        results = autocomplete.get_best_k_completions("Alpha line")
        assert results
        assert all(isinstance(item, autocomplete.AutoCompleteData) for item in results)

    def test_result_type_matches_the_module_export(self, tmp_path):
        """AutoCompleteData exported from the package root must be the exact
        type get_best_k_completions returns, since callers import it from here."""
        config = make_config(tmp_path)
        autocomplete.get_default_index(config)
        [result] = autocomplete.get_best_k_completions("Alpha line one")[:1]
        assert type(result) is autocomplete.AutoCompleteData
