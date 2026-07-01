"""CRUD API for strands + the topology backport (DB -> reseedable YAML)."""

import pytest

from hil_controller.db.connection import get_db, init_db
from hil_controller.topology.seeder import seed_topology

STRAND = {
    "id": "strand-t",
    "mux_aux": "mux1",
    "mux_group": "muxA",
    "tca_address": 0x70,
    "components": [
        {
            "id": "c1",
            "model": "pmsa003i",
            "address": 0x12,
            "tca_channel": None,
            "capabilities": ["sensor:pm25", "air-quality"],
        }
    ],
    "routes": [{"device": "devA", "channel": 0}],
}


async def _insert_device(app, dev_id):
    async with get_db(app.state.db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO devices (id, host_id, kind, model) VALUES (?, ?, ?, ?)",
            (dev_id, "h", "microcontroller", "m"),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_strands_requires_auth(client):
    assert (await client.get("/v1/strands")).status_code == 401


@pytest.mark.asyncio
async def test_strand_crud(app, authed_client):
    await _insert_device(app, "devA")

    assert (await authed_client.post("/v1/strands", json=STRAND)).status_code == 201

    got = (await authed_client.get("/v1/strands/strand-t")).json()
    assert got["mux_group"] == "muxA"
    assert got["tca_address"] == 0x70
    assert got["routes"] == [{"device": "devA", "channel": 0}]
    assert got["components"][0]["capabilities"] == ["sensor:pm25", "air-quality"]

    # Creating the same id again conflicts.
    assert (await authed_client.post("/v1/strands", json=STRAND)).status_code == 409

    # PUT replaces components/routes declaratively.
    new_comp = {"id": "c1", "model": "pmsa003i", "capabilities": ["sensor:pm10"]}
    upd = {**STRAND, "components": [new_comp]}
    assert (await authed_client.put("/v1/strands/strand-t", json=upd)).status_code == 200
    after = (await authed_client.get("/v1/strands/strand-t")).json()
    assert after["components"][0]["capabilities"] == ["sensor:pm10"]

    listing = (await authed_client.get("/v1/strands")).json()["strands"]
    assert any(s["id"] == "strand-t" for s in listing)

    assert (await authed_client.delete("/v1/strands/strand-t")).status_code == 204
    assert (await authed_client.get("/v1/strands/strand-t")).status_code == 404
    assert (await authed_client.delete("/v1/strands/strand-t")).status_code == 404


@pytest.mark.asyncio
async def test_export_backports_and_reseeds(app, authed_client, tmp_path):
    await _insert_device(app, "devA")
    await authed_client.post("/v1/strands", json=STRAND)

    resp = await authed_client.get("/v1/topology/export")
    assert resp.status_code == 200
    yaml_text = resp.text
    assert "strand-t" in yaml_text

    # The backported YAML must reseed into a fresh DB with the same strand.
    exported = tmp_path / "exported.yaml"
    exported.write_text(yaml_text)
    db2 = str(tmp_path / "reseed.db")
    await init_db(db2)
    await seed_topology(db2, str(exported))
    async with get_db(db2) as d:
        cur = await d.execute("SELECT mux_group, tca_address FROM strands WHERE id='strand-t'")
        row = dict(await cur.fetchone())
        assert row["mux_group"] == "muxA"
        assert row["tca_address"] == 0x70
        cur = await d.execute(
            "SELECT device_id, mux_channel FROM device_strands WHERE strand_id='strand-t'"
        )
        assert dict(await cur.fetchone()) == {"device_id": "devA", "mux_channel": 0}
