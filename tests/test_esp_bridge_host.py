"""esp-usbip-bridge hosts end to end in the controller (no hardware).

Covers the ``transport: esp-usbip-bridge`` host handle, topology seeding of the
new fields, inventory mapping onto the exportable-busid model, usbip attach
without bind/unbind (+ the re-attach keeper), bridge port power in the bench
stages and availability probe, and routing the I2C strand mux to the bridge.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller import host_recovery
from hil_controller.adapters import bench_stages, usbip_bridge
from hil_controller.adapters import esp_usbip_bridge as eub
from hil_controller.adapters.bench_stages import BenchContext, StageError, run_stages
from hil_controller.adapters.usbip_bridge import UsbipAttachKeeper, UsbipBridge
from hil_controller.adapters.usbip_inventory import query_host_busids
from hil_controller.db.connection import get_db, init_db
from hil_controller.hosts.base import ExecResult
from hil_controller.hosts.esp_bridge import (
    EspBridgeTransport,
    bridge_client_of,
    resolve_aux_interface,
)
from hil_controller.hosts.local import LocalTransport
from hil_controller.hosts.registry import RealHostRegistry
from hil_controller.topology.seeder import seed_topology
from tests.fake_esp_bridge import FakeBridge, qtpy

EXAMPLE = "deploy/topology.esp-usbip-bridge.example.yaml"


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


def _bridge_tp(fake: FakeBridge) -> EspBridgeTransport:
    return EspBridgeTransport(
        host_id="usbip-bridge-s31", addr="usbip-test.local", bridge=fake.client()
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """asyncio.sleep yields but does not wait (bench stages, keeper, bridge polls)."""
    real_sleep = asyncio.sleep

    async def _instant(_s):
        await real_sleep(0)

    monkeypatch.setattr(eub.asyncio, "sleep", _instant)


# --------------------------------------------------------------------------- #
# host transport + registry                                                   #
# --------------------------------------------------------------------------- #


def test_registry_builds_bridge_transport_with_url_and_token_env(monkeypatch):
    monkeypatch.setenv("HIL_BRIDGE_TOKEN_TEST", "sekret")
    reg = RealHostRegistry(topology_file="", db_path=":memory:")
    t = reg._build_transport(
        {
            "id": "usbip-bridge-s31",
            "transport": "esp-usbip-bridge",
            "addr": "usbip-a1b2c3.local",
            "token_env": "HIL_BRIDGE_TOKEN_TEST",
        }
    )
    assert isinstance(t, EspBridgeTransport)
    assert t.usbip_exports_always is True
    assert t.bridge.base_url == "http://usbip-a1b2c3.local"
    assert t.bridge.token == "sekret"


def test_registry_bridge_api_url_override_and_no_token(monkeypatch):
    monkeypatch.delenv("HIL_BRIDGE_TOKEN_UNSET", raising=False)
    reg = RealHostRegistry(topology_file="", db_path=":memory:")
    t = reg._build_transport(
        {
            "id": "b",
            "transport": "esp-usbip-bridge",
            "addr": "10.0.0.5",
            "api_url": "http://10.0.0.5:8080",
            "token_env": "HIL_BRIDGE_TOKEN_UNSET",
        }
    )
    assert t.bridge.base_url == "http://10.0.0.5:8080"
    assert t.bridge.token is None


async def test_bridge_transport_has_no_shell_but_healthchecks_over_http():
    fake = FakeBridge()
    t = _bridge_tp(fake)
    res = await t.exec(["test", "-e", "/sys/bus/usb/devices/1-1.1"])
    assert res.exit_status == 127
    assert "no shell" in res.stderr
    assert await t.healthcheck() is True
    with pytest.raises(NotImplementedError):
        await t.copy_to(None, None)  # type: ignore[arg-type]


def test_bridge_client_of_sees_through_recording_proxy():
    fake = FakeBridge()
    t = _bridge_tp(fake)
    proxy = bench_stages._RecordingTransport(t, lambda argv, res: None)
    assert bridge_client_of(proxy) is t.bridge
    assert bridge_client_of(AsyncMock()) is None  # mocks are not mistaken for bridges


def test_make_adapter_firmware_bench_on_bridge_runs_dut_side_on_controller():
    from hil_controller.adapters.firmware_bench import FirmwareBenchAdapter

    reg = RealHostRegistry(topology_file="", db_path=":memory:")
    reg._hosts = [
        {"id": "usbip-bridge-s31", "transport": "esp-usbip-bridge", "addr": "usbip-a1b2c3.local"}
    ]
    device = {
        "id": "mcu-qtpy",
        "host_id": "usbip-bridge-s31",
        "hub_port_path": "1-1.1",
        "power_control": "bridge-port",
    }
    adapter = reg.make_adapter(
        reg._hosts[0],
        device,
        {"script": "firmware-bench", "params": {"firmware": {"path": "/tmp/fw.bin"}}},
        "job-1",
    )
    assert isinstance(adapter, FirmwareBenchAdapter)
    assert isinstance(adapter.dut_transport, LocalTransport)  # flash/serial on the controller
    assert isinstance(adapter.hub_transport, EspBridgeTransport)  # power + presence on the bridge
    assert adapter.usbip_server_addr == "usbip-a1b2c3.local"


# --------------------------------------------------------------------------- #
# topology seeding + migration                                                #
# --------------------------------------------------------------------------- #


async def test_example_topology_seeds_bridge_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_BRIDGE_TOKEN_S31", "sekret")
    db = str(tmp_path / "b.db")
    await init_db(db)
    await seed_topology(db, EXAMPLE)
    async with get_db(db) as conn:
        host = dict(
            await (await conn.execute("SELECT * FROM hosts WHERE id='usbip-bridge-s31'")).fetchone()
        )
        dev = dict(
            await (
                await conn.execute("SELECT * FROM devices WHERE id='mcu-qtpy-esp32s3-bridge-s31'")
            ).fetchone()
        )
    assert host["transport"] == "esp-usbip-bridge"
    assert host["token_env"] == "HIL_BRIDGE_TOKEN_S31"
    assert host["api_url"] is None
    assert dev["power_control"] == "bridge-port"
    assert dev["hub_port_path"] == "1-1.1"
    # the analog-mux aux shares the bridge host's URL + token
    url, token = await resolve_aux_interface(db, "bridge:usbip-bridge-s31")
    assert (url, token) == ("http://usbip-a1b2c3.local", "sekret")


async def test_migration_adds_columns_to_an_old_db(tmp_path):
    import aiosqlite

    db = str(tmp_path / "old.db")
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE hosts (id TEXT PRIMARY KEY, addr TEXT)")
        await conn.execute("CREATE TABLE devices (id TEXT PRIMARY KEY, host_id TEXT, kind TEXT)")
        await conn.commit()
    await init_db(db)
    async with get_db(db) as conn:
        host_rows = await (await conn.execute("PRAGMA table_info(hosts)")).fetchall()
        dev_rows = await (await conn.execute("PRAGMA table_info(devices)")).fetchall()
    host_cols = {r["name"] for r in host_rows}
    dev_cols = {r["name"] for r in dev_rows}
    assert {"api_url", "token_env"} <= host_cols
    assert "power_control" in dev_cols


async def test_resolve_aux_interface_plain_url_and_unknown_host(tmp_path):
    db = str(tmp_path / "x.db")
    await init_db(db)
    assert await resolve_aux_interface(db, "http://mux:8080") == ("http://mux:8080", None)
    assert await resolve_aux_interface(db, "bridge:nope") == (None, None)


# --------------------------------------------------------------------------- #
# inventory                                                                   #
# --------------------------------------------------------------------------- #


async def test_inventory_maps_bridge_devices_onto_exportable_rows():
    fake = FakeBridge()
    virtual = {
        "busid": "2-1",
        "vid": "303a",
        "pid": "4002",
        "description": "test harness",
        "virtual": True,
        "speed": "full",
        "speed_mbps": 12,
        "max_power_ma": None,
        "port_power_status": None,
        "port_connect_status": None,
    }
    fake.devices["2-1"] = virtual
    inv = await query_host_busids(
        _bridge_tp(fake), host_id="usbip-bridge-s31", device_busid_map={"1-1.1": "mcu-qtpy"}
    )
    assert inv.daemon_listening is True
    rows = {b.busid: b for b in inv.busids}
    q = rows["1-1.1"]
    assert (q.vid, q.pid, q.matched_device_id) == ("239a", "8143", "mcu-qtpy")
    assert q.manufacturer == "Adafruit" and q.product == "QT Py ESP32-S3"
    assert q.serial == "7C9E1234"
    assert q.speed == "12M" and q.max_power == "100mA"
    assert q.device_class == "0xef" and q.num_interfaces == 2
    assert q.lsusb_description == "qtpy-s3"
    assert q.port_power_status == "on" and q.port_connect_status == "connected"
    assert "per-port power switching" in (q.port_status_text or "")
    assert rows["2-1"].port_status_text == "virtual device (bridge-internal)"
    assert rows["2-1"].matched_device_id is None
    assert inv.hub_info[0].location == "1-1"
    assert inv.hub_info[0].ports[0]["power_switching"] == "per-port"
    # nothing ran over a shell
    assert all(m == "GET" for (m, _, _) in fake.calls)


async def test_inventory_surfaces_ganged_hub():
    fake = FakeBridge(switching="ganged")
    inv = await query_host_busids(_bridge_tp(fake), host_id="b", device_busid_map={})
    assert "ganged power switching" in (inv.busids[0].port_status_text or "")


async def test_inventory_bridge_down_reports_not_listening():
    fake = FakeBridge()
    fake.fail_status = 503
    inv = await query_host_busids(_bridge_tp(fake), host_id="b", device_busid_map={})
    assert inv.daemon_listening is False
    assert inv.busids == []
    assert "503" in (inv.error or "")


# --------------------------------------------------------------------------- #
# usbip attach without bind/unbind                                            #
# --------------------------------------------------------------------------- #


async def test_usbip_attach_from_bridge_skips_bind_and_unbind():
    fake = FakeBridge()
    server = _bridge_tp(fake)
    client = AsyncMock()
    port_listing = "Port 00: <Port in Use> at Full Speed(12Mbps)\n  1-1 -> usbip://x:3240/1-1.1\n"
    ls_calls = {"n": 0}

    async def client_exec(argv, **kw):
        if argv[:2] == ["bash", "-c"]:  # list_serial_ports: before / after
            ls_calls["n"] += 1
            return _result(
                0, "/dev/ttyACM0\n" if ls_calls["n"] == 1 else "/dev/ttyACM0\n/dev/ttyACM1\n"
            )
        if argv[-1:] == ["port"]:
            return _result(0, port_listing)
        return _result(0)

    client.exec = AsyncMock(side_effect=client_exec)
    bridge = UsbipBridge(
        server_tp=server,
        client_tp=client,
        server_addr="usbip-test.local",
        busid="1-1.1",
        settle_s=0,
    )
    assert bridge.bind_required is False
    async with bridge.attached() as tty:
        assert tty == "/dev/ttyACM1"
    argvs = [c.args[0] for c in client.exec.call_args_list]
    assert ["sudo", "usbip", "attach", "-r", "usbip-test.local", "-b", "1-1.1"] in argvs
    assert ["sudo", "usbip", "detach", "-p", "00"] in argvs
    assert not any("bind" in a or "unbind" in a for argv in argvs for a in argv)
    assert fake.calls == []  # the bridge was never asked to bind anything


def test_ssh_server_still_binds():
    bridge = UsbipBridge(server_tp=AsyncMock(), client_tp=AsyncMock(), server_addr="x", busid="1-1")
    assert bridge.bind_required is True


async def test_attach_keeper_reattaches_after_reenumeration():
    attached = {"on": False}

    async def client_exec(argv, **kw):
        if argv[-1:] == ["port"]:
            text = (
                "Port 00: <Port in Use>\n  1-1 -> usbip://x:3240/1-1.1\n" if attached["on"] else ""
            )
            return _result(0, text)
        if "attach" in argv:
            attached["on"] = True
        if "detach" in argv:
            attached["on"] = False
        return _result(0)

    client = AsyncMock()
    client.exec = AsyncMock(side_effect=client_exec)
    bridge = UsbipBridge(
        server_tp=_bridge_tp(FakeBridge()), client_tp=client, server_addr="x", busid="1-1.1"
    )
    present = {"v": True}

    async def is_present():
        return present["v"]

    keeper = UsbipAttachKeeper(bridge, present=is_present, poll_s=0)
    assert await keeper.ensure_attached() is True
    assert keeper.reattach_count == 1
    assert await keeper.ensure_attached() is True  # already attached: no new attach
    assert keeper.reattach_count == 1
    attached["on"] = False  # DUT re-enumerated → client lost the attachment
    present["v"] = False  # ...and it is not back on the bridge yet
    assert await keeper.ensure_attached() is False
    present["v"] = True
    assert await keeper.ensure_attached() is True
    assert keeper.reattach_count == 2
    keeper.start()
    await asyncio.sleep(0)
    await keeper.stop()
    assert attached["on"] is False  # stop() detaches


# A bridge reboot left vhci port 0 in use with no USB device behind it (#5);
# port 1 is another job's healthy attachment from a different server.
_VHCI_STALE_PROBE = """\
hub port sta spd dev      sockfd local_busid
hs  0000 006 002 00010078 000003 1-1
hs  0001 006 002 00010079 000004 1-2
hs  0002 004 000 00000000 000000 0-0
--records--
0 x 3240 1-1.1
1 other-bridge 3240 1-1.3
--devices--
1-0:1.0
1-2
1-2:1.0
usb1
"""
_VHCI_ERROR = (
    "usbip: error: open vhci_driver (is vhci_hcd loaded?)\nusbip: error: list imported devices\n"
)


def test_stale_vhci_ports_only_orphans_and_our_own():
    ports, devices = usbip_bridge.parse_vhci_probe(_VHCI_STALE_PROBE)
    assert [(p.port, p.status, p.local_busid) for p in ports] == [
        (0, 6, "1-1"),
        (1, 6, "1-2"),
        (2, 4, "0-0"),
    ]
    assert ports[0].remote_host == "x" and ports[0].remote_busid == "1-1.1"
    assert "1-2" in devices and "1-1" not in devices
    # Port 0 has no sysfs device; port 1 is live and someone else's.
    assert usbip_bridge.stale_vhci_ports(ports, devices, server_addr="x", busid="1-1.1") == [0]
    assert usbip_bridge.stale_vhci_ports(ports, devices, server_addr="y", busid="1-9") == [0]
    # Our own earlier attachment is replaced even while its device is present.
    assert usbip_bridge.stale_vhci_ports(
        ports, devices | {"1-1"}, server_addr="x", busid="1-1.1"
    ) == [0]
    assert usbip_bridge.stale_vhci_ports(ports, devices | {"1-1"}, server_addr="z", busid="1") == []


def _stale_vhci_client(state: dict) -> AsyncMock:
    """Client whose every usbip command fails until stale port 0 is cleared."""

    async def client_exec(argv, **kw):
        cmd = " ".join(argv)
        if "--records--" in cmd:  # the stale-port probe
            return _result(0, _VHCI_STALE_PROBE if state["broken"] else "")
        if "vhci_hcd.0/detach" in cmd:
            state["cleared"].append(cmd)
            if "echo 0 " in cmd:
                state["broken"] = False
            return _result(0)
        if "usbip" in argv and state["broken"]:
            return _result(1, "", _VHCI_ERROR)
        if argv[-1:] == ["port"]:
            text = "Port 03: <Port in Use>\n  3-1 -> usbip://x:3240/1-1.1\n" if state["on"] else ""
            return _result(0, text)
        if "attach" in argv:
            state["on"] = True
        return _result(0)

    client = AsyncMock()
    client.exec = AsyncMock(side_effect=client_exec)
    return client


async def test_attach_keeper_clears_stale_vhci_port_after_bridge_reboot():
    state = {"broken": True, "on": False, "cleared": []}
    client = _stale_vhci_client(state)
    bridge = UsbipBridge(
        server_tp=_bridge_tp(FakeBridge()), client_tp=client, server_addr="x", busid="1-1.1"
    )
    keeper = UsbipAttachKeeper(bridge, poll_s=0)
    assert await keeper.ensure_attached() is True
    assert keeper.reattach_count == 1
    # Cleared through sysfs, with sudo, and only the orphaned port.
    assert state["cleared"] == ["sudo sh -c echo 0 > /sys/devices/platform/vhci_hcd.0/detach"]
    attaches = [c.args[0] for c in client.exec.call_args_list if "attach" in c.args[0]]
    assert len(attaches) == 2  # failed once, then succeeded after the clear


async def test_detach_clears_stale_vhci_port():
    state = {"broken": True, "on": False, "cleared": []}
    client = _stale_vhci_client(state)
    bridge = UsbipBridge(
        server_tp=_bridge_tp(FakeBridge()), client_tp=client, server_addr="x", busid="1-1.1"
    )
    await bridge.detach(check=False)
    assert len(state["cleared"]) == 1 and state["broken"] is False


async def test_attach_without_stale_ports_fails_once_without_retry():
    async def client_exec(argv, **kw):
        if "--records--" in " ".join(argv):
            return _result(0, "hub port sta spd dev sockfd local_busid\n--records--\n--devices--\n")
        if "usbip" in argv:
            return _result(1, "", _VHCI_ERROR)
        return _result(0)

    client = AsyncMock()
    client.exec = AsyncMock(side_effect=client_exec)
    bridge = UsbipBridge(
        server_tp=_bridge_tp(FakeBridge()), client_tp=client, server_addr="x", busid="1-1.1"
    )
    with pytest.raises(RuntimeError, match="open vhci_driver"):
        await bridge.attach()
    argvs = [c.args[0] for c in client.exec.call_args_list]
    assert sum("attach" in a for a in argvs) == 1
    assert not any("vhci_hcd.0/detach" in " ".join(a) for a in argvs)


# --------------------------------------------------------------------------- #
# bench stages: power_cycle via bridge port                                   #
# --------------------------------------------------------------------------- #


def _ctx(fake: FakeBridge, device: dict, dut: AsyncMock | None = None) -> BenchContext:
    if dut is None:
        dut = AsyncMock()
        dut.exec = AsyncMock(return_value=_result(0))
    return BenchContext(
        dut_transport=dut,
        hub_transport=_bridge_tp(fake),
        flash_serial_port="",
        device=device,
    )


async def test_power_cycle_stage_uses_bridge_port(no_sleep):
    fake = FakeBridge()
    dut = AsyncMock()
    dut.exec = AsyncMock(return_value=_result(0))
    ctx = _ctx(fake, {"hub_port_path": "1-1.1", "power_control": "bridge-port"}, dut=dut)
    await run_stages([{"type": "power_cycle"}], ctx)
    assert [p for p, _ in fake.posts()] == ["/api/ports/1-1.1/off", "/api/ports/1-1.1/on"]
    assert dut.exec.await_count == 0  # no esptool fallback, no `test -e`
    assert "1-1.1" in fake.devices


async def test_power_cycle_stage_timed_when_detection_disabled(no_sleep):
    fake = FakeBridge()
    ctx = _ctx(fake, {"hub_port_path": "1-1.1", "power_control": "bridge-port"})
    await run_stages([{"type": "power_cycle", "await_enumeration": False}], ctx)
    assert [p for p, _ in fake.posts()] == ["/api/ports/1-1.1/off", "/api/ports/1-1.1/on"]
    assert not any(m == "GET" for (m, _, _) in fake.calls)  # no presence polling


async def test_power_cycle_stage_ganged_hub_fails_clearly(no_sleep):
    fake = FakeBridge(switching="ganged")
    ctx = _ctx(fake, {"hub_port_path": "1-1.1", "power_control": "bridge-port"})
    with pytest.raises(StageError, match="bridge-port:force"):
        await run_stages([{"type": "power_cycle"}], ctx)


async def test_power_cycle_stage_force_switches_ganged_hub(no_sleep):
    fake = FakeBridge(switching="ganged")
    ctx = _ctx(fake, {"hub_port_path": "1-1.1", "power_control": "bridge-port:force"})
    await run_stages([{"type": "power_cycle"}], ctx)
    assert fake.posts()[0] == ("/api/ports/1-1.1/off", {"force": True})


async def test_power_cycle_stage_waits_for_controller_serial_node(no_sleep, monkeypatch):
    """Attached over usbip, the serial node returns only after re-attach."""
    fake = FakeBridge()
    seq = iter([1, 0])  # absent on the first probe, then present

    async def dut_exec(argv, **kw):
        if argv[:2] == ["test", "-e"]:
            return _result(next(seq, 0))
        return _result(0)

    dut = AsyncMock()
    dut.exec = AsyncMock(side_effect=dut_exec)
    ctx = _ctx(fake, {"hub_port_path": "1-1.1", "power_control": "bridge-port"}, dut=dut)
    ctx.log_serial_port = "/dev/serial/by-id/usb-Adafruit_QT_Py-if00"
    await run_stages([{"type": "power_cycle"}], ctx)
    probes = [c.args[0] for c in dut.exec.call_args_list if c.args[0][:2] == ["test", "-e"]]
    assert len(probes) == 2


async def test_bridge_power_control_without_bridge_host_is_a_stage_error():
    ctx = BenchContext(
        dut_transport=AsyncMock(),
        hub_transport=AsyncMock(),
        flash_serial_port="",
        device={"hub_port_path": "1-1.1", "power_control": "bridge-port"},
    )
    with pytest.raises(StageError, match="esp-usbip-bridge"):
        await run_stages([{"type": "power_cycle"}], ctx)


async def test_flash_recovery_power_cycles_bridge_port(no_sleep, monkeypatch):
    """_recover_download_via_hub works for a bridge-port DUT (no solenoid channel)."""
    fake = FakeBridge()
    ctx = _ctx(fake, {"hub_port_path": "1-1.1", "power_control": "bridge-port"})
    flasher = MagicMock()
    flasher.force_download_via_reset = AsyncMock(return_value=True)
    monkeypatch.setattr(BenchContext, "make_flasher", lambda self, which: flasher)
    await bench_stages._recover_download_via_hub({}, ctx, reason="test")
    assert [p for p, _ in fake.posts()] == ["/api/ports/1-1.1/off", "/api/ports/1-1.1/on"]
    flasher.force_download_via_reset.assert_awaited()


# --------------------------------------------------------------------------- #
# I2C strand mux on the bridge                                                #
# --------------------------------------------------------------------------- #


class _FakeMux:
    seen: dict = {}

    def __init__(self, base_url, token=None):
        _FakeMux.seen = {"base": base_url, "token": token}

    async def select(self, group, channel):
        _FakeMux.seen["select"] = (group, channel)
        return {"active": f"{group}:{channel}"}

    async def isolate(self):
        _FakeMux.seen["isolate"] = True
        return {}


async def test_select_strand_routes_to_bridge_mux_with_host_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_BRIDGE_TOKEN_S31", "sekret")
    db = str(tmp_path / "m.db")
    await init_db(db)
    await seed_topology(db, EXAMPLE)
    monkeypatch.setattr(bench_stages, "AnalogMuxAdapter", _FakeMux)
    ctx = BenchContext(
        dut_transport=object(),
        hub_transport=object(),
        flash_serial_port="",
        device={"id": "mcu-feather-esp8266-bridge-s31"},
        db_path=db,
    )
    await run_stages([{"type": "select_i2c_strand", "strand_id": "strand-bridge-s31-env"}], ctx)
    assert _FakeMux.seen["base"] == "http://usbip-a1b2c3.local"
    assert _FakeMux.seen["token"] == "sekret"
    assert _FakeMux.seen["select"] == ("muxB", 5)
    await run_stages([{"type": "isolate_i2c_strand", "strand_id": "strand-bridge-s31-env"}], ctx)
    assert _FakeMux.seen["isolate"] is True


async def test_real_mux_adapter_talks_to_bridge_api():
    """The existing AnalogMuxAdapter works unchanged against the bridge's API."""
    from hil_controller.adapters.analog_mux import AnalogMuxAdapter

    fake = FakeBridge(token="sekret")
    mux = AnalogMuxAdapter(fake.client().base_url, token="sekret", client=fake.client()._client)
    await mux.select("muxA", 2)
    await mux.isolate()
    assert fake.posts() == [("/api/groups/muxA/select/2", None), ("/api/isolate", None)]


