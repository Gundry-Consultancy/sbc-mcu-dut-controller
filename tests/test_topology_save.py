"""Saving the live DB back into the topology seed file (comments preserved)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from hil_controller.db.connection import get_db, init_db
from hil_controller.hosts.registry import RealHostRegistry
from hil_controller.topology.drift import topology_drift
from hil_controller.topology.save import plan_save, save_topology
from hil_controller.topology.seeder import seed_topology

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((REPO / "deploy").glob("topology*.example.yaml"))
TOKEN = "test-token-for-ci"
COOKIE = {"hil_token": TOKEN}
AUTH = {"Authorization": f"Bearer {TOKEN}"}

COMMENTED = """\
# Bench topology — hand-maintained header comment.
hosts:
  # the Pi fleet
  - id: rpi-a
    addr: 10.0.0.1          # LAN address
    capabilities: [power-control]
devices:
  # --- python-snapper SBC DUTs ---
  - id: pi-dut
    host_id: rpi-a
    kind: sbc
    model: pi5              # WRONG on purpose: it's a Pi 4
    pool: wippersnapper-python
  - id: gone-dut
    host_id: rpi-a
    kind: microcontroller
"""


async def _seeded(tmp_path: Path, text_or_path) -> tuple[str, Path]:
    topo = tmp_path / "topology.yaml"
    if isinstance(text_or_path, Path):
        topo.write_text(text_or_path.read_text())
    else:
        topo.write_text(text_or_path)
    db_file = str(tmp_path / "save.db")
    await init_db(db_file)
    await seed_topology(db_file, str(topo))
    return db_file, topo


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
async def test_saving_a_freshly_seeded_example_changes_nothing(tmp_path, example):
    db_file, topo = await _seeded(tmp_path, example)
    plan = await plan_save(db_file, str(topo))
    assert plan["changes"] == [] and plan["diff"] == ""
    result = await save_topology(db_file, str(topo))
    assert result["written"] is False and result["backup"] is None
    assert topo.read_text() == example.read_text()


async def test_save_edits_in_place_keeps_comments_and_reseeds_exactly(tmp_path):
    db_file, topo = await _seeded(tmp_path, COMMENTED)
    async with get_db(db_file) as db:
        await db.execute("UPDATE devices SET model='pi4' WHERE id='pi-dut'")
        await db.execute("DELETE FROM devices WHERE id='gone-dut'")
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, model, capabilities_json, pool) "
            "VALUES ('new-dut', 'rpi-a', 'microcontroller', 'QT Py', '[\"i2c\"]', 'public')"
        )
        await db.commit()
    result = await save_topology(db_file, str(topo))
    assert result["written"] is True
    assert Path(result["backup"]).read_text() == COMMENTED
    text = topo.read_text()
    for comment in ("# Bench topology", "# the Pi fleet", "# LAN address",
                    "# --- python-snapper SBC DUTs ---"):
        assert comment in text
    assert "model: pi4" in text and "pi5" not in text
    assert "gone-dut" not in text and "new-dut" in text
    by_change = {(c["id"], c["change"]) for c in result["changes"]}
    assert by_change == {("pi-dut", "updated"), ("gone-dut", "removed"), ("new-dut", "added")}
    assert result["drift_after"] == {"reverted_on_restart": 0, "db_only": 0,
                                     "recreated_on_restart": 0}
    # the saved file re-seeds a fresh DB to exactly the live state
    assert (await topology_drift(db_file, str(topo)))["items"] == []
    (tmp_path / "fresh").mkdir()
    fresh_db = str(tmp_path / "fresh" / "f.db")
    await init_db(fresh_db)
    await seed_topology(fresh_db, str(topo))
    assert (await topology_drift(fresh_db, str(topo)))["items"] == []
    # no defaults sprayed into untouched rows
    assert "hub_host_id" not in text and "status: available" not in text
    # unchanged sections are byte-for-byte; new rows use inline lists
    hosts_block = COMMENTED.split("devices:", 1)[0]
    assert text.startswith(hosts_block)
    assert "capabilities: [i2c]" in text


async def test_save_without_a_topology_file_configured_is_refused(tmp_path):
    db_file = str(tmp_path / "x.db")
    await init_db(db_file)
    with pytest.raises(ValueError, match="no topology file"):
        await plan_save(db_file, "")


@pytest.fixture
async def app_topo(tmp_path):
    from hil_controller.main import create_app

    db_file, topo = await _seeded(tmp_path, COMMENTED)
    os.environ["HIL_DB_PATH"] = db_file
    app = create_app(db_path=db_file, topology_file="")
    async with app.router.lifespan_context(app):
        app.state.topology_file = str(topo)
        app.state.host_registry = RealHostRegistry(topology_file=str(topo), db_path=db_file)
        app.state.host_registry.load()
        yield app, db_file, topo


def _client(app) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=COOKIE, headers=AUTH
    )


async def test_patch_device_then_api_save_round_trip(app_topo):
    app, db_file, topo = app_topo
    async with _client(app) as c:
        r = await c.patch("/v1/devices/pi-dut", json={"model": "pi4"})
        assert r.status_code == 200 and r.json()["model"] == "pi4"
        # live for scheduling immediately
        match = app.state.host_registry.find_device_for_job(
            {"target": {"pool": "wippersnapper-python", "device": {"model": "pi4"}}}
        )
        assert match is not None and match[1]["id"] == "pi-dut"
        preview = await c.post("/v1/topology/save?dry_run=true")
        assert preview.status_code == 200
        body = preview.json()
        assert body["written"] is False and "-    model: pi5" in body["diff"]
        assert topo.read_text() == COMMENTED  # dry run wrote nothing
        saved = (await c.post("/v1/topology/save")).json()
        assert saved["written"] is True and saved["backup"]
        drift = (await c.get("/v1/topology/drift")).json()
    assert "model: pi4" in topo.read_text()
    assert drift["summary"]["reverted_on_restart"] == 0


async def test_patch_device_validation_and_outage_bookkeeping(app_topo):
    app, db_file, _ = app_topo
    async with _client(app) as c:
        assert (await c.patch("/v1/devices/nope", json={"model": "x"})).status_code == 404
        assert (await c.patch("/v1/devices/pi-dut", json={"model": None})).status_code == 422
        assert (await c.patch("/v1/devices/pi-dut", json={"bogus": 1})).status_code == 422
        assert (
            await c.patch("/v1/devices/pi-dut", json={"power_control": "solenoid"})
        ).status_code == 422
        assert (await c.patch("/v1/devices/pi-dut", json={"host_id": None})).status_code == 422
        assert (
            await c.patch("/v1/devices/pi-dut", json={"host_id": "no-such-host"})
        ).status_code == 422
        r = await c.patch("/v1/devices/pi-dut", json={"host_id": "rpi-a"})
        assert r.status_code == 200
        r = await c.patch(
            "/v1/devices/pi-dut",
            json={"status": "unavailable", "unavailable_reason": "re-imaging SD card"},
        )
        assert r.status_code == 200
        async with get_db(db_file) as db:
            async with db.execute("SELECT * FROM devices WHERE id='pi-dut'") as cur:
                row = await cur.fetchone()
        assert row["unavailable_kind"] == "permanent" and row["unavailable_since"]
        assert row["unavailable_reason"] == "re-imaging SD card"
        await c.patch("/v1/devices/pi-dut", json={"status": "available"})
        async with get_db(db_file) as db:
            async with db.execute("SELECT * FROM devices WHERE id='pi-dut'") as cur:
                row = await cur.fetchone()
    assert row["status"] == "available" and row["unavailable_reason"] is None


async def test_topology_page_preview_and_save_buttons(app_topo):
    app, db_file, topo = app_topo
    async with get_db(db_file) as db:
        await db.execute("UPDATE devices SET model='pi4' WHERE id='pi-dut'")
        await db.commit()
    async with _client(app) as c:
        page = await c.get("/ui/topology")
        preview = await c.post("/ui/topology/save?dry_run=1")
        assert topo.read_text() == COMMENTED
        saved = await c.post("/ui/topology/save")
        again = await c.post("/ui/topology/save?dry_run=1")
    assert "Save to topology file" in page.text and "Preview save" in page.text
    assert "Preview: 1 change(s)" in preview.text and "model: pi5" in preview.text
    assert "Saved" in saved.text and ".bak-" in saved.text
    assert "already matches the live DB" in again.text
    assert yaml.safe_load(topo.read_text())["devices"][0]["model"] == "pi4"


async def test_identical_duplicate_keys_are_collapsed_conflicting_ones_refused(tmp_path):
    dup = COMMENTED.replace("    pool: wippersnapper-python\n",
                            "    pool: wippersnapper-python\n    pool: wippersnapper-python\n")
    db_file, topo = await _seeded(tmp_path, dup)
    plan = await plan_save(db_file, str(topo))
    assert plan["changes"][0]["change"] == "collapsed duplicate key(s)"
    assert plan["new_text"].count("pool: wippersnapper-python") == 1
    assert "# LAN address" in plan["new_text"]
    clash = COMMENTED.replace("    pool: wippersnapper-python\n",
                              "    pool: wippersnapper-python\n    pool: public\n")
    topo.write_text(clash)
    with pytest.raises(ValueError, match="different values"):
        await plan_save(db_file, str(topo))
