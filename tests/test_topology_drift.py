"""DB-vs-topology drift: what a restart would revert, and export round-trips."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from hil_controller.db.connection import get_db, init_db
from hil_controller.topology.drift import topology_drift
from hil_controller.topology.export import build_export_yaml
from hil_controller.topology.seeder import seed_topology

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((REPO / "deploy").glob("topology*.example.yaml"))
COOKIE = {"hil_token": "test-token-for-ci"}


async def _seeded(tmp_path: Path, topology: Path) -> str:
    db_file = str(tmp_path / "drift.db")
    await init_db(db_file)
    await seed_topology(db_file, str(topology))
    return db_file


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
async def test_freshly_seeded_example_has_no_drift(tmp_path, example):
    db_file = await _seeded(tmp_path, example)
    drift = await topology_drift(db_file, str(example))
    assert drift["items"] == [], drift["items"]
    assert drift["topology_exists"] is True


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
async def test_export_round_trips_through_the_seeder(tmp_path, example):
    db_file = await _seeded(tmp_path, example)
    exported = tmp_path / "exported.yaml"
    exported.write_text(await build_export_yaml(db_file))
    (tmp_path / "again").mkdir()
    db2 = await _seeded(tmp_path / "again", exported)
    assert (await topology_drift(db2, str(exported)))["items"] == []
    # and the original DB matches its own export
    assert (await topology_drift(db_file, str(exported)))["items"] == []


async def test_drift_classifies_reverted_db_only_and_recreated(tmp_path):
    topo = tmp_path / "topology.yaml"
    topo.write_text(
        yaml.safe_dump(
            {
                "hosts": [{"id": "h1", "addr": "10.0.0.1"}],
                "devices": [
                    {"id": "d1", "host_id": "h1", "kind": "microcontroller"},
                    {"id": "d2", "host_id": "h1", "kind": "microcontroller"},
                ],
            }
        )
    )
    db_file = await _seeded(tmp_path, topo)
    async with get_db(db_file) as db:
        await db.execute("UPDATE devices SET power_control='bridge-port', pool='lab' WHERE id='d1'")
        # runtime wiring is preserved by the seeder: not drift
        await db.execute("UPDATE devices SET hub_port_path='1-1.4' WHERE id='d1'")
        await db.execute("DELETE FROM devices WHERE id='d2'")
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, capabilities_json) "
            "VALUES ('d3', 'h1', 'microcontroller', '[]')"
        )
        await db.commit()
    drift = await topology_drift(db_file, str(topo))
    by_id = {(i["section"], i["id"]): i for i in drift["items"]}
    d1 = by_id[("devices", "d1")]
    assert d1["change"] == "reverted_on_restart"
    assert set(d1["fields"]) == {"power_control", "pool"}
    assert d1["fields"]["pool"] == {"db": "lab", "yaml": "public"}
    assert by_id[("devices", "d2")]["change"] == "recreated_on_restart"
    assert by_id[("devices", "d3")]["change"] == "db_only"
    assert drift["summary"] == {
        "reverted_on_restart": 1,
        "db_only": 1,
        "recreated_on_restart": 1,
    }
    # and the seeder really does what the drift report says
    await seed_topology(db_file, str(topo))
    async with get_db(db_file) as db:
        async with db.execute("SELECT * FROM devices WHERE id='d1'") as cur:
            row = await cur.fetchone()
    assert row["power_control"] is None and row["pool"] == "public"
    assert row["hub_port_path"] == "1-1.4"


async def test_topology_page_banner_and_download(tmp_path):
    from hil_controller.main import create_app

    topo = tmp_path / "topology.yaml"
    topo.write_text(yaml.safe_dump({"hosts": [{"id": "h1", "addr": "10.0.0.1"}]}))
    db_file = str(tmp_path / "ui.db")
    os.environ["HIL_DB_PATH"] = db_file
    app = create_app(db_path=db_file, topology_file="")
    async with app.router.lifespan_context(app):
        await seed_topology(db_file, str(topo))
        app.state.topology_file = str(topo)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", cookies=COOKIE
        ) as c:
            clean = await c.get("/ui/topology/banner")
            await c.post(
                "/ui/hosts/h1", data={"id": "h1", "addr": "10.0.0.99", "transport": "ssh"}
            )
            banner = await c.get("/ui/topology/banner")
            page = await c.get("/ui/topology")
            download = await c.get("/ui/topology/export.yaml")
            api = await c.get(
                "/v1/topology/drift", headers={"Authorization": "Bearer test-token-for-ci"}
            )
    assert clean.text.strip() == ""
    assert "will be reverted by the next controller restart" in banner.text
    assert "10.0.0.99" in page.text and "reverted on restart" in page.text
    assert download.headers["content-disposition"].startswith("attachment")
    assert yaml.safe_load(download.text)["hosts"][0]["addr"] == "10.0.0.99"
    assert api.json()["summary"]["reverted_on_restart"] == 1
