"""M0 tests: /healthz and /readyz endpoints."""

import pytest


@pytest.mark.asyncio
async def test_healthz_returns_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_returns_ok_after_startup(client):
    r = await client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "db" in body


@pytest.mark.asyncio
async def test_readyz_unauthenticated(client):
    """Readiness probe must be callable without auth."""
    r = await client.get("/readyz")
    assert r.status_code == 200
