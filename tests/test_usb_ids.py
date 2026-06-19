"""Tests for multi-VID/PID per device: schema, seeder, topology API."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_USB_TOPOLOGY = """
hosts:
  - id: hub-host-a
    role: microcontroller-fleet
    addr: 127.0.0.10
    transport: fake
    ssh_user: pi
    ssh_key_path: /tmp/k
    capabilities: [usbip-server, power-control]

devices:
  - id: dev-multi
    host_id: hub-host-a
    kind: microcontroller
    model: pyportal
    pool: public
    status: available
    hub_port_path: "1-1.1.3"
    solenoid_channel: 3
    usb_serial: "ABC123SERIAL"
    usb_ids:
      - { vid: "239a", pid: "8053", role: runtime,    description: "WipperSnapper" }
      - { vid: "239a", pid: "8054", role: runtime,    description: "CircuitPython" }
      - { vid: "239a", pid: "0035", role: bootloader, description: "UF2" }

  - id: dev-legacy
    host_id: hub-host-a
    kind: microcontroller
    model: feather-esp32s2
    pool: public
    status: available
    hub_port_path: "1-1.1.4"
    solenoid_channel: 4
    usb: { vid: "239a", pid: "80df" }

  - id: dev-no-usb
    host_id: hub-host-a
    kind: microcontroller
    model: blank
    pool: public
    status: available
