"""Tests for the ``main.py`` command-line entry point.

``main.py`` is the process the assignment actually runs (``python main.py``);
nothing elsewhere in the suite drives its argument parsing, its ``--build``
short-circuit, or the error paths that turn a bad config or a missing corpus
into a clean exit code instead of a traceback.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

import main as entrypoint


def write_corpus(root: Path, files: dict[str, bytes] | None = None) -> Path:
    files = files or {"a.txt": b"Alpha line one.\nAlpha line two.\n"}
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def write_config(tmp_path: Path, corpus_root: Path, cache_dir: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"corpus_root: {corpus_root}\ncache_dir: {cache_dir}\n",
        encoding="utf-8",
    )
    return path


class TestArgumentParsing:
    def test_default_config_path_is_next_to_this_file(self):
        parser = entrypoint.build_parser()
        args = parser.parse_args([])
        assert args.config == entrypoint.REPO_ROOT / "config.yaml"

    def test_config_flag_is_accepted_long_and_short(self, tmp_path):
        parser = entrypoint.build_parser()
        assert parser.parse_args(["-c", str(tmp_path)]).config == tmp_path
        assert parser.parse_args(["--config", str(tmp_path)]).config == tmp_path

    def test_build_and_rebuild_default_to_false(self):
        args = entrypoint.build_parser().parse_args([])
        assert args.build is False
        assert args.rebuild is False

    def test_build_flag_is_recognised(self):
        assert entrypoint.build_parser().parse_args(["--build"]).build is True

    def test_rebuild_flag_is_recognised(self):
        assert entrypoint.build_parser().parse_args(["--rebuild"]).rebuild is True

    def test_stats_defaults_to_off_and_is_recognised(self):
        parser = entrypoint.build_parser()
        assert parser.parse_args([]).stats is False
        assert parser.parse_args(["--stats"]).stats is True

    def test_version_flag_prints_the_package_version_and_exits(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            entrypoint.build_parser().parse_args(["--version"])
        assert excinfo.value.code == 0
        assert entrypoint.__version__ in capsys.readouterr().out

    def test_unknown_flag_is_rejected(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            entrypoint.build_parser().parse_args(["--nonsense"])
        assert excinfo.value.code != 0


class TestMainBuildOnly:
    """``--build`` must prepare the index and stop, never reaching the
    interactive loop (which would otherwise hang waiting on stdin)."""

    def test_build_returns_zero_and_reports_the_corpus(self, tmp_path, capsys):
        corpus_root = write_corpus(tmp_path / "corpus")
        cache_dir = tmp_path / ".cache"
        config_path = write_config(tmp_path, corpus_root, cache_dir)

        exit_code = entrypoint.main(["--config", str(config_path), "--build"])

        assert exit_code == 0
        printed = capsys.readouterr().out
        assert "sentences" in printed
        assert "ready in" in printed
        assert str(corpus_root) in printed

    def test_the_summary_reports_memory_use(self, tmp_path, capsys):
        """Printed whether or not --stats was passed: it describes the index
        that was just prepared, not a search."""
        corpus_root = write_corpus(tmp_path / "corpus")
        config_path = write_config(tmp_path, corpus_root, tmp_path / ".cache")

        entrypoint.main(["--config", str(config_path), "--build"])

        assert "memory in use    :" in capsys.readouterr().out

    def test_build_actually_writes_a_cache(self, tmp_path):
        corpus_root = write_corpus(tmp_path / "corpus")
        cache_dir = tmp_path / ".cache"
        config_path = write_config(tmp_path, corpus_root, cache_dir)

        entrypoint.main(["--config", str(config_path), "--build"])

        assert (cache_dir / "CURRENT").is_file()

    def test_rebuild_also_stops_before_the_interactive_loop(self, tmp_path, capsys, monkeypatch):
        corpus_root = write_corpus(tmp_path / "corpus")
        cache_dir = tmp_path / ".cache"
        config_path = write_config(tmp_path, corpus_root, cache_dir)
        entrypoint.main(["--config", str(config_path), "--build"])

        # If --rebuild fell through to the interactive loop this would hang
        # reading from a stdin that never yields a line; poisoning it turns
        # that mistake into a clear failure instead of a stuck test run.
        monkeypatch.setattr(sys, "stdin", None)
        exit_code = entrypoint.main(["--config", str(config_path), "--rebuild"])
        assert exit_code == 0

    def test_a_second_build_reuses_the_unchanged_cache(self, tmp_path, capsys):
        corpus_root = write_corpus(tmp_path / "corpus")
        cache_dir = tmp_path / ".cache"
        config_path = write_config(tmp_path, corpus_root, cache_dir)

        entrypoint.main(["--config", str(config_path), "--build"])
        capsys.readouterr()
        entrypoint.main(["--config", str(config_path), "--build"])

        assert "loaded" in capsys.readouterr().out


class TestMainInteractiveLoop:
    def test_runs_the_cli_loop_when_not_building(self, tmp_path, capsys, monkeypatch):
        corpus_root = write_corpus(tmp_path / "corpus")
        cache_dir = tmp_path / ".cache"
        config_path = write_config(tmp_path, corpus_root, cache_dir)

        # An immediate end of input ends the loop straight away, the way a
        # closed pipe would.
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        exit_code = entrypoint.main(["--config", str(config_path)])

        assert exit_code == 0
        assert entrypoint.cli.READY_MESSAGE in capsys.readouterr().out


class TestMainErrorHandling:
    def test_missing_config_file_exits_with_code_two(self, tmp_path, capsys):
        exit_code = entrypoint.main(["--config", str(tmp_path / "absent.yaml")])
        assert exit_code == 2
        assert "error:" in capsys.readouterr().out

    def test_invalid_config_value_exits_with_code_two(self, tmp_path, capsys):
        path = tmp_path / "config.yaml"
        path.write_text("num_results: -1\n", encoding="utf-8")
        exit_code = entrypoint.main(["--config", str(path)])
        assert exit_code == 2
        assert "num_results" in capsys.readouterr().out

    def test_missing_corpus_exits_with_code_two_instead_of_a_traceback(self, tmp_path, capsys):
        config_path = write_config(
            tmp_path, tmp_path / "does-not-exist", tmp_path / ".cache"
        )
        exit_code = entrypoint.main(["--config", str(config_path)])
        assert exit_code == 2
        assert "error:" in capsys.readouterr().out


class TestDescribeCache:
    def test_describes_the_generation_and_its_size(self, tmp_path):
        corpus_root = write_corpus(tmp_path / "corpus")
        cache_dir = tmp_path / ".cache"
        config_path = write_config(tmp_path, corpus_root, cache_dir)
        entrypoint.main(["--config", str(config_path), "--build"])

        described = entrypoint._describe_cache(cache_dir)

        generation = (cache_dir / "CURRENT").read_text(encoding="utf-8").strip()
        assert described.startswith(generation)
        assert "MB" in described

    def test_falls_back_to_the_bare_path_when_there_is_no_cache_yet(self, tmp_path):
        cache_dir = tmp_path / "never-built"
        assert entrypoint._describe_cache(cache_dir) == str(cache_dir)
