"""Tests for REST endpoints managing device_usb_ids (CRUD + lookup) and
new device fields (hub_host_id, hub_port_path, solenoid_channel, usb_serial).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

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

devices:
  - id: dev1
    host_id: hub-a
    kind: microcontroller
    model: feather
    pool: public
    status: available
    hub_port_path: "1-1.1.5"
    usb_ids:
      - { vid: "239a", pid: "8053", role: runtime }
      - { vid: "239a", pid: "0035", role: bootloader }

  - id: dev2
    host_id: hub-a
    kind: microcontroller
    model: qtpy
    pool: public
    status: available
"""


@pytest_asyncio.fixture
async def app(tmp_path: Path):
    db_file = str(tmp_path / "usb_api.db")
    topo = tmp_path / "t.yaml"
    topo.write_text(_TOPOLOGY)
    os.environ["HIL_DB_PATH"] = db_file

    from hil_controller.main import create_app

    a = create_app(db_path=db_file, topology_file=str(topo))
    async with a.router.lifespan_context(a):
        yield a


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token-for-ci"},
    ) as ac:
        yield ac


# -- GET /v1/devices/{id}/usb-ids ----------------------------------------


@pytest.mark.asyncio
async def test_list_usb_ids(client):
    r = await client.get("/v1/devices/dev1/usb-ids")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 2
    pids = {x["pid"] for x in body}
    assert pids == {"8053", "0035"}


@pytest.mark.asyncio
async def test_list_usb_ids_empty(client):
    r = await client.get("/v1/devices/dev2/usb-ids")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_usb_ids_unknown_device(client):
    r = await client.get("/v1/devices/missing/usb-ids")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_usb_ids_requires_auth(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/v1/devices/dev1/usb-ids")
        assert r.status_code == 401


# -- POST /v1/devices/{id}/usb-ids ---------------------------------------


@pytest.mark.asyncio
async def test_add_usb_id(client):
    r = await client.post(
        "/v1/devices/dev2/usb-ids",
        json={"vid": "239A", "pid": "8014", "role": "runtime", "description": "CircuitPython"},
    )
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["vid"] == "239a"  # normalised to lower
    assert row["pid"] == "8014"
    assert row["role"] == "runtime"
    assert row["source"] == "manual"

    # Now appears in list.
    r2 = await client.get("/v1/devices/dev2/usb-ids")
    assert r2.status_code == 200
    assert len(r2.json()) == 1


@pytest.mark.asyncio
async def test_add_usb_id_rejects_invalid(client):
    r = await client.post(
        "/v1/devices/dev2/usb-ids",
        json={"vid": "", "pid": "8014"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_add_usb_id_duplicate_is_409(client):
    payload = {"vid": "239a", "pid": "8053"}
    r1 = await client.post("/v1/devices/dev1/usb-ids", json=payload)
    # dev1 already has this seeded with no iserial → conflict
    assert r1.status_code == 409


@pytest.mark.asyncio
async def test_add_usb_id_unknown_device(client):
    r = await client.post("/v1/devices/nope/usb-ids", json={"vid": "239a", "pid": "8053"})
    assert r.status_code == 404


# -- DELETE /v1/devices/{id}/usb-ids/{row_id} ----------------------------


@pytest.mark.asyncio
async def test_delete_usb_id(client):
    rows = (await client.get("/v1/devices/dev1/usb-ids")).json()
    row_id = rows[0]["id"]
    r = await client.delete(f"/v1/devices/dev1/usb-ids/{row_id}")
    assert r.status_code == 204
    rows_after = (await client.get("/v1/devices/dev1/usb-ids")).json()
    assert len(rows_after) == len(rows) - 1


@pytest.mark.asyncio
async def test_delete_usb_id_missing(client):
    r = await client.delete("/v1/devices/dev1/usb-ids/9999999")
    assert r.status_code == 404


# -- POST /v1/devices/lookup-by-usb --------------------------------------


@pytest.mark.asyncio
async def test_lookup_by_usb_match(client):
    r = await client.post(
        "/v1/devices/lookup-by-usb",
        json={"vid": "239a", "pid": "8053"},
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    ids = {x["device_id"] for x in body}
    assert "dev1" in ids


@pytest.mark.asyncio
async def test_lookup_by_usb_no_match(client):
    r = await client.post(
        "/v1/devices/lookup-by-usb",
        json={"vid": "dead", "pid": "beef"},
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_lookup_by_usb_normalises_case(client):
    r = await client.post(
        "/v1/devices/lookup-by-usb",
        json={"vid": "239A", "pid": "8053"},
    )
    assert r.status_code == 200
    assert len(r.json()) >= 1


# -- Web UI: list + add + delete via HTMX --------------------------------


@pytest_asyncio.fixture
async def cookie_client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"hil_token": "test-token-for-ci"},
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_ui_usb_ids_partial_lists_rows(cookie_client):
    r = await cookie_client.get("/ui/devices/dev1/usb-ids")
    assert r.status_code == 200
    body = r.text
    assert "8053" in body
    assert "0035" in body
    assert "bootloader" in body


@pytest.mark.asyncio
async def test_ui_add_usb_id_via_form(cookie_client):
    r = await cookie_client.post(
        "/ui/devices/dev2/usb-ids",
        data={"vid": "239a", "pid": "80df", "role": "runtime", "description": ""},
    )
    assert r.status_code == 200
    # Partial returns the new list of rows.
    assert "80df" in r.text


@pytest.mark.asyncio
async def test_ui_delete_usb_id(cookie_client):
    # Get a row id via REST.
    rows = (
        await cookie_client.get(
            "/v1/devices/dev1/usb-ids",
            headers={"Authorization": "Bearer test-token-for-ci"},
        )
    ).json()
    row_id = rows[0]["id"]
    r = await cookie_client.delete(f"/ui/devices/dev1/usb-ids/{row_id}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_ui_devices_form_includes_hub_fields(cookie_client):
    r = await cookie_client.get("/ui/devices/dev1/form")
    assert r.status_code == 200
    body = r.text
    for fld in ("hub_host_id", "hub_port_path", "solenoid_channel", "usb_serial"):
        assert fld in body, f"missing field {fld} in form"


@pytest.mark.asyncio
async def test_ui_devices_form_save_persists_hub_fields(cookie_client):
    r = await cookie_client.post(
        "/ui/devices/dev2",
        data={
            "host_id": "hub-a",
            "kind": "microcontroller",
            "model": "qtpy",
            "pool": "public",
            "capabilities": "",
            "serial_port": "",
            "flasher": "",
            "usb_vid": "",
            "usb_pid": "",
            "status": "available",
            "camera_id": "",
            "qr_identifier": "",
            "manual_focus_dioptres": "",
            "illuminator_brightness": "",
            "hub_host_id": "hub-a",
            "hub_port_path": "1-1.2.4",
            "solenoid_channel": "4",
            "usb_serial": "Z9Z9",
        },
    )
    assert r.status_code == 200  # HX-Redirect uses 200
    r2 = await cookie_client.get(
        "/v1/topology", headers={"Authorization": "Bearer test-token-for-ci"}
    )
    body = r2.json()
    dev2 = next(d for d in body["devices"] if d["id"] == "dev2")
    assert dev2["hub_port_path"] == "1-1.2.4"
    assert dev2["solenoid_channel"] == 4
    assert dev2["usb_serial"] == "Z9Z9"
