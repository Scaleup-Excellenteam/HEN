"""Checks that this project uses Anthropic's Claude API, and declares the
credential it needs to call it.

These are organization-wide requirement checks, run the same way across every
repository: every project is expected to use the Anthropic/Claude SDK (or call
its API directly) somewhere in its source, and to declare an
``ANTHROPIC_API_KEY`` (or an equivalently named credential) in its
configuration or environment handling. If this fails here, check whether that
organization-wide requirement actually applies to this particular project
before assuming the project's own code is at fault.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directories that are dependencies, build output or caches rather than this
#: project's own source, so they are not searched.
_EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    ".cache",
    "dist",
    "build",
    ".mypy_cache",
}

#: Source-ish file extensions worth scanning for API usage and config keys.
_SCANNED_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".cfg",
    ".ini",
    ".env",
    ".md",
    ".sh",
}

#: Signals that the Anthropic/Claude SDK or API is in use somewhere in the
#: project: the Python package, the JS/TS package, and a direct call to the
#: API's own host.
_ANTHROPIC_USAGE_SIGNALS = (
    "import anthropic",
    "from anthropic",
    "anthropic.Anthropic(",
    "@anthropic-ai/sdk",
    "api.anthropic.com",
)

#: Names the credential is reasonably declared under.
_ANTHROPIC_API_KEY_SIGNALS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)


#: This file itself necessarily contains the very strings it searches for, in
#: its docstrings and signal tuples; without excluding it, the search would
#: trivially find itself and the checks below could never fail.
_THIS_FILE = Path(__file__).resolve()


def _project_files() -> list[Path]:
    files = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() == _THIS_FILE:
            continue
        if path.suffix.lower() not in _SCANNED_SUFFIXES:
            continue
        if _EXCLUDED_DIR_NAMES & set(path.relative_to(REPO_ROOT).parts):
            continue
        files.append(path)
    return files


def _grep(files: list[Path], needles: tuple[str, ...]) -> list[str]:
    """Relative paths of files containing any of ``needles``, verbatim."""
    hits = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(needle in text for needle in needles):
            hits.append(str(path.relative_to(REPO_ROOT)))
    return hits


def test_uses_anthropic_api():
    """The project is expected to import/use the Anthropic (Claude) SDK or
    client library, or call the Anthropic API directly, somewhere in its
    source."""
    files = _project_files()
    hits = _grep(files, _ANTHROPIC_USAGE_SIGNALS)
    assert hits, (
        "no reference to the Anthropic/Claude SDK or API "
        f"(looked for any of {_ANTHROPIC_USAGE_SIGNALS!r}) was found under {REPO_ROOT}"
    )


def test_declares_anthropic_api_key():
    """The project is expected to declare an ANTHROPIC_API_KEY (or equivalent)
    somewhere in its configuration or environment handling."""
    files = _project_files()
    hits = _grep(files, _ANTHROPIC_API_KEY_SIGNALS)
    assert hits, (
        "no declaration of an Anthropic API key "
        f"(looked for any of {_ANTHROPIC_API_KEY_SIGNALS!r}) was found under {REPO_ROOT}"
    )
