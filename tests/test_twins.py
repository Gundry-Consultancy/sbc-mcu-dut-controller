"""Physical specimens, endpoint ownership and conservative discovery semantics."""

import asyncio
import json
import sqlite3
import sys

import pytest
from pydantic import ValidationError

from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db, init_db
from hil_controller.twin_import import import_batch, snapshot
from hil_controller.twin_models import Observation
from hil_controller.twins import TwinError


def observation(name="scan-1", **changes):
    body = {
        "observation_id": name,
        "source": "test:synthetic",
        "observed_at": "2026-01-01T00:00:00Z",
        "kind": "microcontroller",
        "label": "Synthetic board",
        "pool": "public",
        "identities": [
            {
                "namespace": "test.vendor:soc",
                "kind": "silicon_uid",
                "value": "FAKE-001",
                "owner": "chip",
                "component": "soc",
                "unique": True,
                "confidence": "verified",
                "evidence": "Synthetic silicon-register fixture",
            }
        ],
        "ports": [
            {
                "id": "usb-native",
                "kind": "usb",
                "mode": "runtime",
                "attributes": {
                    "vid": "ffff",
                    "pid": "0001",
                    "host_id": "fake-host",
                    "busid": "1-2",
                },
            }
        ],
        "attributes": {
            "architecture": "arm",
            "psram_bytes": 0,
            "build_profiles": [{"system": "arduino", "fqbn": "test:arm:board"}],
        },
    }
    body.update(changes)
    return body


async def create(client, body=None):
    response = await client.post("/v1/twins/observations", json=body or observation())
    assert response.status_code == 200, response.text
    return response.json()["twin_id"]


async def test_ingest_replay_and_conflicting_id(authed_client):
    twin = await create(authed_client)
    again = await authed_client.post("/v1/twins/observations", json=observation())
    assert again.json() == {"twin_id": twin, "observation_id": "scan-1", "replayed": True}
    changed = observation(label="Different payload")
    assert (await authed_client.post("/v1/twins/observations", json=changed)).status_code == 409
    detail = (await authed_client.get(f"/v1/twins/{twin}")).json()
    assert len(detail["observations"]) == 1
    assert detail["attributes"]["psram_bytes"] == 0
    assert detail["identities"][0]["unique"] is True
    assert detail["observations"][0]["actor"] == "static"


async def test_preview_candidates_and_no_writes(authed_client):
    proposed = await authed_client.post("/v1/twins/observations/preview", json=observation())
    assert proposed.json()["valid"] is True
    assert (await authed_client.get("/v1/twins")).json()["twins"] == []
    twin = await create(authed_client)
    later = observation("scan-2", observed_at="2026-01-02T00:00:00Z")
    proposed = (await authed_client.post("/v1/twins/observations/preview", json=later)).json()
    assert proposed["valid"] is False
    assert proposed["candidates"][0]["twin_id"] == twin
    assert (await authed_client.post("/v1/twins/observations", json=later)).status_code == 409
    later["twin_id"] = twin
    assert await create(authed_client, later) == twin
    assert len((await authed_client.get(f"/v1/twins/{twin}")).json()["observations"]) == 2


async def test_no_matching_on_model_address_vidpid_or_unverified_serial(authed_client):
    body = observation(identities=[], attributes={"model": "same", "i2c_address": 89})
    first = await create(authed_client, body)
    body["observation_id"] = "scan-2"
    proposed = (await authed_client.post("/v1/twins/observations/preview", json=body)).json()
    assert proposed["candidates"] == []
    second = await create(authed_client, body)
    assert first != second


async def test_unique_identity_cannot_change_existing_unit(authed_client):
    first = await create(authed_client)
    body = observation("replacement", twin_id=first)
    body["identities"][0]["value"] = "FAKE-002"
    assert (await authed_client.post("/v1/twins/observations", json=body)).status_code == 409
    detail = (await authed_client.get(f"/v1/twins/{first}")).json()
    assert len(detail["observations"]) == 1
    assert detail["identities"][0]["value"] == "FAKE-001"


