"""Tests for the device_leases primitive, REST endpoints, and startup sweep."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_TOPOLOGY = """
hosts:
  - id: hub-a
    role: microcontroller-fleet
    addr: 127.0.0.10
    transport: fake
    ssh_user: pi
    ssh_key_path: /tmp/k
    capabilities: [usbip-server]
  - id: hub-b
    role: microcontroller-fleet
    addr: 127.0.0.11
    transport: fake
    ssh_user: pi
    ssh_key_path: /tmp/k
    capabilities: [usbip-server]

devices:
  - id: a1
    host_id: hub-a
    kind: microcontroller
    pool: public
    status: available
  - id: a2
    host_id: hub-a
    kind: microcontroller
    pool: public
    status: available
  - id: b1
    host_id: hub-b
    kind: microcontroller
    pool: public
    status: available
"""


@pytest_asyncio.fixture
async def app(tmp_path: Path):
    db_file = str(tmp_path / "leases.db")
    topo = tmp_path / "t.yaml"
    topo.write_text(_TOPOLOGY)
    os.environ["HIL_DB_PATH"] = db_file
    from hil_controller.main import create_app

    a = create_app(db_path=db_file, topology_file=str(topo))
    async with a.router.lifespan_context(a):
        a.state._test_db = db_file
        yield a


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token-for-ci"},
    ) as ac:
        yield ac


# -- schema --------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_leases_table_exists(app):
    async with aiosqlite.connect(app.state._test_db) as db:
        async with db.execute("PRAGMA table_info(device_leases)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
    for c in (
        "id",
        "device_id",
        "hub_host_id",
        "job_id",
        "kind",
        "acquired_at",
        "expires_at",
        "released_at",
    ):
        assert c in cols


# -- acquire/release primitive (Python API) -------------------------------


@pytest.mark.asyncio
async def test_acquire_exclusive_device(app):
    from hil_controller.queue.leases import acquire, list_active, release

    lease = await acquire(app.state._test_db, kind="exclusive_device", device_id="a1", job_id="j1")
    assert lease["id"] > 0
    assert lease["device_id"] == "a1"
    assert lease["kind"] == "exclusive_device"

    active = await list_active(app.state._test_db)
    assert any(l["id"] == lease["id"] for l in active)

    await release(app.state._test_db, lease["id"])
    active2 = await list_active(app.state._test_db)
    assert not any(l["id"] == lease["id"] for l in active2)


@pytest.mark.asyncio
async def test_acquire_same_device_twice_conflicts(app):
    from hil_controller.queue.leases import LeaseConflict, acquire

    await acquire(app.state._test_db, kind="exclusive_device", device_id="a1", job_id="j1")
    with pytest.raises(LeaseConflict):
        await acquire(app.state._test_db, kind="exclusive_device", device_id="a1", job_id="j2")


@pytest.mark.asyncio
async def test_acquire_different_devices_no_conflict(app):
    from hil_controller.queue.leases import acquire

    await acquire(app.state._test_db, kind="exclusive_device", device_id="a1", job_id="j1")
    # Different device on same hub — should succeed
    await acquire(app.state._test_db, kind="exclusive_device", device_id="a2", job_id="j2")


@pytest.mark.asyncio
async def test_exclusive_hub_blocks_device_on_same_hub(app):
    from hil_controller.queue.leases import LeaseConflict, acquire

    await acquire(app.state._test_db, kind="exclusive_hub", hub_host_id="hub-a", job_id="learn-1")
    # Cannot now grab a1 (same hub)
    with pytest.raises(LeaseConflict):
        await acquire(app.state._test_db, kind="exclusive_device", device_id="a1", job_id="j2")
    # But b1 on hub-b is fine
    await acquire(app.state._test_db, kind="exclusive_device", device_id="b1", job_id="j3")


@pytest.mark.asyncio
async def test_exclusive_hub_blocked_by_device_on_same_hub(app):
    from hil_controller.queue.leases import LeaseConflict, acquire

    await acquire(app.state._test_db, kind="exclusive_device", device_id="a1", job_id="j1")
    with pytest.raises(LeaseConflict):
        await acquire(
            app.state._test_db, kind="exclusive_hub", hub_host_id="hub-a", job_id="learn-1"
        )


@pytest.mark.asyncio
async def test_release_allows_reacquire(app):
    from hil_controller.queue.leases import acquire, release

    l1 = await acquire(app.state._test_db, kind="exclusive_device", device_id="a1", job_id="j1")
    await release(app.state._test_db, l1["id"])
    l2 = await acquire(app.state._test_db, kind="exclusive_device", device_id="a1", job_id="j2")
    assert l2["id"] != l1["id"]


@pytest.mark.asyncio
async def test_acquire_validates_kind(app):
    from hil_controller.queue.leases import acquire

    with pytest.raises(ValueError):
        await acquire(app.state._test_db, kind="bogus", device_id="a1", job_id="j1")


@pytest.mark.asyncio
async def test_exclusive_device_resolves_hub_host_id(app):
    """If hub_host_id not provided, it must be looked up from devices table."""
    from hil_controller.queue.leases import acquire

    lease = await acquire(app.state._test_db, kind="exclusive_device", device_id="a1", job_id="j1")
    # a1 lives on hub-a → lease should record hub-a as hub_host_id
    assert lease["hub_host_id"] == "hub-a"


# -- REST endpoints ------------------------------------------------------


@pytest.mark.asyncio
async def test_post_lease_endpoint(client):
    r = await client.post(
        "/v1/leases",
        json={"kind": "exclusive_device", "device_id": "a1", "job_id": "j1"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["device_id"] == "a1"


@pytest.mark.asyncio
async def test_post_lease_conflict_returns_409(client):
    r1 = await client.post(
        "/v1/leases",
        json={"kind": "exclusive_device", "device_id": "a1", "job_id": "j1"},
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/v1/leases",
        json={"kind": "exclusive_device", "device_id": "a1", "job_id": "j2"},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_get_leases(client):
    await client.post(
        "/v1/leases",
        json={"kind": "exclusive_device", "device_id": "a1", "job_id": "j1"},
    )
    r = await client.get("/v1/leases")
    assert r.status_code == 200
    body = r.json()
    assert any(l["device_id"] == "a1" and l["released_at"] is None for l in body)


@pytest.mark.asyncio
async def test_delete_lease(client):
    r1 = await client.post(
        "/v1/leases",
        json={"kind": "exclusive_device", "device_id": "a1", "job_id": "j1"},
    )
    lease_id = r1.json()["id"]
    r2 = await client.delete(f"/v1/leases/{lease_id}")
    assert r2.status_code == 204
    # Now can re-acquire.
    r3 = await client.post(
        "/v1/leases",
        json={"kind": "exclusive_device", "device_id": "a1", "job_id": "j2"},
    )
    assert r3.status_code == 201


@pytest.mark.asyncio
async def test_delete_unknown_lease(client):
    r = await client.delete("/v1/leases/999999")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_lease_requires_auth(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/v1/leases")
        assert r.status_code == 401


# -- startup sweep -------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_sweep_releases_orphan_leases(tmp_path: Path):
    """Re-creating app must mark active leases whose job is not in active state as released."""
    db_file = str(tmp_path / "sweep.db")
    topo = tmp_path / "t.yaml"
    topo.write_text(_TOPOLOGY)
    os.environ["HIL_DB_PATH"] = db_file

    # First boot: acquire a lease then crash (simulate by closing without release).
    from hil_controller.main import create_app

    app1 = create_app(db_path=db_file, topology_file=str(topo))
    async with app1.router.lifespan_context(app1):
        from hil_controller.queue.leases import acquire

        # No matching active job in jobs table → sweep should release.
        await acquire(db_file, kind="exclusive_device", device_id="a1", job_id="ghost-job")

    # Second boot — startup sweep should mark ghost-job's lease released.
    app2 = create_app(db_path=db_file, topology_file=str(topo))
    async with app2.router.lifespan_context(app2):
        async with aiosqlite.connect(db_file) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT released_at FROM device_leases WHERE job_id='ghost-job'"
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        assert row["released_at"] is not None, "orphan lease not released by sweep"
