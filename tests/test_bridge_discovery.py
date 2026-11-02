"""Bridge discovery (mDNS), capabilities gating + pre-1.0 fallback, on-bridge rescue.

Everything is mocked: mDNS answers are canned avahi-browse output / fake
ServiceInfo objects, bridges are httpx.MockTransport handlers. No hardware, no
network. Contract: esp-usbip-bridge docs/bridge-discovery-and-capabilities.md.
"""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hil_controller import bridge_discovery as bd
from hil_controller.adapters import bridge_capabilities as bc
from hil_controller.adapters import bridge_rescue as br
from hil_controller.adapters.bench_stages import BenchContext, StageError, run_stages
from hil_controller.adapters.esp_usbip_bridge import EspBridgeError, EspUsbipBridgeClient
from hil_controller.db.connection import get_db, init_db
from hil_controller.hosts.esp_bridge import EspBridgeTransport

# The contract's example document (values are .133's).
CAPS_133: dict[str, Any] = {
    "schema": 1,
    "api_version": "1.0",
    "fw": {
        "project": "esp32p4_usbip_bridge",
        "version": "ac4fbed",
        "build_time": "2026-11-02 17:11:31",
        "idf": "v6.1-dev-8295-gce2100de51a",
        "elf_sha256": "62edb3b63e932c29",
        "running_partition": "factory",
    },
    "board": {
        "target": "esp32s31",
        "profile": "s31-function-coreboard-1",
        "mac": "1c:29:04:d1:9b:ee",
        "hostname": "usbip-d19bee",
        "board_id": None,
    },
    "network": {
        "transports": ["ethernet", "wifi"],
        "interfaces": [
            {"name": "eth", "up": True, "ip": "192.168.1.133", "mac": "1e:29:04:d1:9b:ef"},
            {"name": "wifi", "up": False, "ip": None, "ssid": None, "rssi": None},
        ],
    },
    "ports": {"http": 80, "usbip": 3240, "serial_stream": 3241},
    "auth": {"required": False},
    "limits": {"max_devices": 32, "rescue_staging_bytes": 12582912},
    "features": {
        "usbip": {"version": 1},
        "claims": {"version": 1},
        "hub_power": {"version": 1},
        "mux": {"version": 1, "available": True},
        "device_info": {"version": 1},
        "reset": {"version": 1, "modes": ["esp_hard_reset", "esp_bootloader", "touch_1200"]},
        "console": {"version": 1},
        "flash_esp": {"version": 1},
        "flash_picoboot": {"version": 1},
        "flash_samba": {"version": 1},
        "flash_uf2": {"version": 1},
        "some_future_feature": {"version": 7, "shiny": True},
    },
    "unknown_top_level": {"ignored": True},
}

# What .147 / .133 really answer today (pre-1.0: /api/capabilities is 404).
INFO_147 = {
    "hostname": "usbip-d07a04",
    "board_id": "",
    "target": "esp32s31",
    "version": "ac4fbed 2026-11-02 17:11:31",
    "auth_required": False,
    "usbip_port": 3240,
}
INFO_133 = {**INFO_147, "hostname": "usbip-d19bee", "board_id": None}