async def test_conflicts_rollback_ports_and_new_twin(authed_client):
    first = await create(authed_client)
    body = observation("claim", identities=[])
    second = await create(authed_client, body)
    body = observation("conflict", twin_id=second, ports=[{"id": "new", "kind": "uart"}])
    assert (await authed_client.post("/v1/twins/observations", json=body)).status_code == 409
    detail = (await authed_client.get(f"/v1/twins/{second}")).json()
    assert [port["id"] for port in detail["ports"]] == ["usb-native"]
    assert len(detail["observations"]) == 1
    assert first != second


async def test_multiple_usb_modes_and_debug_ports_one_physical_twin(authed_client):
    twin = await create(authed_client)
    body = observation(
        "boot-scan",
        twin_id=twin,
        observed_at="2026-01-02T00:00:00Z",
        identities=[
            {
                "namespace": "test.usb:boot",
                "kind": "endpoint_serial",
                "value": "FAKE-BOOT",
                "owner": "endpoint",
                "component": "usb-boot",
                "evidence": "Synthetic USB descriptor",
            }
        ],
        ports=[
            {"id": "usb-boot", "kind": "usb", "mode": "bootloader"},
            {"id": "debug", "kind": "swd"},
            {"id": "console", "kind": "uart"},
        ],
    )
    assert await create(authed_client, body) == twin
    detail = (await authed_client.get(f"/v1/twins/{twin}")).json()
    assert len(detail["ports"]) == 4
    assert len((await authed_client.get("/v1/twins")).json()["twins"]) == 1
    bad = observation("bad-endpoint", twin_id=twin, identities=body["identities"], ports=[])
    bad["identities"][0]["component"] = "missing-port"
    assert (await authed_client.post("/v1/twins/observations", json=bad)).status_code == 422


async def test_replacement_preserves_old_twin_and_slot_history(seeded_client, tmp_topology):
    slot = {"kind": "device", "id": "fake-qtpy-01", "expected_twin_id": None}
    first = await create(seeded_client, observation(slot=slot))
    replacement = observation("replacement", observed_at="2026-01-02T00:00:00Z", slot=slot)
    replacement["identities"][0]["value"] = "FAKE-002"
    assert (await seeded_client.post("/v1/twins/observations", json=replacement)).status_code == 409
    replacement["slot"]["expected_twin_id"] = first
    second = await create(seeded_client, replacement)
    assert first != second
    old = (await seeded_client.get(f"/v1/twins/{first}")).json()
    assert old["identities"][0]["value"] == "FAKE-001"
    assert old["slot_history"][0]["ended_at"] is not None
    slots = (await seeded_client.get("/v1/twins/slots?kind=device")).json()
    assert next(s for s in slots["slots"] if s["id"] == "fake-qtpy-01")["twin_id"] == second
    assert len(slots["history"]) == 2
    # Reseeding the existing topology must not overwrite physical replacement history.
    from hil_controller.topology.seeder import seed_topology

    app = seeded_client._transport.app
    await seed_topology(app.state.db_path, str(tmp_topology))
    assert len((await seeded_client.get(f"/v1/twins/{first}")).json()["slot_history"]) == 1


async def test_unknown_slot_and_stale_replacement_rejected(seeded_client):
    body = observation(slot={"kind": "device", "id": "not-real", "expected_twin_id": None})
    assert (await seeded_client.post("/v1/twins/observations", json=body)).status_code == 422
    assert (await seeded_client.get("/v1/twins")).json()["twins"] == []
    body["slot"]["id"] = "fake-qtpy-01"
    first = await create(seeded_client, body)
    body["observation_id"] = "earlier"
    body["identities"][0]["value"] = "FAKE-002"
    body["slot"]["expected_twin_id"] = first
    assert (await seeded_client.post("/v1/twins/observations", json=body)).status_code == 409


