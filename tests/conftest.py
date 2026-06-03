"""Shared pytest fixtures for the twohundredsma test suite.

Every test runs against an isolated temporary SQLite database (the Store
factory falls back to SQLite when ``COSMOS_CONNECTION_STRING`` is unset). We
reset the cached backend singleton between tests so each test starts with a
fresh DB.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the repo root importable so ``from advisor import ...`` works.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_sqlite_store(tmp_path, monkeypatch):
    """Point every test at a private SQLite DB and reset the Store singleton."""
    # Force the factory to choose SQLite even if the dev shell has Cosmos creds.
    monkeypatch.delenv("COSMOS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "andrewdikih")

    # The Store factory respects /data when writable. To avoid leaking into
    # the developer's /data dir, point the SQLite resolver at our tmp dir.
    from advisor import state as state_mod
    monkeypatch.setattr(
        state_mod, "DEFAULT_DB_PATHS",
        (str(tmp_path / "advisor.db"),),
    )
    state_mod._reset_store_singleton_for_tests()
    yield
    state_mod._reset_store_singleton_for_tests()