class CapsBridge:
    """A bridge's HTTP API: capabilities (or 404), info, rescue endpoints."""

    def __init__(self, caps: dict[str, Any] | None, info: dict[str, Any]) -> None:
        self.caps = caps
        self.info = info
        self.calls: list[tuple[str, str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.imported = False
        self.job_polls_until_done = 1

    def client(self, base: str = "http://bridge.test") -> EspUsbipBridgeClient:
        return EspUsbipBridgeClient(
            base, client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: Any = None
        if request.content and request.method != "PUT":
            body = json.loads(request.content)
        self.calls.append((request.method, path, body))
        if path == "/api/capabilities":
            if self.caps is None:
                return httpx.Response(404, json={"error": "Not Found"})
            return httpx.Response(200, json=self.caps)
        if path == "/api/info":
            return httpx.Response(200, json=self.info)
        if path.startswith("/api/rescue/files/"):
            name = path.rsplit("/", 1)[1]
            if request.method == "PUT":
                self.files[name] = request.content
                return httpx.Response(200, json={"ok": True, "size": len(request.content)})
            if request.method == "DELETE":
                self.files.pop(name, None)
                return httpx.Response(200, json={"ok": True})
        if path.startswith("/api/rescue/jobs/"):
            job = self.jobs[path.rsplit("/", 1)[1]]
            job["polls"] += 1
            if job["polls"] >= self.job_polls_until_done:
                job["state"] = "done"
            return httpx.Response(200, json=job)
        if path.startswith("/api/usb/devices/") and request.method == "POST":
            if self.imported:
                return httpx.Response(409, json={"ok": False, "error": "imported over usbip"})
            busid, _, action = path[len("/api/usb/devices/") :].partition("/")
            if action == "console":
                return httpx.Response(
                    200,
                    json={"text": ">>> ", "bytes": 4, "classification": "micropython_repl"},
                )
            if action == "reset":
                return httpx.Response(200, json={"ok": True, "dropped": True, "reenumerated": {}})
            job_id = str(len(self.jobs) + 1)
            self.jobs[job_id] = {
                "id": job_id,
                "state": "running",
                "log": f"[0.001] {action} on {busid}\n",  # one growing text, as the bridge
                "result": {"chip": "ESP32-S31"},
                "polls": 0,
            }
            return httpx.Response(202, json={"job": job_id, "poll": f"/api/rescue/jobs/{job_id}"})
        if path.endswith("/rescue"):
            return httpx.Response(200, json={"busid": "1-1", "serial": {"kind": "cp210x"}})
        return httpx.Response(404, json={"error": "Not Found"})

    def posts(self) -> list[str]:
        return [p for (m, p, _) in self.calls if m == "POST"]


# --------------------------------------------------------------------------- #
# avahi-browse / zeroconf parsing                                             #
# --------------------------------------------------------------------------- #

AVAHI_OUT = (
    "+;eth0;IPv4;usbip-d19bee;_hil-bridge._tcp;local\n"
    '=;eth0;IPv4;usbip-d19bee;_hil-bridge._tcp;local;usbip-d19bee.local;192.168.1.133;80;'
    '"auth=0" "caps=/api/capabilities" "mac=1c:29:04:d1:9b:ee" "board=s31-function-coreboard-1" '
    '"target=esp32s31" "fw=ac4fbed" "api=1.0"\n'
    '=;wlan0;IPv4;usbip-d19bee;_hil-bridge._tcp;local;usbip-d19bee.local;192.168.1.201;80;'
    '"api=1.0" "mac=1C:29:04:D1:9B:EE"\n'
    '=;eth0;IPv6;usbip-d19bee;_hil-bridge._tcp;local;usbip-d19bee.local;fe80::1e29:4ff:fed1:9bef;80;'
    '"api=1.0" "mac=1c:29:04:d1:9b:ee"\n'
    '=;eth0;IPv4;usbip-d19c04;_hil-bridge._tcp;local;usbip-d19c04.local;192.168.1.140;80;'
    '"api=1.0" "mac=1c:29:04:d1:9c:04" "board=semi\\059colon"\n'
    '=;eth0;IPv4;usbip-d19bee;_usbip._tcp;local;usbip-d19bee.local;192.168.1.133;3240;"busid=1-1"\n'
)


def test_parse_avahi_browse_resolved_lines_only():
    recs = bd.parse_avahi_browse(AVAHI_OUT)
    assert len(recs) == 4  # '+' line and the _usbip._tcp answer are ignored
    first = recs[0]
    assert first.instance == "usbip-d19bee"
    assert first.hostname == "usbip-d19bee.local"
    assert first.addresses == ["192.168.1.133"]
    assert first.port == 80
    assert first.txt["api"] == "1.0"
    assert first.txt["caps"] == "/api/capabilities"
    assert first.mac == "1c:29:04:d1:9b:ee"
    assert recs[3].txt["board"] == "semi;colon"  # avahi \DDD escape decoded


def test_merge_collapses_one_bridge_seen_on_several_interfaces():
    cands = bd.merge_records(bd.parse_avahi_browse(AVAHI_OUT))
    assert [c.key for c in cands] == ["1c:29:04:d1:9b:ee", "1c:29:04:d1:9c:04"]
    bee = cands[0]
    # eth + wifi + link-local v6 → one candidate, IPv4 first, fe80 last
    assert bee.addresses == ["192.168.1.133", "192.168.1.201", "fe80::1e29:4ff:fed1:9bef"]


def test_record_from_zeroconf_service_info():
    class Info:
        name = "usbip-d19bee._hil-bridge._tcp.local."
        server = "usbip-d19bee.local."
        port = 80
        properties = {b"api": b"1.0", b"mac": b"1c:29:04:d1:9b:ee", b"auth": None}

        def parsed_addresses(self):
            return ["192.168.1.133"]

    rec = bd.record_from_service_info(Info())
    assert rec.instance == "usbip-d19bee"
    assert rec.hostname == "usbip-d19bee.local"
    assert rec.addresses == ["192.168.1.133"]
    assert rec.txt == {"api": "1.0", "mac": "1c:29:04:d1:9b:ee", "auth": ""}


async def test_browse_reports_unavailable_without_zeroconf_or_avahi(monkeypatch):
    monkeypatch.setattr(bd, "zeroconf_available", lambda: False)
    monkeypatch.setattr(bd.shutil, "which", lambda _name: None)
    result = await bd.browse(0.5)
    assert result.method == "unavailable"
    assert result.records == []
    assert "[discovery]" in result.error and "by address" in result.error


async def test_browse_falls_back_to_avahi_browse(monkeypatch):
    monkeypatch.setattr(bd, "zeroconf_available", lambda: False)
    monkeypatch.setattr(bd.shutil, "which", lambda _name: "/usr/bin/avahi-browse")

    class Proc:
        returncode = 0

        async def communicate(self):
            return AVAHI_OUT.encode(), b""

    async def fake_exec(*argv, **_kw):
        assert argv[1:] == ("-rpt", "_hil-bridge._tcp")
        return Proc()

    monkeypatch.setattr(bd.asyncio, "create_subprocess_exec", fake_exec)
    result = await bd.browse(0.5)
    assert result.method == "avahi-browse"
    assert len(result.records) == 4


# --------------------------------------------------------------------------- #
# capabilities: parsing, gating, pre-1.0 fallback                             #
# --------------------------------------------------------------------------- #


def test_parse_contract_example_and_gate_on_feature_versions():
    caps = bc.parse_capabilities(CAPS_133)
    assert caps.api_version == "1.0" and caps.compatible and not caps.pre_1_0
    assert caps.mac == "1c:29:04:d1:9b:ee"
    assert caps.hostname == "usbip-d19bee"
    assert caps.fw_version == "ac4fbed"
    assert caps.eth_ip() == "192.168.1.133"
    assert caps.has("flash_esp") and caps.has("console")
    assert not caps.has("flash_esp", 2)
    assert caps.feature_version("ota") == 0  # absent → 0
    assert caps.has("some_future_feature", 7)  # unknown names are just data
    assert caps.reset_modes() == ["esp_hard_reset", "esp_bootloader", "touch_1200"]
    assert caps.staging_bytes() == 12582912


def test_available_false_gates_a_feature_off():
    doc = deepcopy(CAPS_133)
    doc["features"]["mux"] = {"version": 1, "available": False}
    caps = bc.parse_capabilities(doc)
    assert caps.feature_version("mux") == 0
    assert "i2c-strand-mux" not in bd.suggested_capability_tags(caps)


def test_unsupported_major_gates_everything_off_but_minor_bump_is_fine():
    doc = deepcopy(CAPS_133)
    doc["api_version"] = "1.4"
    assert bc.parse_capabilities(doc).has("flash_esp")
    doc["api_version"] = "2.0"
    caps = bc.parse_capabilities(doc)
    assert not caps.compatible
    assert not caps.has("flash_esp") and not caps.has("usbip")
    assert "not a 1.x API" in br.gate(caps, "esp_flash")


async def test_pre_1_0_bridge_falls_back_to_info_and_gets_no_features():
    """Firmware ac4fbed really has the rescue API — but without /api/capabilities
    the contract says pre-1.0, so nothing beyond info + devices is offered."""
    bridge = CapsBridge(None, INFO_147)
    caps = await bc.fetch_capabilities(bridge.client())
    assert caps.pre_1_0 and caps.api_version is None
    assert caps.fw_version.startswith("ac4fbed")
    assert caps.hostname == "usbip-d07a04"
    assert caps.mac == ""  # pre-1.0 /api/info carries no MAC
    for feature in ("flash_esp", "console", "reset", "device_info", "usbip", "hub_power"):
        assert not caps.has(feature)
    assert br.available_actions(caps) == []
    assert "pre-1.0" in br.gate(caps, "console")


async def test_non_bridge_address_is_rejected():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    client = EspUsbipBridgeClient(
        "http://printer.test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(EspBridgeError, match="not a bridge"):
        await bc.fetch_capabilities(client)

    def web_server(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/info":
            return httpx.Response(200, json={"name": "some other device"})
        return httpx.Response(404)

    other = EspUsbipBridgeClient(
        "http://other.test", client=httpx.AsyncClient(transport=httpx.MockTransport(web_server))
    )
    with pytest.raises(EspBridgeError, match="does not look like"):
        await bc.fetch_capabilities(other)


def test_cached_json_round_trip_keeps_pre_1_0_and_1_0():
    caps = bc.parse_capabilities(CAPS_133, info=INFO_133)
    again = bc.capabilities_from_json(caps.to_json())
    assert again is not None and again.has("flash_esp") and again.mac == caps.mac
    pre = bc.capabilities_from_json(bc.pre_1_0_capabilities(INFO_147).to_json())
    assert pre is not None and pre.pre_1_0 and pre.hostname == "usbip-d07a04"
    assert bc.capabilities_from_json("not json") is None


# --------------------------------------------------------------------------- #
# capabilities cache on the hosts row                                         #
# --------------------------------------------------------------------------- #


async def _db_with_bridge_hosts(tmp_path) -> str:
    db = str(tmp_path / "bridges.db")
    await init_db(db)
    async with get_db(db) as conn:
        await conn.execute(
            "INSERT INTO hosts (id, role, addr, transport, status, capabilities_json) "
            "VALUES ('usbip-bridge-s31', 'microcontroller-fleet', '192.168.1.147', "
            "'esp-usbip-bridge', 'available', '[]')"
        )
        await conn.commit()
    return db


async def test_cache_keeps_last_good_document_when_a_fetch_fails(tmp_path):
    db = await _db_with_bridge_hosts(tmp_path)
    bridge = CapsBridge(CAPS_133, INFO_133)
    cached = await bc.refresh_capabilities(db, "usbip-bridge-s31", bridge.client())
    assert cached.caps is not None and cached.caps.has("flash_esp") and not cached.stale

    def down(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    dead = EspUsbipBridgeClient(
        "http://bridge.test", client=httpx.AsyncClient(transport=httpx.MockTransport(down))
    )
    cached = await bc.refresh_capabilities(db, "usbip-bridge-s31", dead)
    assert cached.stale and "no route" in (cached.error or "")
    assert cached.caps is not None and cached.caps.has("flash_esp")  # not cleared
    async with get_db(db) as conn:
        row = await (await conn.execute("SELECT status FROM hosts")).fetchone()
    assert row["status"] == "available"  # a failed fetch never disables the host


async def test_get_capabilities_uses_cache_until_it_ages_out(tmp_path):
    db = await _db_with_bridge_hosts(tmp_path)
    bridge = CapsBridge(CAPS_133, INFO_133)
    await bc.get_capabilities(db, "usbip-bridge-s31", bridge.client())
    n = len(bridge.calls)
    await bc.get_capabilities(db, "usbip-bridge-s31", bridge.client())
    assert len(bridge.calls) == n  # fresh: served from the cache
    await bc.get_capabilities(db, "usbip-bridge-s31", bridge.client(), max_age_s=-1)
    assert len(bridge.calls) > n


# --------------------------------------------------------------------------- #
# proposals: discover / probe / apply                                         #
# --------------------------------------------------------------------------- #


def _factory(bridges: dict[str, CapsBridge]):
    def make(address: str) -> EspUsbipBridgeClient:
        if address not in bridges:

            def down(_req: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("unreachable")

            return EspUsbipBridgeClient(
                f"http://{address}", client=httpx.AsyncClient(transport=httpx.MockTransport(down))
            )
        return bridges[address].client(f"http://{address}")

    return make


async def _hosts_snapshot(db: str) -> list[dict[str, Any]]:
    async with get_db(db) as conn:
        cur = await conn.execute("SELECT id, addr, status FROM hosts ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def test_discover_proposes_new_bridges_and_never_writes(tmp_path):
    db = await _db_with_bridge_hosts(tmp_path)
    before = await _hosts_snapshot(db)
    browse_result = bd.BrowseResult(bd.parse_avahi_browse(AVAHI_OUT), "avahi-browse")
    bridges = {"192.168.1.133": CapsBridge(CAPS_133, INFO_133)}  # d19c04 not reachable
    out = await bd.discover(
        db, browse_result=browse_result, client_factory=_factory(bridges)
    )
    by_key = {p["key"]: p for p in out["proposals"]}
    bee = by_key["1c:29:04:d1:9b:ee"]
    assert bee["action"] == "new"
    assert bee["address"] == "192.168.1.133"  # Ethernet IP preferred
    assert bee["host"]["id"] == "usbip-d19bee"
    assert bee["host"]["transport"] == "esp-usbip-bridge"
    assert bee["host"]["capabilities"] == ["usbip-server", "bridge-port-power", "i2c-strand-mux"]
    c04 = by_key["1c:29:04:d1:9c:04"]
    assert c04["action"] == "unconfirmed" and "unreachable" in c04["error"]
    assert c04["host"] is None
    # the pre-1.0 .147 host does not advertise: listed as not seen, never touched
    assert out["not_seen"] == ["usbip-bridge-s31"]
    assert await _hosts_snapshot(db) == before


async def test_known_bridge_matches_by_mac_hostname_or_address(tmp_path):
    db = await _db_with_bridge_hosts(tmp_path)
    # .147 is pre-1.0 (no MAC) and its host id is not its hostname: address match.
    p = await bd.probe_address(
        db, "192.168.1.147", client_factory=_factory({"192.168.1.147": CapsBridge(None, INFO_147)})
    )
    assert p.action == "known" and p.matched_host_id == "usbip-bridge-s31"
    assert p.match_reason == "address 192.168.1.147"
    # once it has a 1.0 capabilities cache, a new address with the same MAC is "moved"
    doc = deepcopy(CAPS_133)
    doc["board"].update(mac="1c:29:04:d0:7a:04", hostname="usbip-d07a04")
    doc["network"]["interfaces"][0]["ip"] = "192.168.1.147"
    await bc.store_capabilities(db, "usbip-bridge-s31", bc.parse_capabilities(doc))
    moved = deepcopy(doc)
    moved["network"]["interfaces"] = [
        {"name": "wifi", "up": True, "ip": "192.168.1.150"},
    ]
    p = await bd.probe_address(
        db, "192.168.1.150", client_factory=_factory({"192.168.1.150": CapsBridge(moved, {})})
    )
    assert p.action == "moved" and p.match_reason == "base MAC 1c:29:04:d0:7a:04"
    # hostname match (e.g. typed as name.local)
    p = await bd.probe_address(
        db,
        "usbip-d07a04.local",
        client_factory=_factory({"usbip-d07a04.local": CapsBridge(None, INFO_147)}),
    )
    assert p.action in ("known", "moved") and p.matched_host_id == "usbip-bridge-s31"


async def test_apply_rechecks_creates_one_host_and_refuses_duplicates(tmp_path):
    db = await _db_with_bridge_hosts(tmp_path)
    factory = _factory({"192.168.1.133": CapsBridge(CAPS_133, INFO_133)})
    host = await bd.apply_proposal(db, "192.168.1.133", client_factory=factory)
    assert host["id"] == "usbip-d19bee" and host["addr"] == "192.168.1.133"
    cached = await bc.load_cached(db, "usbip-d19bee")
    assert cached.caps is not None and cached.caps.mac == "1c:29:04:d1:9b:ee"
    with pytest.raises(bd.ApplyError) as exc:
        await bd.apply_proposal(db, "192.168.1.133", client_factory=factory)
    assert exc.value.status == 409 and "usbip-d19bee" in exc.value.detail
    with pytest.raises(bd.ApplyError) as exc:
        await bd.apply_proposal(db, "192.168.1.99", client_factory=factory)
    assert exc.value.status == 422  # did not confirm
    assert [h["id"] for h in await _hosts_snapshot(db)] == ["usbip-bridge-s31", "usbip-d19bee"]


async def test_apply_rejects_a_token_value_in_token_env(tmp_path):
    db = await _db_with_bridge_hosts(tmp_path)
    factory = _factory({"192.168.1.133": CapsBridge(CAPS_133, INFO_133)})
    with pytest.raises(bd.ApplyError, match="NAME"):
        await bd.apply_proposal(
            db, "192.168.1.133", token_env="abc def-123!", client_factory=factory
        )


# --------------------------------------------------------------------------- #
# HTTP API                                                                    #
# --------------------------------------------------------------------------- #

AUTH = {"Authorization": "Bearer test-token-for-ci"}


@pytest_asyncio.fixture
async def api(tmp_path, monkeypatch):
    from hil_controller.main import create_app

    db_file = str(tmp_path / "api.db")
    os.environ["HIL_DB_PATH"] = db_file
    app = create_app(db_path=db_file)
    bridges = {
        "192.168.1.133": CapsBridge(CAPS_133, INFO_133),
        "192.168.1.147": CapsBridge(None, INFO_147),
    }
    monkeypatch.setattr(bd, "_client", lambda address, token=None: _factory(bridges)(address))
    async with app.router.lifespan_context(app):
        async with get_db(db_file) as conn:
            await conn.execute(
                "INSERT INTO hosts (id, role, addr, transport, status, capabilities_json) "
                "VALUES ('usbip-bridge-s31', 'microcontroller-fleet', '192.168.1.147', "
                "'esp-usbip-bridge', 'available', '[]')"
            )
            await conn.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield app, c


async def test_api_probe_apply_and_capabilities(api):
    app, c = api
    r = await c.post("/v1/bridges/probe", json={"address": "192.168.1.133"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["action"] == "new"
    r = await c.post(
        "/v1/bridges/apply",
        json={"address": "192.168.1.133", "token_env": "HIL_BRIDGE_TOKEN_D19BEE"},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    assert r.json()["created"]["token_env"] == "HIL_BRIDGE_TOKEN_D19BEE"
    r = await c.post("/v1/bridges/apply", json={"address": "192.168.1.133"}, headers=AUTH)
    assert r.status_code == 409
    r = await c.get("/v1/bridges", headers=AUTH)
    views = {b["host_id"]: b["capabilities"] for b in r.json()["bridges"]}
    assert views["usbip-d19bee"]["api_version"] == "1.0"
    assert views["usbip-d19bee"]["features"]["flash_esp"]["usable"] is True
    assert views["usbip-bridge-s31"]["unknown"] is True  # never fetched yet
    r = await c.post("/v1/bridges/probe", json={"address": "192.168.1.147"}, headers=AUTH)
    assert r.json()["action"] == "known"
    assert r.json()["capabilities"]["pre_1_0"] is True


async def test_api_requires_auth(api):
    _, c = api
    r = await c.get("/v1/bridges/discover")
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# bridge_rescue stage                                                         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def fast_poll(monkeypatch):
    real_sleep = asyncio.sleep

    async def instant(_s):
        await real_sleep(0)

    monkeypatch.setattr(br.asyncio, "sleep", instant)


def _ctx(bridge: CapsBridge, tmp_path, **kw: Any) -> BenchContext:
    tp = EspBridgeTransport(host_id="usbip-d19bee", addr="192.168.1.133", bridge=bridge.client())
    return BenchContext(
        dut_transport=object(),
        hub_transport=tp,
        flash_serial_port="",
        device={"id": "mcu-s31-d19c04-on-d19bee", "hub_port_path": "1-1"},
        probe_log_path=str(tmp_path / "serial-probe.log"),
        **kw,
    )


async def test_bridge_rescue_console_peek_goes_through_the_bridge(tmp_path):
    bridge = CapsBridge(CAPS_133, INFO_133)
    lines: list[str] = []
    ctx = _ctx(bridge, tmp_path, emit=lines.append)
    await run_stages(
        [{"type": "bridge_rescue", "action": "console", "pokes": ["enter"], "capture_s": 2}],
        ctx,
    )
    posts = [(p, b) for (m, p, b) in bridge.calls if m == "POST"]
    assert posts == [
        (
            "/api/usb/devices/1-1/console",
            {"serial_baud": 115200, "pokes": ["enter"], "settle_s": 1.0, "capture_s": 2.0},
        )
    ]
    assert any("SERIAL_PROBE_VERDICT classification=micropython_repl" in ln for ln in lines)
    assert ">>>" in (tmp_path / "serial-probe.log").read_text()


async def test_bridge_rescue_esp_flash_stages_file_runs_job_and_cleans_up(tmp_path, fast_poll):
    image = tmp_path / "fw.bin"
    image.write_bytes(b"\xe9" + b"\x00" * 1023)
    bridge = CapsBridge(CAPS_133, INFO_133)
    bridge.job_polls_until_done = 2
    ctx = _ctx(bridge, tmp_path, local_firmware_path=str(image))
    await run_stages([{"type": "bridge_rescue", "action": "esp_flash", "address": "0x0"}], ctx)
    puts = [p for (m, p, _) in bridge.calls if m == "PUT"]
    assert len(puts) == 1 and puts[0].startswith("/api/rescue/files/hil-")
    flash = next(b for (m, p, b) in bridge.calls if p == "/api/usb/devices/1-1/flash/esp")
    assert flash["images"][0]["offset"] == 0
    assert flash["images"][0]["file"] == puts[0].rsplit("/", 1)[1]
    assert bridge.files == {}  # staged file deleted after the job
    assert any("[bridge_rescue esp_flash 1-1]" in t["cmd"] for t in ctx.transcript)


def test_staged_name_stays_under_the_bridge_name_limit():
    name = br.staged_name("0123456789abcdef", "/tmp/" + "x" * 200 + " firmware (1).bin")
    assert len(name) < 64 and name.startswith("hil-01234567-")
    assert all(c.isalnum() or c in "._-" for c in name)


async def test_bridge_rescue_refuses_a_pre_1_0_bridge_without_any_post(tmp_path):
    bridge = CapsBridge(None, INFO_147)
    with pytest.raises(StageError, match="pre-1.0"):
        await run_stages([{"type": "bridge_rescue", "action": "console"}], _ctx(bridge, tmp_path))
    assert bridge.posts() == []


async def test_bridge_rescue_refuses_reset_mode_the_bridge_does_not_offer(tmp_path):
    bridge = CapsBridge(CAPS_133, INFO_133)
    with pytest.raises(StageError, match="reset mode"):
        await run_stages(
            [{"type": "bridge_rescue", "action": "reset", "mode": "rp_bootsel"}],
            _ctx(bridge, tmp_path),
        )
    assert bridge.posts() == []


async def test_bridge_rescue_explains_usbip_import_conflict(tmp_path):
    bridge = CapsBridge(CAPS_133, INFO_133)
    bridge.imported = True
    with pytest.raises(StageError, match="detach"):
        await run_stages(
            [{"type": "bridge_rescue", "action": "reset", "mode": "esp_hard_reset"}],
            _ctx(bridge, tmp_path),
        )


async def test_bridge_rescue_checks_staging_limit(tmp_path):
    doc = deepcopy(CAPS_133)
    doc["limits"]["rescue_staging_bytes"] = 16
    image = tmp_path / "big.bin"
    image.write_bytes(b"\x00" * 64)
    bridge = CapsBridge(doc, INFO_133)
    with pytest.raises(StageError, match="stages at most 16 bytes"):
        await run_stages(
            [{"type": "bridge_rescue", "action": "esp_flash"}],
            _ctx(bridge, tmp_path, local_firmware_path=str(image)),
        )
    assert [m for (m, _, _) in bridge.calls if m in ("PUT", "POST")] == []


def test_tool_actions_map_onto_bridge_rescue_or_refuse():
    stage, err = br.bridge_stage_for_tool("esptool", "write", address="0x10000")
    assert err == "" and stage == {"type": "bridge_rescue", "action": "esp_flash",
                                   "address": "0x10000"}
    assert br.bridge_stage_for_tool("esptool", "chip_id")[0]["action"] == "esp_info"
    assert br.bridge_stage_for_tool("esptool", "erase_flash")[0] is None
    stage, _ = br.bridge_stage_for_tool(
        "picotool", "reboot_bootsel", reset_modes=["picoboot_bootsel"]
    )
    assert stage == {"type": "bridge_rescue", "action": "reset", "mode": "picoboot_bootsel"}
    assert br.bridge_stage_for_tool("bossac", "info")[0]["action"] == "samba_info"
    assert br.bridge_stage_for_tool("bossac", "erase")[0] is None


# --------------------------------------------------------------------------- #
# firmware-bench: a bridge_rescue-only job never attaches over USB/IP         #
# --------------------------------------------------------------------------- #


async def test_bridge_only_job_skips_usbip_attach_and_local_serial(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from hil_controller.adapters.firmware_bench import FirmwareBenchAdapter

    bridge = CapsBridge(CAPS_133, INFO_133)
    controller = AsyncMock()
    tp = EspBridgeTransport(host_id="usbip-d19bee", addr="192.168.1.133", bridge=bridge.client())
    adapter = FirmwareBenchAdapter(
        controller_transport=controller,
        dut_transport=controller,
        hub_transport=tp,
        job_id="job-bridge",
        device={
            "id": "mcu-s31-d19c04-on-d19bee",
            "hub_port_path": "1-1",
            "serial_port": "/dev/ttyUSB9",
        },
        params={
            "bare": True,
            "window_minutes": 0,
            "stages": [{"type": "bridge_rescue", "action": "console"}],
        },
        jobs_dir=str(tmp_path),
        usbip_server_addr="192.168.1.133",
    )

    async def boom() -> None:
        raise AssertionError("bridge_rescue jobs must not usbip-attach the DUT")

    monkeypatch.setattr(adapter, "_attach_usbip", boom)
    await adapter.flash()
    assert "/api/usb/devices/1-1/console" in bridge.posts()
    assert adapter._serial is None  # no local serial capture on /dev/ttyUSB9
    controller.copy_to.assert_not_called()  # no firmware staged on a host
    await adapter._teardown()


def test_bridge_only_detection():
    from hil_controller.adapters.firmware_bench import FirmwareBenchAdapter

    assert FirmwareBenchAdapter._bridge_only([{"type": "bridge_rescue", "action": "info"}])
    assert not FirmwareBenchAdapter._bridge_only(
        [{"type": "bridge_rescue", "action": "info"}, {"type": "power_cycle"}]
    )
    assert not FirmwareBenchAdapter._bridge_only([])


# --------------------------------------------------------------------------- #
# web UI                                                                      #
# --------------------------------------------------------------------------- #

COOKIE = {"hil_token": "test-token-for-ci"}


@pytest_asyncio.fixture
async def ui(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from hil_controller.main import create_app

    db_file = str(tmp_path / "ui.db")
    os.environ["HIL_DB_PATH"] = db_file
    app = create_app(db_path=db_file)
    bridges = {
        "192.168.1.133": CapsBridge(CAPS_133, INFO_133),
        "192.168.1.147": CapsBridge(None, INFO_147),
    }
    monkeypatch.setattr(bd, "_client", lambda address, token=None: _factory(bridges)(address))
    async with app.router.lifespan_context(app):
        async with get_db(db_file) as conn:
            for hid, addr in (
                ("usbip-bridge-s31", "192.168.1.147"),
                ("usbip-d19bee", "192.168.1.133"),
            ):
                await conn.execute(
                    "INSERT INTO hosts (id, role, addr, transport, status, capabilities_json) "
                    "VALUES (?, 'microcontroller-fleet', ?, 'esp-usbip-bridge', 'available', '[]')",
                    (hid, addr),
                )
            for dev, host, busid in (
                ("mcu-s31-d19c04-on-d19bee", "usbip-d19bee", "1-1"),
                ("mcu-p4-on-147", "usbip-bridge-s31", "1-1.1"),
            ):
                await conn.execute(
                    "INSERT INTO devices (id, host_id, hub_host_id, hub_port_path, kind, model, "
                    "capabilities_json, pool, status) VALUES (?, 'localhost', ?, ?, "
                    "'microcontroller', 'esp32', '[]', 'bridge-uat', 'available')",
                    (dev, host, busid),
                )
            await conn.commit()
        registry = MagicMock()

        def transport_for(hid: str) -> EspBridgeTransport:
            addr = "192.168.1.133" if hid == "usbip-d19bee" else "192.168.1.147"
            return EspBridgeTransport(host_id=hid, addr=addr, bridge=bridges[addr].client())

        registry.transport_for = transport_for
        app.state.host_registry = registry
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", cookies=COOKIE
        ) as c:
            yield app, c, bridges


async def test_rescue_page_offers_bridge_actions_only_for_a_1_0_bridge(ui):
    _, c, _ = ui
    r = await c.get("/ui/rescue/mcu-s31-d19c04-on-d19bee")
    assert r.status_code == 200
    assert "Through the bridge (usbip-d19bee" in r.text
    assert '<option value="esp_flash">' in r.text
    assert '<option value="esp_bootloader">' in r.text
    r = await c.get("/ui/rescue/mcu-p4-on-147")
    assert "Through the bridge (usbip-bridge-s31" in r.text
    assert "pre-1.0" in r.text
    assert 'name="bridge_action"' not in r.text


async def test_rescue_bridge_action_submits_a_bridge_rescue_job(ui, monkeypatch):
    from hil_controller.web import router as web_router

    _, c, _ = ui
    submitted: list[dict] = []

    async def fake_call(_request, job_req, _token):
        submitted.append(job_req)
        return {"id": "job-123"}

    monkeypatch.setattr(web_router, "_call_jobs_api", fake_call)
    r = await c.post(
        "/ui/rescue/mcu-s31-d19c04-on-d19bee",
        data={
            "rescue_action": "bridge",
            "bridge_action": "reset",
            "bridge_mode": "esp_bootloader",
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/ui/jobs/job-123"
    stages = submitted[0]["params"]["stages"]
    assert stages == [{"type": "bridge_rescue", "action": "reset", "mode": "esp_bootloader"}]
    assert submitted[0]["params"]["bare"] is True
    # a pre-1.0 bridge is refused before any job is submitted
    r = await c.post(
        "/ui/rescue/mcu-p4-on-147", data={"rescue_action": "bridge", "bridge_action": "console"}
    )
    assert r.status_code == 200 and "pre-1.0" in r.text and len(submitted) == 1


async def test_tools_route_bridge_maps_esptool_and_refuses_pre_1_0(ui, monkeypatch):
    from hil_controller.web import router as web_router

    _, c, _ = ui
    submitted: list[dict] = []

    async def fake_call(_request, job_req, _token):
        submitted.append(job_req)
        return {"id": "job-9"}

    monkeypatch.setattr(web_router, "_call_jobs_api", fake_call)
    r = await c.post(
        "/ui/tools",
        data={
            "rescue_action": "esptool",
            "esptool_action": "flash_id",
            "route": "bridge",
            "device_id": "mcu-s31-d19c04-on-d19bee",
            "serial_port": "/dev/ttyUSB0",
        },
    )
    assert r.status_code == 303
    assert submitted[0]["params"]["stages"] == [{"type": "bridge_rescue", "action": "esp_info"}]
    assert "flash_serial_port" not in submitted[0]["params"]
    r = await c.post(
        "/ui/tools",
        data={
            "rescue_action": "esptool",
            "esptool_action": "flash_id",
            "route": "bridge",
            "device_id": "mcu-p4-on-147",
        },
    )
    assert r.status_code == 200 and "pre-1.0" in r.text and len(submitted) == 1


async def test_bridges_page_probe_and_apply(ui):
    _, c, _ = ui
    r = await c.get("/ui/twins/bridges")
    assert r.status_code == 200 and "Add by address" in r.text
    r = await c.post("/ui/twins/bridges/probe", data={"address": "192.168.1.147"})
    assert "known" in r.text and "usbip-bridge-s31" in r.text
    r = await c.post("/ui/twins/bridges/probe", data={"address": "192.168.1.133"})
    assert "known" in r.text and "usbip-d19bee" in r.text  # matched by address
    r = await c.post("/ui/twins/bridges/apply", data={"address": "192.168.1.133"})
    assert r.status_code == 409 and "already host" in r.text


async def test_host_and_usbip_pages_show_capabilities(ui):
    _, c, _ = ui
    r = await c.get("/ui/hosts/usbip-d19bee")
    assert r.status_code == 200
    assert "api <strong>1.0</strong>" in r.text and "flash_esp v1" in r.text
    r = await c.get("/ui/hosts/usbip-bridge-s31")
    assert "pre-1.0" in r.text


# --------------------------------------------------------------------------- #
# mDNS names: bridges are addressed as <hostname>.local, not by DHCP lease    #
# --------------------------------------------------------------------------- #


def _resolver(table: dict[str, list[str]]):
    async def resolve(name, **_kw):
        return list(table.get(name.lower().rstrip("."), []))

    return resolve


async def test_new_proposal_suggests_the_mdns_name_and_keeps_the_ip(tmp_path, monkeypatch):
    from hil_controller.adapters import esp_usbip_bridge as eub

    monkeypatch.setattr(eub, "resolve_ipv4", _resolver({"usbip-d19bee.local": ["192.168.1.133"]}))
    db = await _db_with_bridge_hosts(tmp_path)
    bridges = {
        "192.168.1.133": CapsBridge(CAPS_133, INFO_133),
        "usbip-d19bee.local": CapsBridge(CAPS_133, INFO_133),
    }
    p = await bd.probe_address(db, "192.168.1.133", client_factory=_factory(bridges))
    assert p.action == "new"
    assert p.address == "usbip-d19bee.local" and p.host["addr"] == "usbip-d19bee.local"
    assert p.ip == "192.168.1.133" and p.name_resolves is True
    # apply goes through the name and stores it
    host = await bd.apply_proposal(db, p.address, client_factory=_factory(bridges))
    assert host["addr"] == "usbip-d19bee.local"


async def test_new_proposal_falls_back_to_ip_when_the_name_does_not_resolve(tmp_path):
    db = await _db_with_bridge_hosts(tmp_path)  # conftest: nothing resolves
    p = await bd.probe_address(
        db, "192.168.1.133", client_factory=_factory({"192.168.1.133": CapsBridge(CAPS_133, {})})
    )
    assert p.action == "new" and p.address == "192.168.1.133"
    assert p.name_resolves is False and "does not resolve" in p.note


async def _db_with_named_host(tmp_path, mac_doc: dict[str, Any]) -> str:
    db = str(tmp_path / "named.db")
    await init_db(db)
    async with get_db(db) as conn:
        await conn.execute(
            "INSERT INTO hosts (id, role, addr, transport, status, capabilities_json) "
            "VALUES ('usbip-d19bee', 'microcontroller-fleet', 'usbip-d19bee.local', "
            "'esp-usbip-bridge', 'available', '[]')"
        )
        await conn.commit()
    await bc.store_capabilities(db, "usbip-d19bee", bc.parse_capabilities(mac_doc))
    return db


def _at(ip: str) -> dict[str, Any]:
    doc = deepcopy(CAPS_133)
    doc["network"]["interfaces"][0]["ip"] = ip
    return doc


async def test_a_name_addressed_host_whose_ip_changed_is_known_not_moved(tmp_path, monkeypatch):
    from hil_controller.adapters import esp_usbip_bridge as eub

    db = await _db_with_named_host(tmp_path, CAPS_133)
    # new DHCP lease: the bridge now answers at .150 and the name follows it
    monkeypatch.setattr(eub, "resolve_ipv4", _resolver({"usbip-d19bee.local": ["192.168.1.150"]}))
    here = {"192.168.1.150": CapsBridge(_at("192.168.1.150"), {})}
    p = await bd.probe_address(db, "192.168.1.150", client_factory=_factory(here))
    assert p.action == "known", p.move_reason
    assert p.match_reason == "base MAC 1c:29:04:d1:9b:ee"
    assert p.address == "usbip-d19bee.local" and p.ip == "192.168.1.150"


async def test_name_addressed_host_is_moved_only_when_the_name_breaks(tmp_path, monkeypatch):
    from hil_controller.adapters import esp_usbip_bridge as eub

    db = await _db_with_named_host(tmp_path, CAPS_133)
    here = {"192.168.1.150": CapsBridge(_at("192.168.1.150"), {})}
    # 1. the name stopped resolving
    p = await bd.probe_address(db, "192.168.1.150", client_factory=_factory(here))
    assert p.action == "moved" and "no longer resolves" in p.move_reason
    # 2. the name resolves somewhere that is the same bridge (another interface)
    monkeypatch.setattr(eub, "resolve_ipv4", _resolver({"usbip-d19bee.local": ["192.168.1.201"]}))
    same = {**here, "usbip-d19bee.local": CapsBridge(_at("192.168.1.201"), {})}
    p = await bd.probe_address(db, "192.168.1.150", client_factory=_factory(same))
    assert p.action == "known"
    # 3. the name now answers with a different base MAC
    other = deepcopy(CAPS_133)
    other["board"]["mac"] = "1c:29:04:aa:bb:cc"
    diff = {**here, "usbip-d19bee.local": CapsBridge(other, {})}
    p = await bd.probe_address(db, "192.168.1.150", client_factory=_factory(diff))
    assert p.action == "moved" and "different bridge (base MAC 1c:29:04:aa:bb:cc)" in p.move_reason


def test_is_mdns_name():
    from hil_controller.adapters.esp_usbip_bridge import bridge_base_url, is_mdns_name

    assert is_mdns_name("usbip-d19bee.local") and is_mdns_name("USBIP-D19BEE.local.")
    assert not is_mdns_name("192.168.1.133") and not is_mdns_name("rpi-hil006")
    assert bridge_base_url("usbip-d19bee.local") == "http://usbip-d19bee.local"


async def test_usbip_attach_resolves_local_names_on_the_controller(monkeypatch):
    """The receiver (e.g. a Pi without nss-mdns) gets the IPv4, not the name."""
    from unittest.mock import AsyncMock, MagicMock

    from hil_controller.adapters import esp_usbip_bridge as eub
    from hil_controller.adapters.usbip_bridge import UsbipBridge, VhciPort, stale_vhci_ports

    monkeypatch.setattr(eub, "resolve_ipv4", _resolver({"usbip-d19bee.local": ["192.168.1.133"]}))
    ok = MagicMock(exit_status=0, stdout="", stderr="")
    client = AsyncMock()
    client.exec = AsyncMock(return_value=ok)
    bridge = UsbipBridge(
        server_tp=MagicMock(usbip_exports_always=True),
        client_tp=client,
        server_addr="usbip-d19bee.local",
        busid="1-1",
    )
    await bridge.attach()
    assert client.exec.call_args.args[0] == [
        "sudo", "usbip", "attach", "-r", "192.168.1.133", "-b", "1-1"
    ]
    assert bridge.resolved_addr == "192.168.1.133"
    # vhci records written with the IP are still recognised as ours
    ports = [VhciPort(3, 6, "3-1", "192.168.1.133", "1-1")]
    assert stale_vhci_ports(
        ports, {"3-1"}, server_addr="usbip-d19bee.local", busid="1-1", aliases=["192.168.1.133"]
    ) == [3]
    # unresolvable on the controller: the name is passed through to the receiver
    monkeypatch.setattr(eub, "resolve_ipv4", _resolver({}))
    await bridge.attach()
    assert client.exec.call_args.args[0][4] == "usbip-d19bee.local"
    # plain IPs never go through the resolver
    ip_bridge = UsbipBridge(
        server_tp=MagicMock(usbip_exports_always=True),
        client_tp=client,
        server_addr="192.168.1.147",
        busid="1-1.1",
    )
    assert await ip_bridge.attach_addr() == "192.168.1.147"


# --------------------------------------------------------------------------- #
# a bridge that renames itself (same base MAC) is known, never new            #
# --------------------------------------------------------------------------- #

RENAMED = deepcopy(CAPS_133)
RENAMED["board"]["hostname"] = "usbip-s31-d19bee"  # firmware's new default scheme


async def test_bridge_renamed_itself_is_known_by_mac_not_new(tmp_path, monkeypatch):
    """usbip-d19bee -> usbip-s31-d19bee after a firmware update: same base MAC.

    The host still points at the old .local name, which no longer resolves; the
    proposal stays ``known`` and suggests the new name, editing nothing.
    """
    from hil_controller.adapters import esp_usbip_bridge as eub

    db = await _db_with_named_host(tmp_path, CAPS_133)  # id/addr use the old name
    before = await _hosts_snapshot(db)
    monkeypatch.setattr(
        eub, "resolve_ipv4", _resolver({"usbip-s31-d19bee.local": ["192.168.1.133"]})
    )
    avahi = (
        '=;eth0;IPv4;usbip-s31-d19bee;_hil-bridge._tcp;local;usbip-s31-d19bee.local;'
        '192.168.1.133;80;"api=1.0" "mac=1c:29:04:d1:9b:ee"\n'
    )
    out = await bd.discover(
        db,
        browse_result=bd.BrowseResult(bd.parse_avahi_browse(avahi), "avahi-browse"),
        client_factory=_factory({"192.168.1.133": CapsBridge(RENAMED, {})}),
    )
    (p,) = out["proposals"]
    assert p["action"] == "known"
    assert p["matched_host_id"] == "usbip-d19bee"
    assert p["match_reason"] == "base MAC 1c:29:04:d1:9b:ee"
    assert p["renamed_from"] == "usbip-d19bee"
    assert p["address"] == "usbip-s31-d19bee.local"
    assert "edit its address to usbip-s31-d19bee.local" in p["note"]
    assert p["host"] is None  # nothing to create
    assert out["not_seen"] == []
    assert await _hosts_snapshot(db) == before  # never edited automatically


async def test_ip_addressed_bridge_that_renamed_itself_is_plain_known(tmp_path):
    db = await _db_with_bridge_hosts(tmp_path)  # usbip-bridge-s31 @ 192.168.1.147
    old = deepcopy(CAPS_133)
    old["board"].update(mac="1c:29:04:d0:7a:04", hostname="usbip-d07a04")
    old["network"]["interfaces"][0]["ip"] = "192.168.1.147"
    await bc.store_capabilities(db, "usbip-bridge-s31", bc.parse_capabilities(old))
    new = deepcopy(old)
    new["board"]["hostname"] = "usbip-s31-d07a04"
    p = await bd.probe_address(
        db, "192.168.1.147", client_factory=_factory({"192.168.1.147": CapsBridge(new, {})})
    )
    assert p.action == "known" and p.matched_host_id == "usbip-bridge-s31"
    assert p.renamed_from == "usbip-d07a04" and p.address == "192.168.1.147"