# --------------------------------------------------------------------------- #
# availability probe                                                          #
# --------------------------------------------------------------------------- #


async def test_validate_bridge_presence_active_powers_on_then_off(no_sleep):
    fake = FakeBridge()
    fake._set_power("1-1.1", False)  # idle-off under on-demand power
    ok = await host_recovery.validate_bridge_presence(
        fake.client(), {"hub_port_path": "1-1.1", "power_control": "bridge-port"}, poll_s=0
    )
    assert ok is True
    assert [p for p, _ in fake.posts()] == ["/api/ports/1-1.1/on", "/api/ports/1-1.1/off"]
    assert fake.power["1-1.1"] is False  # left idle-off


async def test_validate_bridge_presence_passive_without_bridge_power():
    fake = FakeBridge()
    assert await host_recovery.validate_bridge_presence(fake.client(), {"hub_port_path": "1-1.1"})
    assert not await host_recovery.validate_bridge_presence(
        fake.client(), {"hub_port_path": "1-1.9"}
    )
    assert fake.posts() == []
    assert not await host_recovery.validate_bridge_presence(fake.client(), {})


async def test_validate_bridge_presence_absent_device(no_sleep):
    fake = FakeBridge()
    fake.devices.pop("1-1.1")
    ok = await host_recovery.validate_bridge_presence(
        fake.client(),
        {"hub_port_path": "1-1.1", "power_control": "bridge-port"},
        settle_s=0,
        poll_s=0,
    )
    assert ok is False
    assert fake.posts()[-1][0] == "/api/ports/1-1.1/off"  # always switched back off


