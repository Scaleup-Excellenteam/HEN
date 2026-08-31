"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from autocomplete.config import VALIDATION_LEVELS, Config, ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_valid_without_a_file():
    config = Config()
    assert config.num_results == 5
    assert config.use_mmap is True
    assert config.validation_level == "content"


def test_shipped_config_file_loads():
    """The config.yaml committed at the repository root must stay valid."""
    config = Config.from_yaml(REPO_ROOT / "config.yaml")
    assert config.num_results >= 1
    assert config.validation_level in VALIDATION_LEVELS


def test_empty_file_yields_all_defaults(tmp_path):
    path = write_config(tmp_path, "")
    assert Config.from_yaml(path) == Config()


def test_values_override_defaults(tmp_path):
    path = write_config(
        tmp_path,
        "num_results: 3\nuse_mmap: false\nvalidation_level: full\n",
    )
    config = Config.from_yaml(path)
    assert config.num_results == 3
    assert config.use_mmap is False
    assert config.validation_level == "full"


def test_relative_paths_resolve_against_the_config_file(tmp_path):
    path = write_config(tmp_path, "corpus_root: data/corpus\ncache_dir: .cache\n")
    config = Config.from_yaml(path)
    assert config.corpus_root == tmp_path / "data/corpus"
    assert config.cache_dir == tmp_path / ".cache"


def test_absolute_paths_are_left_alone(tmp_path):
    absolute = tmp_path / "elsewhere"
    path = write_config(tmp_path, f"corpus_root: {absolute}\n")
    assert Config.from_yaml(path).corpus_root == absolute


def test_user_home_is_expanded(tmp_path):
    path = write_config(tmp_path, "corpus_root: ~/corpus\n")
    config = Config.from_yaml(path)
    assert config.corpus_root == Path.home() / "corpus"


def test_environment_variables_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOMPLETE_TEST_DIR", str(tmp_path / "from_env"))
    path = write_config(tmp_path, "corpus_root: $AUTOCOMPLETE_TEST_DIR\n")
    assert Config.from_yaml(path).corpus_root == tmp_path / "from_env"


def test_config_is_frozen():
    config = Config()
    with pytest.raises(Exception):
        config.num_results = 7  # type: ignore[misc]


def test_unknown_key_is_rejected(tmp_path):
    path = write_config(tmp_path, "num_reslts: 5\n")
    with pytest.raises(ConfigError, match="unknown configuration key"):
        Config.from_yaml(path)


@pytest.mark.parametrize("value", [0, -1])
def test_num_results_must_be_positive(value):
    with pytest.raises(ConfigError, match="num_results"):
        Config(num_results=value)


def test_num_results_rejects_bool():
    with pytest.raises(ConfigError, match="num_results"):
        Config(num_results=True)  # type: ignore[arg-type]


def test_num_results_rejects_non_integer(tmp_path):
    path = write_config(tmp_path, "num_results: five\n")
    with pytest.raises(ConfigError, match="num_results"):
        Config.from_yaml(path)


def test_validation_level_must_be_known():
    with pytest.raises(ConfigError, match="validation_level"):
        Config(validation_level="paranoid")


def test_use_mmap_must_be_boolean(tmp_path):
    path = write_config(tmp_path, "use_mmap: sometimes\n")
    with pytest.raises(ConfigError, match="use_mmap"):
        Config.from_yaml(path)


def test_path_value_must_be_a_string(tmp_path):
    path = write_config(tmp_path, "corpus_root: 12\n")
    with pytest.raises(ConfigError, match="corpus_root"):
        Config.from_yaml(path)


def test_invalid_yaml_is_reported(tmp_path):
    path = write_config(tmp_path, "corpus_root: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        Config.from_yaml(path)


def test_non_mapping_document_is_reported(tmp_path):
    path = write_config(tmp_path, "- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        Config.from_yaml(path)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config file"):
        Config.from_yaml(tmp_path / "absent.yaml")
