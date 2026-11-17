from __future__ import annotations

from pathlib import Path

import pytest

from vault_search.client import Client
from vault_search.config import Settings

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def vault_root() -> Path:
    return FIXTURES


@pytest.fixture
def client(vault_root: Path, tmp_path: Path) -> Client:
    settings = Settings(vault_root=vault_root, index_path=tmp_path / "index.db")
    instance = Client(settings)
    instance.reindex()
    try:
        yield instance
    finally:
        instance.close()