# --------------------------------------------------------------------------- #
# firmware-bench on a bridge DUT                                              #
# --------------------------------------------------------------------------- #


async def test_firmware_bench_powers_attaches_and_tears_down(no_sleep, tmp_path, monkeypatch):
    from hil_controller.adapters import firmware_bench as fb
    from hil_controller.adapters.firmware_bench import FirmwareBenchAdapter

    fake = FakeBridge()
    fake._set_power("1-1.1", False)
    attached = {"on": False}
    ls = {"n": 0}

    async def ctl_exec(argv, **kw):
        if argv[:2] == ["bash", "-c"]:
            ls["n"] += 1
            return _result(0, "" if ls["n"] == 1 else "/dev/ttyACM3\n")
        if argv[-1:] == ["port"]:
            return _result(
                0, "Port 00: <Port in Use>\n  1-1 -> usbip://x/1-1.1\n" if attached["on"] else ""
            )
        if "attach" in argv:
            attached["on"] = True
        if "detach" in argv:
            attached["on"] = False
        return _result(0)

    controller = AsyncMock()
    controller.exec = AsyncMock(side_effect=ctl_exec)
    adapter = FirmwareBenchAdapter(
        controller_transport=controller,
        dut_transport=controller,
        hub_transport=_bridge_tp(fake),
        job_id="job-b",
        device={"id": "mcu-qtpy", "hub_port_path": "1-1.1", "power_control": "bridge-port"},
        params={},
        jobs_dir=str(tmp_path),
        usbip_server_addr="usbip-test.local",
    )
    await adapter._power_on_dut()
    assert fake.power["1-1.1"] is True and "1-1.1" in fake.devices
    await adapter._attach_usbip()
    assert attached["on"] is True
    assert adapter._usbip_tty == "/dev/ttyACM3"
    argvs = [c.args[0] for c in controller.exec.call_args_list]
    assert ["sudo", "modprobe", "vhci-hcd"] in argvs
    assert ["sudo", "usbip", "attach", "-r", "usbip-test.local", "-b", "1-1.1"] in argvs
    await adapter._teardown()
    assert attached["on"] is False  # detached
    assert fake.power["1-1.1"] is False  # idle-off again
    # never logged the controller's own gh/git out
    assert not any(
        "gh auth logout" in " ".join(a) for a in (c.args[0] for c in controller.exec.call_args_list)
    )


