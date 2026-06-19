"""Tests for GET /v1/targets and the devices availability-column migration."""

from __future__ import annotations

import aiosqlite
import pytest

from hil_controller.db.connection import get_db, init_db

_AVAIL_COLS = {
    "unavailable_kind",
    "unavailable_reason",
    "unavailable_since",
    "retry_attempts",
    "retry_after",
    "last_checked_at",
}


async def _seed_device(
    db_path,
    *,
    device_id,
    model,
    status="available",
    kind=None,
    reason=None,
    retry_after=None,
):
    async with get_db(db_path) as db:
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, model, status, "
            "unavailable_kind, unavailable_reason, retry_after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, "host-1", "microcontroller", model, status, kind, reason, retry_after),
        )
        await db.commit()


async def _columns(db_path) -> set[str]:
    async with get_db(db_path) as db:
        cur = await db.execute("PRAGMA table_info('devices')")
        rows = await cur.fetchall()
    return {r["name"] for r in rows}


# --------------------------------------------------------------------------- #
# Endpoint                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_targets_requires_auth(client):
    r = await client.get("/v1/targets")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_targets_empty(authed_client):
    r = await authed_client.get("/v1/targets")
    assert r.status_code == 200
    assert r.json() == {"targets": []}


@pytest.mark.asyncio
async def test_targets_returns_available_and_unavailable(authed_client, app):
    db_path = app.state.db_path
    await _seed_device(db_path, device_id="d-avail", model="qtpy_esp32s3_n4r2")
    await _seed_device(
        db_path,
        device_id="d-temp",
        model="feather_esp32s2",
        status="unavailable",
        kind="temporary",
        reason="USB enumeration wedged",
        retry_after="2026-06-14T16:20:00Z",
    )
    await _seed_device(
        db_path,
        device_id="d-perm",
        model="metro_esp32s2",
        status="unavailable",
        kind="permanent",
        reason="not wired to bench",
    )

    r = await authed_client.get("/v1/targets")
    assert r.status_code == 200
    by_id = {t["device_id"]: t for t in r.json()["targets"]}
    assert len(by_id) == 3

    avail = by_id["d-avail"]
    assert avail["available"] is True
    assert avail["target"] == "qtpy_esp32s3_n4r2"
    assert avail["kind"] is None

    temp = by_id["d-temp"]
    assert temp["available"] is False
    assert temp["status"] == "unavailable"
    assert temp["kind"] == "temporary"
    assert temp["reason"] == "USB enumeration wedged"
    assert temp["retry_after"] == "2026-06-14T16:20:00Z"

    perm = by_id["d-perm"]
    assert perm["available"] is False
    assert perm["kind"] == "permanent"
    assert perm["reason"] == "not wired to bench"
    assert perm["retry_after"] is None


# --------------------------------------------------------------------------- #
# Migration                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_migration_adds_columns_to_fresh_db(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    await init_db(db_path)
    assert _AVAIL_COLS.issubset(await _columns(db_path))


@pytest.mark.asyncio
async def test_migration_adds_columns_to_existing_db(tmp_path):
    """A pre-availability devices table gets the columns added idempotently."""
    db_path = str(tmp_path / "old.db")
    # Simulate an existing DB whose devices table predates the availability cols.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE devices ("
            "id TEXT PRIMARY KEY, host_id TEXT NOT NULL, kind TEXT NOT NULL, "
            "model TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'available')"
        )
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, model) VALUES ('d1', 'h1', 'mcu', 'x')"
        )
        await db.commit()

    cols_before = await _columns(db_path)
    assert not (_AVAIL_COLS & cols_before)

    # First run adds the columns; second run is a clean no-op.
    await init_db(db_path)
    await init_db(db_path)

    assert _AVAIL_COLS.issubset(await _columns(db_path))
    # Existing row survived and reads as available via the endpoint policy.
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT retry_attempts FROM devices WHERE id='d1'")
        row = await cur.fetchone()
    assert row["retry_attempts"] == 0
