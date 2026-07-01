"""Seeding I2C strands + components + per-DUT routes from topology YAML."""

from hil_controller.db.connection import get_db, init_db
from hil_controller.topology.seeder import seed_topology

EXAMPLE = "deploy/topology.strands.example.yaml"


async def _seed(tmp_path):
    db = str(tmp_path / "strands.db")
    await init_db(db)
    await seed_topology(db, EXAMPLE)
    return db


async def _rows(db, sql, params=()):
    async with get_db(db) as d:
        cur = await d.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def test_seeds_strand_components_and_routes(tmp_path):
    db = await _seed(tmp_path)
    s = (await _rows(db, "SELECT * FROM strands WHERE id='strand-hil006-air'"))[0]
    assert s["mux_aux_id"] == "mux-hil006"
    assert s["mux_group"] == "muxA"
    assert s["tca_address"] == 0x70

    comps = await _rows(
        db,
        "SELECT model, address, tca_channel FROM strand_components "
        "WHERE strand_id='strand-hil006-air'",
    )
    by_model = {c["model"]: c for c in comps}
    assert by_model["pmsa003i"]["address"] == 0x12
    assert by_model["pmsa003i"]["tca_channel"] is None  # direct bus
    assert by_model["sgp41"]["tca_channel"] == 0  # behind on-strand TCA

    routes = {
        r["device_id"]: r["mux_channel"]
        for r in await _rows(db, "SELECT device_id, mux_channel FROM device_strands")
    }
    assert routes["mcu-qtpy-esp32s3-hil006"] == 0
    assert routes["sbc-rpi-hil006-self"] == 3  # the Pi itself is ch3


async def test_reseed_is_idempotent(tmp_path):
    db = await _seed(tmp_path)
    await seed_topology(db, EXAMPLE)  # second pass must not duplicate
    assert (await _rows(db, "SELECT COUNT(*) c FROM strand_components"))[0]["c"] == 2
    assert (await _rows(db, "SELECT COUNT(*) c FROM device_strands"))[0]["c"] == 4
