"""Configuration loading and validation.

Settings come from a YAML file (``config.yaml`` in the repository root by
default). Every setting has a documented default, so a missing or partial file is
valid; unknown keys are rejected so that a typo cannot silently keep a default.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

__all__ = ["Config", "ConfigError", "VALIDATION_LEVELS", "DEFAULT_CONFIG_FILENAME"]

DEFAULT_CONFIG_FILENAME = "config.yaml"

#: Cache validation strength on load, weakest first. See the ADR (ss9.2): the
#: measured cost of "content" on the real corpus is ~0.3 s, so it is the default.
VALIDATION_LEVELS = ("structural", "content", "full")

#: Settings holding filesystem paths, anchored to the config file when loading.
_PATH_KEYS = ("corpus_root", "cache_dir")


class ConfigError(ValueError):
    """Raised when a configuration file or value is invalid."""


@dataclass(frozen=True)
class Config:
    """Validated, immutable project configuration.

    The defaults below are relative paths. Loading through :meth:`from_yaml`
    anchors them to the config file's directory; constructing a ``Config``
    directly leaves them relative to the working directory.

    Attributes:
        corpus_root: Directory tree holding the corpus ``.txt`` files.
        cache_dir: Directory for generated index artifacts. Created on demand.
        num_results: Number of completions to return (``k`` in the ADR).
        use_mmap: Memory-map index artifacts instead of reading them into RAM.
        validation_level: How thoroughly a cache is validated when loaded;
            one of :data:`VALIDATION_LEVELS`.
    """

    corpus_root: Path = Path("corpus")
    cache_dir: Path = Path(".cache")
    num_results: int = 5
    use_mmap: bool = True
    validation_level: str = "content"

    def __post_init__(self) -> None:
        if not isinstance(self.corpus_root, Path):
            raise ConfigError("corpus_root must be a path")
        if not isinstance(self.cache_dir, Path):
            raise ConfigError("cache_dir must be a path")
        # bool is a subclass of int, so reject it explicitly.
        if isinstance(self.num_results, bool) or not isinstance(self.num_results, int):
            raise ConfigError("num_results must be an integer")
        if self.num_results < 1:
            raise ConfigError(f"num_results must be >= 1, got {self.num_results}")
        if not isinstance(self.use_mmap, bool):
            raise ConfigError("use_mmap must be a boolean")
        if self.validation_level not in VALIDATION_LEVELS:
            raise ConfigError(
                f"validation_level must be one of {list(VALIDATION_LEVELS)}, "
                f"got {self.validation_level!r}"
            )

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], base_dir: Path | None = None
    ) -> "Config":
        """Build a config from a mapping, filling in defaults.

        When ``base_dir`` is given, every relative path setting is resolved
        against it, whether the mapping supplied the value or the default was
        used. That is what makes a config file mean the same thing regardless of
        the working directory. With no ``base_dir`` the paths are left relative.
        """
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(
                f"unknown configuration key(s): {', '.join(unknown)}; "
                f"expected any of {sorted(known)}"
            )

        values: dict[str, Any] = dict(data)
        defaults = {f.name: f.default for f in fields(cls)}
        for key in _PATH_KEYS:
            # Anchor omitted keys too: a default path is as much a config-relative
            # path as a written one, and leaving it to be read against the
            # process working directory would make an empty config file mean
            # different things depending on where the program was started.
            values[key] = _resolve_path(values.get(key, defaults[key]), key, base_dir)
        return cls(**values)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "Config":
        """Load configuration from a YAML file.

        An empty file is treated as "all defaults". Path settings, including
        defaulted ones, are anchored to the directory holding ``path``.
        """
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config file {path}: {exc}") from exc

        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

        if parsed is None:
            parsed = {}
        if not isinstance(parsed, Mapping):
            raise ConfigError(
                f"{path} must contain a mapping of settings, got {type(parsed).__name__}"
            )
        return cls.from_mapping(parsed, base_dir=path.parent)


def _resolve_path(value: Any, key: str, base_dir: Path | None) -> Path:
    """Expand ``~``/environment variables and anchor relative paths to base_dir."""
    if isinstance(value, Path):
        candidate = value
    elif isinstance(value, str):
        candidate = Path(value)
    else:
        raise ConfigError(f"{key} must be a string path, got {type(value).__name__}")

    candidate = Path(os.path.expandvars(candidate.expanduser()))
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    # Collapse "..", so a path built from a config-relative value reads cleanly in
    # output and error messages. Symlinks are deliberately left unresolved.
    return Path(os.path.normpath(candidate))
