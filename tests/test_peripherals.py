"""Tests for the peripherals topology section: seeder, DB, and API."""

import pytest

# ---------------------------------------------------------------------------
# Seeder — peripherals table populated from topology.yaml
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seeder_creates_peripheral_records(seeded_client):
    """Topology seed must create peripheral rows accessible via topology API."""
    r = await seeded_client.get("/v1/topology")
    assert r.status_code == 200
    body = r.json()
    periph_ids = {p["id"] for p in body["peripherals"]}
    assert "fake-oled-periph-01" in periph_ids


@pytest.mark.asyncio
async def test_seeder_peripheral_fields_are_correct(seeded_client):
    r = await seeded_client.get("/v1/topology")
    assert r.status_code == 200
    periph = next(p for p in r.json()["peripherals"] if p["id"] == "fake-oled-periph-01")
    assert periph["kind"] == "display"
    assert periph["model"] == "OLED 128x32"
    assert periph["product_url"] == "https://adafru.it/2900"
    assert periph["notes"] == "Monochrome OLED FeatherWing 128x32"


@pytest.mark.asyncio
async def test_seeder_links_device_to_peripheral(seeded_client):
    """device_peripherals junction must be seeded from peripheral_ids on devices."""
    r = await seeded_client.get("/v1/topology")
    assert r.status_code == 200
    body = r.json()
    qtpy = next(d for d in body["devices"] if d["id"] == "fake-qtpy-01")
    assert "peripheral_ids" in qtpy
    assert "fake-oled-periph-01" in qtpy["peripheral_ids"]


@pytest.mark.asyncio
async def test_device_without_peripherals_has_empty_list(seeded_client):
    """SBC device with no peripheral_ids must have an empty list, not missing key."""
    r = await seeded_client.get("/v1/topology")
    assert r.status_code == 200
    pi5 = next(d for d in r.json()["devices"] if d["id"] == "fake-pi5-01")
    assert "peripheral_ids" in pi5
    assert pi5["peripheral_ids"] == []


# ---------------------------------------------------------------------------
# DB integrity — foreign key references
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topology_peripherals_section_is_present(seeded_client):
    r = await seeded_client.get("/v1/topology")
    assert r.status_code == 200
    assert "peripherals" in r.json()


@pytest.mark.asyncio
async def test_topology_requires_auth_for_peripherals(client):
    r = await client.get("/v1/topology")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Peripherals UI form — structured specs + device association
# ---------------------------------------------------------------------------

import os  # noqa: E402

TOKEN = "test-token-for-ci"


async def _mk_host_device(client, host_id, device_id):
    await client.post(
        "/ui/hosts",
        data={
            "id": host_id,
            "role": "sbc-fleet",
            "addr": "10.0.0.9",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies={"hil_token": TOKEN},
    )
    await client.post(
        "/ui/devices",
        data={
            "id": device_id,
            "host_id": host_id,
            "kind": "sbc",
            "model": "pi5",
            "pool": "wippersnapper-python",
            "status": "available",
        },
        cookies={"hil_token": TOKEN},
    )


@pytest.mark.asyncio
async def test_create_peripheral_with_specs_and_association(client):
    import aiosqlite

    await _mk_host_device(client, "p-host-01", "p-dev-01")
    r = await client.post(
        "/ui/peripherals",
        data={
            "id": "periph-eink-37-mono",
            "kind": "display",
            "model": "3.7in Monochrome eInk Bare Display (416x240)",
            "product_url": "https://adafru.it/6395",
            "resolution": "416x240",
            "controller": "UC8253",
            "interface": "SPI",
            "notes": "bare panel",
            "device_ids": ["p-dev-01"],
        },
        cookies={"hil_token": TOKEN},
    )
    assert r.status_code < 400, r.text

    async with aiosqlite.connect(os.environ["HIL_DB_PATH"]) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT specs_json FROM peripherals WHERE id='periph-eink-37-mono'"
        ) as cur:
            row = await cur.fetchone()
        async with db.execute(
            "SELECT device_id FROM device_peripherals WHERE peripheral_id='periph-eink-37-mono'"
        ) as cur:
            assoc = [r2["device_id"] for r2 in await cur.fetchall()]
    import json as _json

    specs = _json.loads(row["specs_json"])
    assert specs == {"resolution": "416x240", "controller": "UC8253", "interface": "SPI"}
    assert assoc == ["p-dev-01"]


@pytest.mark.asyncio
async def test_update_peripheral_resyncs_associations(client):
    import aiosqlite

    await _mk_host_device(client, "p-host-02", "p-dev-02a")
    await _mk_host_device(client, "p-host-03", "p-dev-02b")
    await client.post(
        "/ui/peripherals",
        data={
            "id": "periph-x",
            "model": "x",
            "device_ids": ["p-dev-02a"],
        },
        cookies={"hil_token": TOKEN},
    )
    # Re-point the association to a different device.
    await client.post(
        "/ui/peripherals/periph-x",
        data={
            "model": "x",
            "device_ids": ["p-dev-02b"],
        },
        cookies={"hil_token": TOKEN},
    )
    async with aiosqlite.connect(os.environ["HIL_DB_PATH"]) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT device_id FROM device_peripherals WHERE peripheral_id='periph-x'"
        ) as cur:
            assoc = [r["device_id"] for r in await cur.fetchall()]
    assert assoc == ["p-dev-02b"]
