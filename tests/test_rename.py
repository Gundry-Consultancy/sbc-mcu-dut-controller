"""Cascade-rename tests for hosts and devices (topology.rename)."""

from __future__ import annotations

import pytest

from hil_controller.db.connection import get_db, init_db, now_iso
from hil_controller.topology.rename import rename_device, rename_host


async def _seed(db_path: str) -> None:
    async with get_db(db_path) as db:
        await db.execute("INSERT INTO hosts (id, role) VALUES ('rpi-hil001', 'sbc-fleet')")
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, hub_host_id) "
            "VALUES ('rpi-hil001-pi5-a', 'rpi-hil001', 'sbc', 'rpi-hil001')"
        )
        await db.execute(
            "INSERT INTO device_usb_ids (device_id, vid, pid, first_seen_at, last_seen_at) "
            "VALUES ('rpi-hil001-pi5-a', '2e8a', '0003', ?, ?)",
            (now_iso(), now_iso()),
        )
        await db.execute(
            "INSERT INTO cameras (id, host_id, source) VALUES ('cam-1', 'rpi-hil001', 'csi')"
        )
        await db.execute(
            "INSERT INTO device_leases (device_id, hub_host_id, kind, acquired_at) "
            "VALUES ('rpi-hil001-pi5-a', 'rpi-hil001', 'exclusive_device', ?)",
            (now_iso(),),
        )
        await db.execute(
            "INSERT INTO jobs (id, request_json, created_at, assigned_host, assigned_device) "
            "VALUES ('job-1', '{}', ?, 'rpi-hil001', 'rpi-hil001-pi5-a')",
            (now_iso(),),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_rename_host_cascades(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_db(db_path)
    await _seed(db_path)

    async with get_db(db_path) as db:
        n = await rename_host(db, "rpi-hil001", "rpi-zerow-001")
    assert n >= 4  # devices.host_id, devices.hub_host_id, cameras, jobs, leases

    async with get_db(db_path) as db:
        assert await (await db.execute("SELECT 1 FROM hosts WHERE id='rpi-zerow-001'")).fetchone()
        assert not (
            await (await db.execute("SELECT 1 FROM hosts WHERE id='rpi-hil001'")).fetchone()
        )
        # No row still references the old host id.
        for table, col in [
            ("devices", "host_id"),
            ("devices", "hub_host_id"),
            ("cameras", "host_id"),
            ("jobs", "assigned_host"),
            ("device_leases", "hub_host_id"),
        ]:
            orphan = await (
                await db.execute(f"SELECT 1 FROM {table} WHERE {col}='rpi-hil001'")
            ).fetchone()
            assert orphan is None, f"{table}.{col} still points at old host id"


@pytest.mark.asyncio
async def test_rename_device_cascades(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_db(db_path)
    await _seed(db_path)

    async with get_db(db_path) as db:
        n = await rename_device(db, "rpi-hil001-pi5-a", "rpi-hil001-zerow-a")
    assert n >= 3  # usb_ids, leases, jobs.assigned_device

    async with get_db(db_path) as db:
        assert await (
            await db.execute("SELECT 1 FROM devices WHERE id='rpi-hil001-zerow-a'")
        ).fetchone()
        for table, col in [
            ("device_usb_ids", "device_id"),
            ("device_leases", "device_id"),
            ("jobs", "assigned_device"),
        ]:
            orphan = await (
                await db.execute(f"SELECT 1 FROM {table} WHERE {col}='rpi-hil001-pi5-a'")
            ).fetchone()
            assert orphan is None, f"{table}.{col} still points at old device id"


@pytest.mark.asyncio
async def test_rename_rejects_existing_target(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_db(db_path)
    await _seed(db_path)
    async with get_db(db_path) as db:
        await db.execute("INSERT INTO hosts (id, role) VALUES ('taken', 'sbc-fleet')")
        await db.commit()
        with pytest.raises(KeyError):
            await rename_host(db, "rpi-hil001", "taken")


@pytest.mark.asyncio
async def test_rename_rejects_bad_id(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_db(db_path)
    await _seed(db_path)
    async with get_db(db_path) as db:
        with pytest.raises(ValueError):
            await rename_host(db, "rpi-hil001", "has spaces")


@pytest.mark.asyncio
async def test_rename_same_id_is_noop(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_db(db_path)
    await _seed(db_path)
    async with get_db(db_path) as db:
        assert await rename_host(db, "rpi-hil001", "rpi-hil001") == 0