async def test_out_of_order_observation_preserves_current_port_and_attributes(authed_client):
    twin = await create(authed_client, observation(observed_at="2026-01-03T00:00:00Z"))
    stale = observation(
        "old",
        twin_id=twin,
        label="Old label",
        attributes={"psram_bytes": 100},
        ports=[{"id": "usb-native", "kind": "usb", "mode": "old"}],
    )
    await create(authed_client, stale)
    detail = (await authed_client.get(f"/v1/twins/{twin}")).json()
    assert detail["ports"][0]["mode"] == "runtime"
    assert detail["attributes"]["psram_bytes"] == 0
    assert detail["label"] == "Synthetic board"
    assert detail["identities"][0]["first_seen_at"].startswith("2026-01-01")


async def test_connections_ownership_history_and_mux_metadata(authed_client):
    dut = await create(authed_client)
    probe = await create(
        authed_client,
        observation(
            "probe", kind="debugger", identities=[], ports=[{"id": "swd-out", "kind": "swd"}]
        ),
    )
    item = {
        "id": "wire-1",
        "from_twin_id": probe,
        "from_port": "swd-out",
        "to_twin_id": dut,
        "to_port": "usb-native",
        "kind": "debug",
        "state": "potential",
        "attributes": {"mux": {"channel": 3}, "supplies_power": False},
        "source": "operator",
        "confidence": "reported",
        "evidence": "Synthetic wiring plan",
        "started_at": "2026-01-02T00:00:00Z",
    }
    assert (await authed_client.post("/v1/twins/connections", json=item)).status_code == 200
    assert (await authed_client.post("/v1/twins/connections", json=item)).status_code == 200
    modified = dict(item, from_port="unknown")
    assert (await authed_client.post("/v1/twins/connections", json=modified)).status_code == 409
    modified["id"] = "bad-wire"
    assert (await authed_client.post("/v1/twins/connections", json=modified)).status_code == 422
    closure = {"ended_at": "2026-01-03T00:00:00Z", "source": "operator", "reason": "rewired"}
    assert (
        await authed_client.post("/v1/twins/connections/wire-1/close", json=closure)
    ).status_code == 200
    assert (
        await authed_client.post("/v1/twins/connections/wire-1/close", json=closure)
    ).status_code == 200
    detail = (await authed_client.get(f"/v1/twins/{dut}")).json()
    assert detail["connections"][0]["ended_at"] is not None
    assert detail["connections"][0]["attributes"]["mux"]["channel"] == 3


async def test_concurrent_duplicate_ingestion(authed_client):
    results = await asyncio.gather(
        *[authed_client.post("/v1/twins/observations", json=observation()) for _ in range(4)]
    )
    assert all(result.status_code == 200 for result in results)
    assert len({result.json()["twin_id"] for result in results}) == 1
    assert sum(not result.json()["replayed"] for result in results) == 1


async def test_auth_required_and_inventory_scope(client, app):
    assert (await client.get("/v1/twins")).status_code == 401
    restricted = Principal("db-token", "test", "", ["public"], [], "", ["inventory:write"])
    app.dependency_overrides[require_auth] = lambda: restricted
    assert (await client.get("/v1/twins")).status_code == 403
    restricted.allowed_pools = ["*"]
    restricted.capabilities = ["inventory:read"]
    assert (await client.get("/v1/twins")).status_code == 200
    assert (await client.post("/v1/twins/observations", json=observation())).status_code == 403
    assert (
        await client.post("/v1/twins/observations/preview", json=observation())
    ).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"observed_at": "2026-01-01T00:00:00"},
        {
            "identities": [
                {
                    "namespace": "i2c",
                    "kind": "address",
                    "value": "0x59",
                    "owner": "chip",
                    "component": "sensor",
                    "evidence": "scan",
                }
            ]
        },
        {"slot": {"kind": "device", "id": "fake-qtpy-01"}},
    ],
)
def test_invalid_contracts(change):
    with pytest.raises(ValidationError):
        Observation.model_validate(observation(**change))


