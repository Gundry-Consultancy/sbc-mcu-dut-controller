"""Tests for the availability retry endpoints (reset the reconciler budget)."""

from __future__ import annotations

import pytest

from hil_controller.db.connection import get_db


async def _seed(app, *, device_id, status="unavailable", kind="temporary", attempts=3):
    async with get_db(app.state.db_path) as db:
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, model, status, unavailable_kind, "
            "retry_attempts, retry_after) VALUES (?, 'h1', 'mcu', 'feather', ?, ?, ?, "
            "'2026-01-01T00:00:00+00:00')",
            (device_id, status, kind, attempts),
        )
        await db.commit()


async def _row(app, device_id):
    async with get_db(app.state.db_path) as db:
        cur = await db.execute("SELECT * FROM devices WHERE id = ?", (device_id,))
        return dict(await cur.fetchone())


@pytest.mark.asyncio
async def test_retry_requires_auth(client, app):
    resp = await client.post("/v1/devices/d1/availability/retry")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_retry_single_resets_budget(authed_client, app):
    await _seed(app, device_id="d1")
    resp = await authed_client.post("/v1/devices/d1/availability/retry")
    assert resp.status_code == 200
    assert resp.json()["reset"] == ["d1"]
    row = await _row(app, "d1")
    assert row["retry_attempts"] == 0
    assert row["retry_after"] is None
    # status untouched — the reconciler's probe decides, not the endpoint
    assert row["status"] == "unavailable"


@pytest.mark.asyncio
async def test_retry_single_404_unknown(authed_client):
    resp = await authed_client.post("/v1/devices/nope/availability/retry")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retry_single_409_permanent(authed_client, app):
    await _seed(app, device_id="d-perm", kind="permanent")
    resp = await authed_client.post("/v1/devices/d-perm/availability/retry")
    assert resp.status_code == 409
    assert (await _row(app, "d-perm"))["retry_attempts"] == 3


@pytest.mark.asyncio
async def test_retry_bulk_resets_temporary_skips_permanent(authed_client, app):
    await _seed(app, device_id="d1")
    await _seed(app, device_id="d2", attempts=5)
    await _seed(app, device_id="d-perm", kind="permanent")
    resp = await authed_client.post("/v1/devices/availability/retry")
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body["reset"]) == ["d1", "d2"]
    assert body["skipped_permanent"] == ["d-perm"]
    assert (await _row(app, "d1"))["retry_attempts"] == 0
    assert (await _row(app, "d-perm"))["retry_attempts"] == 3
