"""Host detail page and job detail: the operator sees what the API exposes."""

from __future__ import annotations

import json
import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hil_controller.db.connection import get_db, insert_job

COOKIE = {"hil_token": "test-token-for-ci"}


@pytest_asyncio.fixture
async def app_db(tmp_path):
    from hil_controller.main import create_app

    db_file = str(tmp_path / "detail.db")
    os.environ["HIL_DB_PATH"] = db_file
    app = create_app(db_path=db_file, topology_file="")
    async with app.router.lifespan_context(app):
        yield app, db_file


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=COOKIE)


async def test_host_detail_lists_devices_jobs_leases_and_bridge_token_state(app_db, monkeypatch):
    app, db_file = app_db
    monkeypatch.delenv("HIL_BRIDGE_TOKEN_S31", raising=False)
    async with get_db(db_file) as db:
        await db.execute(
            "INSERT INTO hosts (id, addr, transport, capabilities_json, token_env, last_seen_at) "
            "VALUES ('usbip-bridge-s31', 'usbip-a1.local', 'esp-usbip-bridge', "
            "'[\"usbip-server\"]', 'HIL_BRIDGE_TOKEN_S31', '2026-10-17T10:00:00+00:00')"
        )
        await db.execute(
            "INSERT INTO hosts (id, addr, capabilities_json) VALUES ('tachyon','x','[]')"
        )
        await db.execute(
            "INSERT INTO devices (id, host_id, hub_host_id, hub_port_path, kind, capabilities_json,"
            " power_control, status, unavailable_kind, unavailable_reason) VALUES "
            "('mcu-q', 'tachyon', 'usbip-bridge-s31', '1-1.1', 'microcontroller', '[]',"
            " 'bridge-port', 'unavailable', 'temporary', 'not enumerated')"
        )
        await db.execute(
            "INSERT INTO device_leases (device_id, kind, acquired_at) "
            "VALUES ('mcu-q', 'exclusive_device', '2026-10-17T10:01:00+00:00')"
        )
        await insert_job(
            db,
            job_id="job-on-bridge",
            request_json={"script": "firmware-bench"},
            secrets_profile="bench-protomq",
            exclusive_host=False,
        )
        await db.execute(
            "UPDATE jobs SET assigned_host='usbip-bridge-s31', assigned_device='mcu-q' "
            "WHERE id='job-on-bridge'"
        )
        await db.commit()
    async with _client(app) as c:
        page = await c.get("/ui/hosts/usbip-bridge-s31")
        hosts = await c.get("/ui/hosts")
        missing = await c.get("/ui/hosts/nope")
    t = page.text
    assert page.status_code == 200
    assert "http://usbip-a1.local" in t and "NOT set" in t
    assert "mcu-q" in t and "USB hub host (owner tachyon)" in t and "not enumerated" in t
    assert "exclusive_device" in t and "job-on-bridge"[:12] in t
    assert '<a href="/ui/hosts/usbip-bridge-s31">' in hosts.text
    assert "esp-usbip-bridge" in hosts.text and "seen 2026-10-17T10:00:00" in hosts.text
    assert missing.status_code == 404


async def test_job_detail_shows_host_request_masked_and_all_event_kinds(app_db):
    app, db_file = app_db
    request = {
        "script": "firmware-bench",
        "target": {"pool": "public", "device": {"id": "mcu-q"}},
        "secrets": {"io_username": "tyeth", "io_key": "aio_SUPERSECRETVALUE1234"},
        "params": {"wifi_password": "hunter22pass", "token_env": "HIL_BRIDGE_TOKEN_S31"},
    }
    async with get_db(db_file) as db:
        await insert_job(
            db,
            job_id="job-x",
            request_json=request,
            secrets_profile="bench-protomq",
            exclusive_host=False,
        )
        await db.execute("UPDATE jobs SET assigned_host='tachyon' WHERE id='job-x'")
        for seq, kind, payload in (
            (1, "state", {"state": "running"}),
            (2, "log", {"stream": "serial", "msg": "WipperSnapper booted"}),
            (3, "metric", {"name": "checkin_s", "value": 4.2}),
        ):
            await db.execute(
                "INSERT INTO events (job_id, seq, at, kind, payload_json) VALUES (?, ?, ?, ?, ?)",
                ("job-x", seq, f"2026-10-17T10:00:0{seq}", kind, json.dumps(payload)),
            )
        await db.commit()
    async with _client(app) as c:
        page = await c.get("/ui/jobs/job-x")
    t = page.text
    assert '<a href="/ui/hosts/tachyon">tachyon</a>' in t
    assert "SUPERSECRETVALUE" not in t and "hunter22" not in t
    assert "****1234" in t and "****pass" in t
    assert "HIL_BRIDGE_TOKEN_S31" in t  # env var NAMES are not secrets
    assert 'data-stream="serial"' in t and "checkin_s" in t
    assert 'class="log-stream"' in t