# --------------------------------------------------------------------------- #
# REST: GET /v1/hosts/{id}/usbip/exportable for a bridge host                 #
# --------------------------------------------------------------------------- #


async def test_exportable_endpoint_lists_bridge_duts(tmp_path, monkeypatch):
    import os

    from httpx import ASGITransport, AsyncClient

    from hil_controller.main import create_app

    db_file = str(tmp_path / "api.db")
    os.environ["HIL_DB_PATH"] = db_file
    app = create_app(db_path=db_file)
    fake = FakeBridge()
    async with app.router.lifespan_context(app):
        async with get_db(db_file) as db:
            await db.execute(
                "INSERT INTO hosts (id, addr, transport, capabilities_json) VALUES (?, ?, ?, ?)",
                ("usbip-bridge-s31", "usbip-test.local", "esp-usbip-bridge", "[]"),
            )
            await db.execute(
                "INSERT INTO devices (id, host_id, hub_host_id, hub_port_path, kind, "
                "capabilities_json, power_control) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "mcu-qtpy",
                    "usbip-bridge-s31",
                    "usbip-bridge-s31",
                    "1-1.1",
                    "microcontroller",
                    "[]",
                    "bridge-port",
                ),
            )
            await db.commit()
        registry = MagicMock()
        registry.transport_for = MagicMock(return_value=_bridge_tp(fake))
        app.state.host_registry = registry
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token-for-ci"},
        ) as client:
            resp = await client.get("/v1/hosts/usbip-bridge-s31/usbip/exportable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["daemon_listening"] is True
    assert body["dev_links"] is None
    (row,) = body["busids"]
    assert row["busid"] == "1-1.1"
    assert row["matched_device_id"] == "mcu-qtpy"
    assert row["vid"] == "239a" and row["serial"] == "7C9E1234"
    assert "per-port power switching" in row["port_status_text"]
