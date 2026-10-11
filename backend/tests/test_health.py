"""`/health` reports each dependency separately and never fails the request."""

import asyncio
import socket
import time

import httpx
import pytest

from api.routes import health as health_module


def _fixed(value: str):
    async def _probe(*_args, **_kwargs) -> str:
        return value

    return _probe


def _patch_probes(monkeypatch, *, db: str, temporal: str, ollama: str) -> None:
    monkeypatch.setattr(health_module, "check_db", _fixed(db))
    monkeypatch.setattr(health_module, "check_temporal", _fixed(temporal))
    monkeypatch.setattr(health_module, "check_ollama", _fixed(ollama))


def test_all_dependencies_up(client, monkeypatch):
    _patch_probes(monkeypatch, db="ok", temporal="ok", ollama="ok")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "db": "ok",
        "temporal": "ok",
        "ollama": "ok",
    }


def test_everything_down_still_returns_200(client, monkeypatch):
    """Spec 01 §13: a dependency being down must not crash the endpoint."""
    _patch_probes(monkeypatch, db="down", temporal="down", ollama="down")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "db": "down",
        "temporal": "down",
        "ollama": "down",
    }


def test_one_dependency_down_does_not_affect_the_others(client, monkeypatch):
    _patch_probes(monkeypatch, db="down", temporal="ok", ollama="ok")

    body = client.get("/health").json()

    assert body["db"] == "down"
    assert body["temporal"] == "ok"
    assert body["ollama"] == "ok"


def test_response_has_exactly_the_documented_keys(client, monkeypatch):
    _patch_probes(monkeypatch, db="ok", temporal="ok", ollama="ok")

    assert set(client.get("/health").json()) == {"status", "db", "temporal", "ollama"}


# --- the probes themselves -------------------------------------------------


def test_check_db_maps_a_failed_ping_to_down(monkeypatch):
    monkeypatch.setattr(health_module, "ping_db", lambda: False)

    assert asyncio.run(health_module.check_db()) == "down"


def test_check_db_maps_a_successful_ping_to_ok(monkeypatch):
    monkeypatch.setattr(health_module, "ping_db", lambda: True)

    assert asyncio.run(health_module.check_db()) == "ok"


def test_check_db_gives_up_rather_than_hanging(monkeypatch):
    """A blocking `ping_db` must not hold the response open.

    libpq waits indefinitely without `connect_timeout`, and a blocked thread
    cannot be cancelled — so the probe bounds its own wait.
    """
    monkeypatch.setattr(health_module, "PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(health_module, "ping_db", lambda: time.sleep(0.5) or True)

    started = time.monotonic()
    result = asyncio.run(health_module.check_db())

    assert result == "down"
    assert time.monotonic() - started < 0.4


def test_check_temporal_against_a_listening_port():
    """Loopback only — proves the probe recognises something listening.

    A bare listening socket rather than `asyncio.start_server`: on Windows'
    Proactor loop, tearing down a server that still has a live connection
    deadlocks in `wait_closed()`.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        assert asyncio.run(health_module.check_temporal(f"127.0.0.1:{port}")) == "ok"


def test_check_temporal_against_a_closed_port():
    # Bind to claim a free port, then release it so nothing is listening.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    assert asyncio.run(health_module.check_temporal(f"127.0.0.1:{port}")) == "down"


@pytest.mark.parametrize("address", ["localhost", "localhost:not-a-port", ""])
def test_check_temporal_rejects_a_malformed_address(address):
    assert asyncio.run(health_module.check_temporal(address)) == "down"


def test_check_ollama_is_down_when_the_host_refuses(monkeypatch):
    async def _raise(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", _raise)

    assert asyncio.run(health_module.check_ollama("http://localhost:11434")) == "down"


def test_check_ollama_is_down_on_a_non_200(monkeypatch):
    async def _not_found(*_args, **_kwargs):
        return httpx.Response(404)

    monkeypatch.setattr(httpx.AsyncClient, "get", _not_found)

    assert asyncio.run(health_module.check_ollama("http://localhost:11434")) == "down"


def test_check_ollama_is_ok_on_200(monkeypatch):
    captured: dict[str, str] = {}

    async def _tags(_self, url, *_args, **_kwargs):
        captured["url"] = str(url)
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", _tags)

    # The trailing slash must not produce a doubled path.
    assert asyncio.run(health_module.check_ollama("http://localhost:11434/")) == "ok"
    assert captured["url"] == "http://localhost:11434/api/tags"