async def test_leases_create_conflict_history_and_release(app_db):
    app, db_file = app_db
    async with get_db(db_file) as db:
        await db.execute("INSERT INTO hosts (id, addr, capabilities_json) VALUES ('h','x','[]')")
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, capabilities_json) "
            "VALUES ('mcu-l', 'h', 'microcontroller', '[]')"
        )
        await db.commit()
    async with _client(app) as c:
        page = await c.get("/ui/leases")
        assert 'hx-post="/ui/leases"' in page.text and "mcu-l" in page.text
        ok = await c.post(
            "/ui/leases",
            data={"kind": "exclusive_device", "device_id": "mcu-l", "expires_minutes": "15"},
        )
        clash = await c.post("/ui/leases", data={"kind": "exclusive_device", "device_id": "mcu-l"})
        active = await c.get("/ui/leases/body")
        lease_id = ok.text.split("Lease ")[1].split(" ")[0]
        await c.delete(f"/ui/leases/{lease_id}")
        after = await c.get("/ui/leases/body")
        history = await c.get("/ui/leases/body?active_only=false")
    assert "acquired" in ok.text and "until" in ok.text
    assert "Conflict" in clash.text
    assert "mcu-l" in active.text and "Force release" in active.text
    assert "mcu-l" not in after.text
    assert "mcu-l" in history.text and "Force release" not in history.text


async def test_usb_lookup_and_hardware_detail(app_db):
    app, db_file = app_db
    async with get_db(db_file) as db:
        await db.execute("INSERT INTO hosts (id, addr, capabilities_json) VALUES ('h','x','[]')")
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, capabilities_json, usb_serial) "
            "VALUES ('mcu-u', 'h', 'microcontroller', '[]', 'E663AC91')"
        )
        await db.execute(
            "INSERT INTO device_usb_ids (device_id, vid, pid, iserial, role, first_seen_at, "
            "last_seen_at, source) VALUES ('mcu-u', '239a', '8143', 'E663AC91', 'app', "
            "'2026-10-01', '2026-10-02', 'learned')"
        )
        await db.execute(
            "INSERT INTO auxes (id, kind, model, capabilities_json, interface, observability) "
            "VALUES ('mux-bridge-s31', 'i2c-mux', 'ADG729', '[]', 'bridge:usbip-bridge-s31', "
            "'none')"
        )
        await db.commit()
    async with _client(app) as c:
        await c.post(
            "/v1/strands",
            headers={"Authorization": "Bearer test-token-for-ci"},
            json={
                "id": "strand-a",
                "components": [
                    {"id": "c1", "model": "SCD30", "address": 0x61, "capabilities": ["co2"]},
                    {"id": "c2", "model": "BME280", "address": 0x77, "tca_channel": 3},
                ],
                "routes": [{"device": "mcu-u", "channel": 1}],
            },
        )
        by_vidpid = await c.get("/ui/devices/lookup-usb?vid=0x239A&pid=8143")
        by_serial = await c.get("/ui/devices/lookup-usb?iserial=E663AC91")
        nothing = await c.get("/ui/devices/lookup-usb?vid=dead&pid=beef")
        hardware = await c.get("/ui/hardware")
        devices = await c.get("/ui/devices")
    assert "learned USB ID" in by_vidpid.text and "mcu-u" in by_vidpid.text
    assert "device iSerial" in by_serial.text
    assert "No device" in nothing.text
    assert "Who owns this USB device?" in devices.text
    h = hardware.text
    assert "SCD30" in h and "@0x61" in h and "direct" in h and "TCA ch 3" in h and "co2" in h
    assert '<a href="/ui/hosts/usbip-bridge-s31">' in h