"""


@pytest_asyncio.fixture
async def usb_app(tmp_path: Path):
    db_file = str(tmp_path / "usb.db")
    topo_file = tmp_path / "usb_topo.yaml"
    topo_file.write_text(_USB_TOPOLOGY)
    os.environ["HIL_DB_PATH"] = db_file

    from hil_controller.main import create_app

    app = create_app(db_path=db_file, topology_file=str(topo_file))
    async with app.router.lifespan_context(app):
        app.state._test_db_file = db_file
        yield app


@pytest_asyncio.fixture
async def usb_client(usb_app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=usb_app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token-for-ci"},
    ) as ac:
        yield ac


# -- schema --------------------------------------------------------------


@pytest.mark.asyncio
async def test_devices_table_has_new_columns(usb_app):
    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        async with db.execute("PRAGMA table_info(devices)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
    for c in ("hub_host_id", "hub_port_path", "solenoid_channel", "usb_serial"):
        assert c in cols, f"devices missing column {c}"


@pytest.mark.asyncio
async def test_device_usb_ids_table_exists_with_surrogate_pk(usb_app):
    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        async with db.execute("PRAGMA table_info(device_usb_ids)") as cur:
            cols = {row[1]: row for row in await cur.fetchall()}
        # PK is the surrogate id column
        pk_cols = [name for name, info in cols.items() if info[5] > 0]
        assert pk_cols == ["id"], f"PK should be 'id', got {pk_cols}"
        for col in (
            "device_id",
            "vid",
            "pid",
            "role",
            "iserial",
            "bcd_device",
            "description",
            "first_seen_at",
            "last_seen_at",
            "learned_from_job",
            "source",
        ):
            assert col in cols, f"device_usb_ids missing column {col}"


@pytest.mark.asyncio
async def test_device_usb_ids_unique_dedup(usb_app):
    """UNIQUE (device_id, vid, pid, iserial) must reject exact duplicates."""
    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        await db.execute(
            "INSERT INTO device_usb_ids "
            "(device_id, vid, pid, role, iserial, first_seen_at, last_seen_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("dev-multi", "ffff", "ffff", "unknown", "X", "now", "now", "manual"),
        )
        await db.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO device_usb_ids "
                "(device_id, vid, pid, role, iserial, first_seen_at, last_seen_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("dev-multi", "ffff", "ffff", "unknown", "X", "now", "now", "manual"),
            )
            await db.commit()


# -- seeder --------------------------------------------------------------


@pytest.mark.asyncio
async def test_seeder_populates_usb_ids_from_list(usb_app):
    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT vid, pid, role, description, source FROM device_usb_ids "
            "WHERE device_id=? ORDER BY pid",
            ("dev-multi",),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 3
    pids = {r["pid"] for r in rows}
    assert pids == {"8053", "8054", "0035"}
    uf2 = next(r for r in rows if r["pid"] == "0035")
    assert uf2["role"] == "bootloader"
    assert uf2["description"] == "UF2"
    assert uf2["source"] == "seeder"


@pytest.mark.asyncio
async def test_seeder_legacy_usb_block_creates_one_row(usb_app):
    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT vid, pid, role, source FROM device_usb_ids WHERE device_id=?",
            ("dev-legacy",),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["vid"] == "239a"
    assert rows[0]["pid"] == "80df"
    assert rows[0]["role"] == "unknown"
    assert rows[0]["source"] == "seeder"


@pytest.mark.asyncio
async def test_seeder_no_usb_means_no_rows(usb_app):
    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM device_usb_ids WHERE device_id=?",
            ("dev-no-usb",),
        ) as cur:
            (n,) = await cur.fetchone()
    assert n == 0


@pytest.mark.asyncio
async def test_seeder_writes_device_hub_columns(usb_app):
    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT hub_host_id, hub_port_path, solenoid_channel, usb_serial "
            "FROM devices WHERE id=?",
            ("dev-multi",),
        ) as cur:
            row = dict(await cur.fetchone())
    # hub_host_id defaults to host_id when omitted from YAML
    assert row["hub_host_id"] == "hub-host-a"
    assert row["hub_port_path"] == "1-1.1.3"
    assert row["solenoid_channel"] == 3
    assert row["usb_serial"] == "ABC123SERIAL"


@pytest.mark.asyncio
async def test_seeder_is_idempotent(usb_app, tmp_path):
    """Re-running seed must not duplicate device_usb_ids rows."""
    from hil_controller.topology.seeder import seed_topology

    topo_file = tmp_path / "usb_topo.yaml"  # already written by fixture
    await seed_topology(usb_app.state._test_db_file, str(topo_file))

    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM device_usb_ids WHERE device_id=?", ("dev-multi",)
        ) as cur:
            (n,) = await cur.fetchone()
    assert n == 3, "re-seed should not duplicate usb_ids rows"


@pytest.mark.asyncio
async def test_reseed_preserves_runtime_hub_port_when_yaml_omits_it(usb_app, tmp_path):
    from hil_controller.topology.seeder import seed_topology

    topo_file = tmp_path / "usb_topo.yaml"
    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        await db.execute(
            "UPDATE devices SET hub_host_id=?, hub_port_path=? WHERE id=?",
            ("hub-host-a", "1-9.9.9", "dev-no-usb"),
        )
        await db.commit()

    await seed_topology(usb_app.state._test_db_file, str(topo_file))

    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT hub_host_id, hub_port_path FROM devices WHERE id=?",
            ("dev-no-usb",),
        ) as cur:
            row = dict(await cur.fetchone())
        async with db.execute(
            "SELECT detail_json FROM audit_log WHERE event=? AND entity_id=? ORDER BY id DESC LIMIT 1",  # noqa: E501
            ("topology.seed.device_drift", "dev-no-usb"),
        ) as cur:
            audit = await cur.fetchone()
    assert row["hub_host_id"] == "hub-host-a"
    assert row["hub_port_path"] == "1-9.9.9"
    assert audit is not None
    detail = json.loads(audit["detail_json"])
    assert detail["fields"]["hub_port_path"] == {"db": "1-9.9.9", "seed": None}


@pytest.mark.asyncio
async def test_reseed_preserves_runtime_hub_port_when_yaml_disagrees(usb_app, tmp_path):
    from hil_controller.topology.seeder import seed_topology

    topo_file = tmp_path / "usb_topo.yaml"
    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        await db.execute(
            "UPDATE devices SET hub_host_id=?, hub_port_path=? WHERE id=?",
            ("hub-host-a", "1-7.7.7", "dev-legacy"),
        )
        await db.commit()

    await seed_topology(usb_app.state._test_db_file, str(topo_file))

    async with aiosqlite.connect(usb_app.state._test_db_file) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT hub_host_id, hub_port_path FROM devices WHERE id=?",
            ("dev-legacy",),
        ) as cur:
            row = dict(await cur.fetchone())
        async with db.execute(
            "SELECT detail_json FROM audit_log WHERE event=? AND entity_id=? ORDER BY id DESC LIMIT 1",  # noqa: E501
            ("topology.seed.device_drift", "dev-legacy"),
        ) as cur:
            audit = await cur.fetchone()
    assert row["hub_host_id"] == "hub-host-a"
    assert row["hub_port_path"] == "1-7.7.7"
    assert audit is not None
    detail = json.loads(audit["detail_json"])
    assert detail["fields"]["hub_port_path"] == {"db": "1-7.7.7", "seed": "1-1.1.4"}


# -- topology API --------------------------------------------------------


@pytest.mark.asyncio
async def test_topology_api_returns_usb_ids_list(usb_client):
    r = await usb_client.get("/v1/topology")
    assert r.status_code == 200
    body = r.json()
    multi = next(d for d in body["devices"] if d["id"] == "dev-multi")
    assert "usb_ids" in multi
    ids = multi["usb_ids"]
    assert len(ids) == 3
    pids = {x["pid"] for x in ids}
    assert pids == {"8053", "8054", "0035"}
    # Each row carries role + description + source
    bootloader = next(x for x in ids if x["pid"] == "0035")
    assert bootloader["role"] == "bootloader"


@pytest.mark.asyncio
async def test_topology_api_device_includes_hub_fields(usb_client):
    r = await usb_client.get("/v1/topology")
    body = r.json()
    multi = next(d for d in body["devices"] if d["id"] == "dev-multi")
    assert multi["hub_host_id"] == "hub-host-a"
    assert multi["hub_port_path"] == "1-1.1.3"
    assert multi["solenoid_channel"] == 3
    assert multi["usb_serial"] == "ABC123SERIAL"


@pytest.mark.asyncio
async def test_topology_api_no_usb_returns_empty_list(usb_client):
    r = await usb_client.get("/v1/topology")
    body = r.json()
    bare = next(d for d in body["devices"] if d["id"] == "dev-no-usb")
    assert bare["usb_ids"] == []


# -- migration of pre-existing usb_json -----------------------------------


@pytest.mark.asyncio
async def test_migration_backfills_existing_usb_json(tmp_path: Path):
    """A DB that pre-dates device_usb_ids must backfill from usb_json."""
    import json as _json

    db_file = str(tmp_path / "legacy.db")

    # Build a "legacy" DB: devices table only, with usb_json populated.
    async with aiosqlite.connect(db_file) as db:
        await db.executescript(
            """
            CREATE TABLE devices (
                id TEXT PRIMARY KEY, host_id TEXT NOT NULL, kind TEXT NOT NULL,
                model TEXT, capabilities_json TEXT DEFAULT '[]', usb_json TEXT,
                pool TEXT DEFAULT 'public', status TEXT DEFAULT 'available',
                serial_port TEXT, flasher TEXT
            );
            CREATE TABLE hosts (id TEXT PRIMARY KEY);
            """
        )
        await db.execute("INSERT INTO hosts (id) VALUES ('h1')")
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, usb_json) VALUES (?, ?, ?, ?)",
            ("legacy-dev", "h1", "microcontroller", _json.dumps({"vid": "239a", "pid": "8053"})),
        )
        await db.commit()

    # Run init_db, which should add columns + table + backfill.
    from hil_controller.db.connection import init_db

    await init_db(db_file)

    async with aiosqlite.connect(db_file) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT vid, pid, source FROM device_usb_ids WHERE device_id=?",
            ("legacy-dev",),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["vid"] == "239a"
    assert rows[0]["pid"] == "8053"
    assert rows[0]["source"] == "migration"
