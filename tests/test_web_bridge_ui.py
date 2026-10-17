"""Web-UI parity for esp-usbip-bridge hosts: forms, live registry sync, port power."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hil_controller.db.connection import get_db
from hil_controller.hosts.esp_bridge import EspBridgeTransport
from hil_controller.hosts.registry import HostRegistry, RealHostRegistry
from tests.fake_esp_bridge import FakeBridge

TOKEN = "test-token-for-ci"
COOKIE = {"hil_token": TOKEN}
BRIDGE = "usbip-bridge-s31"


@pytest_asyncio.fixture
async def app_db(tmp_path):
    from hil_controller.main import create_app

    db_file = str(tmp_path / "ui.db")
    os.environ["HIL_DB_PATH"] = db_file
    app = create_app(db_path=db_file)
    async with app.router.lifespan_context(app):
        yield app, db_file


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=COOKIE)


async def _host_row(db_file: str, host_id: str):
    async with get_db(db_file) as db:
        async with db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)) as cur:
            return await cur.fetchone()


async def test_host_form_offers_bridge_transport_and_fields(app_db):
    app, _ = app_db
    async with _client(app) as c:
        resp = await c.get("/ui/hosts/form")
    assert resp.status_code == 200
    assert '<option value="esp-usbip-bridge"' in resp.text
    assert 'name="api_url"' in resp.text
    assert 'name="token_env"' in resp.text


async def test_create_bridge_host_is_live_in_registry_without_restart(app_db):
    app, db_file = app_db
    app.state.host_registry = RealHostRegistry(topology_file="", db_path=db_file)
    async with _client(app) as c:
        resp = await c.post(
            "/ui/hosts",
            data={
                "id": BRIDGE,
                "role": "microcontroller-fleet",
                "addr": "usbip-a1b2c3.local",
                "transport": "esp-usbip-bridge",
                "api_url": "http://192.168.1.60",
                "token_env": "HIL_BRIDGE_TOKEN_S31",
            },
        )
    assert resp.status_code == 200 and resp.headers.get("HX-Redirect") == "/ui/hosts"
    row = await _host_row(db_file, BRIDGE)
    assert row["api_url"] == "http://192.168.1.60"
    assert row["token_env"] == "HIL_BRIDGE_TOKEN_S31"
    transport = app.state.host_registry.transport_for(BRIDGE)
    assert isinstance(transport, EspBridgeTransport)
    assert transport.bridge.base_url == "http://192.168.1.60"


async def test_token_value_pasted_into_token_env_is_rejected_and_not_stored(app_db):
    app, db_file = app_db
    async with _client(app) as c:
        resp = await c.post(
            "/ui/hosts",
            data={
                "id": BRIDGE,
                "addr": "usbip-a1b2c3.local",
                "transport": "esp-usbip-bridge",
                "token_env": "s3cr3t-token value!",
            },
        )
    assert "environment variable NAME" in resp.text
    assert "s3cr3t-token value!" not in resp.text
    assert await _host_row(db_file, BRIDGE) is None


async def test_bridge_host_needs_an_address(app_db):
    app, db_file = app_db
    async with _client(app) as c:
        resp = await c.post("/ui/hosts", data={"id": BRIDGE, "transport": "esp-usbip-bridge"})
    assert "needs an Address" in resp.text
    assert await _host_row(db_file, BRIDGE) is None


async def test_edit_shows_whether_token_env_is_set_never_the_value(app_db, monkeypatch):
    app, db_file = app_db
    async with get_db(db_file) as db:
        await db.execute(
            "INSERT INTO hosts (id, addr, transport, capabilities_json, token_env) "
            "VALUES (?, ?, ?, ?, ?)",
            (BRIDGE, "usbip-test.local", "esp-usbip-bridge", "[]", "HIL_BRIDGE_TOKEN_S31"),
        )
        await db.commit()
    monkeypatch.delenv("HIL_BRIDGE_TOKEN_S31", raising=False)
    async with _client(app) as c:
        unset = await c.get(f"/ui/hosts/{BRIDGE}/form")
        monkeypatch.setenv("HIL_BRIDGE_TOKEN_S31", "very-secret-value")
        is_set = await c.get(f"/ui/hosts/{BRIDGE}/form")
    assert "NOT set on controller" in unset.text
    assert "set on controller" in is_set.text and "NOT set" not in is_set.text
    assert "very-secret-value" not in is_set.text


async def test_edit_and_delete_follow_into_registry(app_db):
    app, db_file = app_db
    registry = RealHostRegistry(topology_file="", db_path=db_file)
    app.state.host_registry = registry
    async with _client(app) as c:
        await c.post(
            "/ui/hosts",
            data={"id": "rpi-x", "addr": "10.0.0.5", "transport": "ssh", "ssh_user": "pi"},
        )
        await c.post(
            "/ui/hosts/rpi-x",
            data={"id": "rpi-y", "addr": "10.0.0.6", "transport": "ssh", "ssh_user": "bench"},
        )
        assert not registry.has_host("rpi-x")
        transport = registry.transport_for("rpi-y")
        assert transport.host == "10.0.0.6" and transport.user == "bench"
        await c.delete("/ui/hosts/rpi-y")
    assert not registry.has_host("rpi-y")


def test_sync_host_blank_column_falls_back_to_defaults():
    registry = HostRegistry(topology_file="")
    registry._hosts = [{"id": "h", "addr": "a", "ssh_user": "pi", "known_hosts": "/k"}]
    registry.sync_host(
        {"id": "h", "addr": "b", "transport": "ssh", "ssh_user": None, "capabilities_json": "[]"}
    )
    (host,) = registry._hosts
    assert host["addr"] == "b"
    assert "ssh_user" not in host  # SSHTransport then defaults to "pi"
    assert host["known_hosts"] == "/k"  # topology-only keys survive


async def _seed_bridge_device(db_file: str, power_control: str | None) -> None:
    async with get_db(db_file) as db:
        await db.execute(
            "INSERT INTO hosts (id, addr, transport, capabilities_json) VALUES (?, ?, ?, ?)",
            (BRIDGE, "usbip-test.local", "esp-usbip-bridge", "[]"),
        )
        await db.execute(
            "INSERT INTO devices (id, host_id, hub_host_id, hub_port_path, kind, "
            "capabilities_json, power_control) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mcu-qtpy", BRIDGE, BRIDGE, "1-1.1", "microcontroller", "[]", power_control),
        )
        await db.commit()


def _bridge_registry(fake: FakeBridge) -> MagicMock:
    transport = EspBridgeTransport(host_id=BRIDGE, addr="usbip-test.local", bridge=fake.client())
    registry = MagicMock()
    registry.transport_for = MagicMock(return_value=transport)
    return registry


async def test_usbip_fragment_shows_bridge_panel_and_port_power_buttons(app_db):
    app, db_file = app_db
    await _seed_bridge_device(db_file, "bridge-port")
    app.state.host_registry = _bridge_registry(FakeBridge())
    async with _client(app) as c:
        resp = await c.get(f"/ui/usbip/host/{BRIDGE}")
    assert resp.status_code == 200
    assert "esp-usbip-bridge — usbip-bridge-s31" in resp.text
    assert "http://usbip-test.local" in resp.text
    assert f"/ui/hosts/{BRIDGE}/bridge/ports/1-1.1/off" in resp.text
    # the empty port 1-1.2 is still switchable from the hub panel
    assert f"/ui/hosts/{BRIDGE}/bridge/ports/1-1.2/on" in resp.text
    assert "mcu-qtpy" in resp.text


async def test_bridge_port_power_buttons_switch_the_port(app_db):
    app, db_file = app_db
    await _seed_bridge_device(db_file, "bridge-port")
    fake = FakeBridge()
    app.state.host_registry = _bridge_registry(fake)
    async with _client(app) as c:
        off = await c.post(f"/ui/hosts/{BRIDGE}/bridge/ports/1-1.1/off", data={"force": ""})
        bogus = await c.post(f"/ui/hosts/{BRIDGE}/bridge/ports/1-1.1/explode")
    assert "port 1-1.1 off sent" in off.text
    assert fake.power["1-1.1"] is False
    assert "unknown action" in bogus.text


async def test_ganged_hub_refusal_explains_force(app_db):
    app, db_file = app_db
    await _seed_bridge_device(db_file, None)
    fake = FakeBridge(switching="ganged")
    app.state.host_registry = _bridge_registry(fake)
    async with _client(app) as c:
        refused = await c.post(f"/ui/hosts/{BRIDGE}/bridge/ports/1-1.1/off")
        forced = await c.post(f"/ui/hosts/{BRIDGE}/bridge/ports/1-1.1/off", data={"force": "1"})
    assert "bridge-port:force" in refused.text
    assert fake.power["1-1.1"] is False and "(forced)" in forced.text


async def test_device_form_saves_power_control_and_absent_field_keeps_it(app_db):
    app, db_file = app_db
    await _seed_bridge_device(db_file, None)
    base = {"host_id": BRIDGE, "kind": "microcontroller", "hub_port_path": "1-1.1"}
    async with _client(app) as c:
        form = await c.get("/ui/devices/mcu-qtpy/form")
        assert 'name="power_control"' in form.text
        await c.post(
            "/ui/devices/mcu-qtpy",
            data={**base, "power_control": "bridge-port:force", "power_control_present": "1"},
        )
        async with get_db(db_file) as db:
            async with db.execute("SELECT power_control FROM devices WHERE id='mcu-qtpy'") as cur:
                assert (await cur.fetchone())["power_control"] == "bridge-port:force"
        await c.post("/ui/devices/mcu-qtpy", data=base)  # field absent: unchanged
        async with get_db(db_file) as db:
            async with db.execute("SELECT power_control FROM devices WHERE id='mcu-qtpy'") as cur:
                assert (await cur.fetchone())["power_control"] == "bridge-port:force"
        await c.post(
            "/ui/devices/mcu-qtpy", data={**base, "power_control": "", "power_control_present": "1"}
        )
        async with get_db(db_file) as db:
            async with db.execute("SELECT power_control FROM devices WHERE id='mcu-qtpy'") as cur:
                assert (await cur.fetchone())["power_control"] is None


async def test_ui_created_device_and_api_strand_are_schedulable_without_restart(app_db):
    app, db_file = app_db
    registry = RealHostRegistry(topology_file="", db_path=db_file)
    app.state.host_registry = registry
    async with _client(app) as c:
        await c.post(
            "/ui/hosts",
            data={"id": BRIDGE, "addr": "usbip-test.local", "transport": "esp-usbip-bridge"},
        )
        await c.post(
            "/ui/devices",
            data={
                "id": "mcu-qtpy-bridge",
                "host_id": BRIDGE,
                "kind": "microcontroller",
                "pool": "public",
                "capabilities": "arduino, wifi",
                "hub_port_path": "1-1.1",
            },
        )
        job = {"target": {"pool": "public", "device": {"capabilities": ["wifi"]}}}
        host, device = registry.find_device_for_job(job)
        assert (host["id"], device["id"]) == (BRIDGE, "mcu-qtpy-bridge")

        resp = await c.post(
            "/v1/strands",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "id": "strand-a",
                "components": [{"id": "c1", "model": "SCD30", "capabilities": ["co2"]}],
                "routes": [{"device": "mcu-qtpy-bridge", "channel": 2}],
            },
        )
        assert resp.status_code == 201
        assert registry.strand_for_device("mcu-qtpy-bridge", {"scd30"})["channel"] == 2

        await c.post(
            "/ui/devices/mcu-qtpy-bridge",
            data={"id": "mcu-qtpy-s31", "host_id": BRIDGE, "kind": "microcontroller"},
        )
        assert registry.strand_for_device("mcu-qtpy-s31", {"co2"}) is not None
        await c.delete("/ui/devices/mcu-qtpy-s31")
    assert registry.find_device_for_job(job) is None
