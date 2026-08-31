"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
MINI_CORPUS = FIXTURES_DIR / "mini_corpus"


@pytest.fixture(scope="session")
def mini_corpus() -> Path:
    """Path to the small committed corpus used across tests."""
    return MINI_CORPUS