async def test_additive_migration_and_atomic_import(tmp_path):
    path = tmp_path / "inventory.db"
    await init_db(str(path))
    await init_db(str(path))
    body = observation()
    result = await import_batch(str(path), [body], "test-import")
    assert len(result["observations"]) == 1
    next_body = observation("next", identities=[])
    invalid = observation("bad", identities=[], twin_id="missing")
    with pytest.raises(TwinError):
        await import_batch(str(path), [next_body, invalid], "test-import")
    async with get_db(str(path)) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM physical_twins")).fetchone())[0] == 1
    backup = tmp_path / "backup.db"
    snapshot(path, backup)
    assert backup.is_file()
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM physical_twins").fetchone()[0] == 1


async def test_import_connection_observation_references(tmp_path):
    path = tmp_path / "inventory.db"
    await init_db(str(path))
    first = observation("host", kind="sbc", identities=[])
    second = observation("device", identities=[])
    connection = {
        "id": "cable",
        "from_twin_id": "observation:host",
        "from_port": "usb-native",
        "to_twin_id": "observation:device",
        "to_port": "usb-native",
        "kind": "usb",
        "source": "fixture",
        "evidence": "Synthetic cable",
        "started_at": first["observed_at"],
    }
    document = {"observations": [first, second], "connections": [connection]}
    result = await import_batch(str(path), document, "test-import")
    assert result["connections"] == 1
    repeat = await import_batch(str(path), document, "test-import")
    assert all(o["replayed"] for o in repeat["observations"])


async def test_identity_promotion_has_verified_provenance_and_current_label(authed_client):
    reported = observation()
    reported["identities"][0].update(
        unique=False, confidence="reported", evidence="Unverified label"
    )
    twin = await create(authed_client, reported)
    verified = observation(
        "verified", twin_id=twin, label="Verified board", observed_at="2026-01-02T00:00:00Z"
    )
    verified["identities"][0]["evidence"] = "Verified register read"
    await create(authed_client, verified)
    detail = (await authed_client.get(f"/v1/twins/{twin}")).json()
    identity = detail["identities"][0]
    assert identity["unique"] is True
    assert identity["confidence"] == "verified"
    assert identity["evidence"] == "Verified register read"
    assert identity["observation_id"] == "verified"
    assert (await authed_client.get("/v1/twins")).json()["twins"][0]["label"] == detail["label"]


async def test_unique_identity_cannot_be_reassigned_to_another_owner(authed_client):
    twin = await create(authed_client)
    modified = observation("other-chip", twin_id=twin)
    modified["identities"][0]["component"] = "unrelated-chip"
    result = await authed_client.post("/v1/twins/observations", json=modified)
    assert result.status_code == 409


async def test_import_schema_rolls_back_with_failed_batch(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE devices(id TEXT PRIMARY KEY)")
    invalid = observation(twin_id="missing")
    with pytest.raises(TwinError):
        await import_batch(str(path), [invalid], "test")
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='physical_twins'").fetchone()
            is None
        )


def test_import_cli_dry_run_does_not_change_source_and_apply_backs_up(
    tmp_path, monkeypatch, capsys
):
    from hil_controller.twin_import import cli

    database = tmp_path / "source.db"
    asyncio.run(init_db(str(database)))
    source_before = database.read_bytes()
    source_json = tmp_path / "observations.json"
    source_json.write_text(json.dumps([observation()]))
    command = ["hil-twins-import", "--db", str(database), "--input", str(source_json)]
    monkeypatch.setattr(sys, "argv", command)
    cli()
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert database.read_bytes() == source_before
    backup = tmp_path / "before.db"
    monkeypatch.setattr(sys, "argv", command + ["--apply", "--backup", str(backup)])
    cli()
    assert json.loads(capsys.readouterr().out)["applied"] is True
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM physical_twins").fetchone()[0] == 0
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM physical_twins").fetchone()[0] == 1
    with pytest.raises(SystemExit):
        cli()  # A reused backup filename is never overwritten.
