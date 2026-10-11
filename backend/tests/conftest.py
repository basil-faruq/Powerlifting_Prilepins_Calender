"""Shared test fixtures.

No test in this suite touches a real database, a real Temporal server, a real
Ollama or the network. The three `/health` probes are patched; everything else is
pure Python.
"""

import pytest
from fastapi.testclient import TestClient

from planner_core.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`get_settings` is cached, so monkeypatched env vars need the cache dropped."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    from api.main import app

    return TestClient(app)
