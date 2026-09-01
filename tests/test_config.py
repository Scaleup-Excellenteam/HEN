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


def test_empty_file_yields_defaults_anchored_to_the_file(tmp_path):
    path = write_config(tmp_path, "")
    config = Config.from_yaml(path)
    assert config.corpus_root == tmp_path / "corpus"
    assert config.cache_dir == tmp_path / ".cache"
    assert config.num_results == Config().num_results
    assert config.use_mmap == Config().use_mmap
    assert config.validation_level == Config().validation_level


def test_partial_file_omitting_both_paths_still_anchors_them(tmp_path):
    path = write_config(tmp_path, "num_results: 3\n")
    config = Config.from_yaml(path)
    assert config.num_results == 3
    assert config.corpus_root == tmp_path / "corpus"
    assert config.cache_dir == tmp_path / ".cache"


def test_supplying_only_corpus_root_anchors_the_cache_default(tmp_path):
    path = write_config(tmp_path, "corpus_root: data/corpus\n")
    config = Config.from_yaml(path)
    assert config.corpus_root == tmp_path / "data/corpus"
    assert config.cache_dir == tmp_path / ".cache"


def test_supplying_only_cache_dir_anchors_the_corpus_default(tmp_path):
    path = write_config(tmp_path, "cache_dir: build/index\n")
    config = Config.from_yaml(path)
    assert config.corpus_root == tmp_path / "corpus"
    assert config.cache_dir == tmp_path / "build/index"


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "num_results: 3\n",
        "corpus_root: data/corpus\n",
        "cache_dir: build/index\n",
        "corpus_root: data/corpus\ncache_dir: build/index\n",
    ],
)
def test_paths_do_not_depend_on_the_working_directory(tmp_path, monkeypatch, contents):
    """The same file must describe the same directories from anywhere."""
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    path = write_config(config_dir, contents)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.chdir(tmp_path)
    from_one = Config.from_yaml(path)
    monkeypatch.chdir(elsewhere)
    from_another = Config.from_yaml(path)

    assert from_one == from_another
    assert from_one.corpus_root.is_absolute()
    assert from_one.cache_dir.is_absolute()
    assert from_one.corpus_root.is_relative_to(config_dir)
    assert from_one.cache_dir.is_relative_to(config_dir)


def test_direct_construction_keeps_defaults_relative():
    """Only loading anchors paths; constructing a Config does not guess a root."""
    config = Config()
    assert config.corpus_root == Path("corpus")
    assert config.cache_dir == Path(".cache")


def test_from_mapping_without_a_base_dir_leaves_paths_relative():
    config = Config.from_mapping({})
    assert config.corpus_root == Path("corpus")
    assert config.cache_dir == Path(".cache")


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


class TestConfigPathOverride:
    """``HEN_CONFIG`` exists so a throwaway corpus and cache can be served
    without touching the index the machine has already prepared."""

    def test_the_project_file_is_used_when_it_is_unset(self, monkeypatch):
        from autocomplete.config import CONFIG_PATH_VARIABLE, default_config_path

        monkeypatch.delenv(CONFIG_PATH_VARIABLE, raising=False)
        assert default_config_path().name == "config.yaml"
        assert default_config_path().parent.name == "HEN"

    def test_it_points_somewhere_else_when_set(self, monkeypatch, tmp_path):
        from autocomplete.config import CONFIG_PATH_VARIABLE, default_config_path

        elsewhere = tmp_path / "demo" / "config.yaml"
        elsewhere.parent.mkdir()
        elsewhere.write_text("num_results: 5\n", encoding="utf-8")
        monkeypatch.setenv(CONFIG_PATH_VARIABLE, str(elsewhere))
        assert default_config_path() == elsewhere.resolve()

    def test_an_empty_value_is_ignored(self, monkeypatch):
        from autocomplete.config import CONFIG_PATH_VARIABLE, default_config_path

        monkeypatch.setenv(CONFIG_PATH_VARIABLE, "   ")
        assert default_config_path().name == "config.yaml"

    def test_the_override_is_actually_loaded(self, monkeypatch, tmp_path):
        from autocomplete.config import CONFIG_PATH_VARIABLE, load_default_config

        elsewhere = tmp_path / "config.yaml"
        elsewhere.write_text(
            f"corpus_root: {tmp_path / 'corpus'}\ncache_dir: {tmp_path / 'cache'}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(CONFIG_PATH_VARIABLE, str(elsewhere))
        loaded = load_default_config()
        assert loaded.cache_dir == tmp_path / "cache"
