"""Tests for device matching and no-match handling in the host registry."""

import pytest

from hil_controller.hosts.registry import HostRegistry, RealHostRegistry, _UnmatchedAdapter


def _registry(devices, hosts=None):
    reg = HostRegistry(topology_file="")
    reg._hosts = hosts or [{"id": "h1"}]
    reg._devices = devices
    return reg


_MCU = {
    "id": "mcu-revtft",
    "host_id": "h1",
    "kind": "microcontroller",
    "model": "Feather ESP32-S3 Reverse TFT",
    "pool": "public",
    "capabilities": ["arduino", "wippersnapper", "tft-display"],
    "status": "available",
}


def test_explicit_id_skips_pool_gate():
    reg = _registry([_MCU])
    # Job pins a pool the device is NOT in, but selects it explicitly by id.
    req = {"target": {"pool": "wippersnapper-arduino", "device": {"id": "mcu-revtft"}}}
    result = reg.find_device_for_job(req)
    assert result is not None
    _host, device = result
    assert device["id"] == "mcu-revtft"


def test_explicit_id_unavailable_no_match():
    busy = {**_MCU, "status": "busy"}
    reg = _registry([busy])
    req = {"target": {"device": {"id": "mcu-revtft"}}}
    assert reg.find_device_for_job(req) is None


def test_pool_mismatch_no_match_without_id():
    reg = _registry([_MCU])
    req = {"target": {"pool": "wippersnapper-arduino", "device": {"kind": "microcontroller"}}}
    assert reg.find_device_for_job(req) is None


def test_capability_subset_match():
    reg = _registry([_MCU])
    req = {"target": {"pool": "public", "device": {"capabilities": ["wippersnapper"]}}}
    assert reg.find_device_for_job(req) is not None


def test_capability_not_subset_no_match():
    reg = _registry([_MCU])
    req = {"target": {"pool": "public", "device": {"capabilities": ["bluetooth"]}}}
    assert reg.find_device_for_job(req) is None


@pytest.mark.asyncio
async def test_unmatched_adapter_acquire_raises():
    adapter = _UnmatchedAdapter("no device matched (pool='x')")
    with pytest.raises(RuntimeError, match="no device matched"):
        await adapter.acquire()


def test_make_adapter_routes_arduino_ws_to_exec_adapter():
    from hil_controller.adapters.arduino_ws_exec import ArduinoWsExecAdapter
    from hil_controller.hosts.local import LocalTransport

    reg = RealHostRegistry(topology_file="", db_path="db")
    # The device lives on rpi-displays (SSH) — its USB is there. "controller"
    # must still resolve to LocalTransport (the box running hil-controller),
    # NOT an SSH transport to the device's host.
    reg._hosts = [{"id": "rpi-displays", "addr": "192.168.1.234", "ssh_user": "pi"}]
    device = {
        "id": "mcu-revtft",
        "host_id": "rpi-displays",
        "hub_port_path": "1-1.1.1.4",
    }
    request = {
        "payload": {"kind": "git-source", "source": {"repo": "r", "ref": "m", "setup": []}},
        "params": {"exec": {"build_host": "controller", "flash_mode": "usbip", "pio_env": "e"}},
    }
    adapter = reg.make_adapter(reg._hosts[0], device, request, "job-1")
    assert isinstance(adapter, ArduinoWsExecAdapter)
    # controller == local; usbip server (dut-host) is rpi-displays
    assert isinstance(adapter.controller_transport, LocalTransport)
    assert adapter.server_addr == "192.168.1.234"


def test_make_adapter_routes_plain_git_source_to_git_deploy():
    from hil_controller.adapters.git_deploy import GitDeployAdapter

    reg = RealHostRegistry(topology_file="", db_path="db")
    reg._hosts = [{"id": "rpi", "addr": "10.0.0.5", "ssh_user": "pi"}]
    device = {"id": "sbc-1", "host_id": "rpi"}
    request = {
        "payload": {"kind": "git-source", "source": {"repo": "r", "ref": "m"}},
        "params": {},
    }
    adapter = reg.make_adapter(reg._hosts[0], device, request, "job-2")
    assert isinstance(adapter, GitDeployAdapter)


@pytest.mark.asyncio
async def test_get_adapter_overlays_db_hardware_fields_onto_topology_device(tmp_path):
    """get_adapter must overlay the DB's authoritative hardware-addressing fields
    (serial_port / hub / solenoid / build_target) onto the matched topology
    device — the topology file is only a seed. Regression: the CI HIL job flashed
    to an empty ``--port ''`` because the matched topology entry lacked the
    serial_port that lived only in the DB."""
    from hil_controller.adapters.firmware_bench import FirmwareBenchAdapter
    from hil_controller.db.connection import get_db, init_db, insert_job

    db_file = str(tmp_path / "reg.db")
    await init_db(db_file)
    async with get_db(db_file) as db:
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, status, serial_port, "
            "hub_host_id, hub_port_path, solenoid_channel, build_target) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "mcu-x",
                "rpi-displays",
                "microcontroller",
                "available",
                "/dev/serial/by-path/the-port",
                "rpi-displays",
                "1-1.2",
                4,
                "qtpy_esp32s3_n4r2",
            ),
        )
        await db.commit()
        await insert_job(
            db,
            job_id="j-enrich",
            request_json={
                "script": "firmware-bench",
                "target": {"device": {"id": "mcu-x"}, "pool": "public"},
                "params": {"firmware": {"path": "/tmp/fw.bin"}, "stages": [{"type": "flash"}]},
                "payload": {},
            },
            secrets_profile="bench-protomq",
            exclusive_host=False,
        )

    reg = RealHostRegistry(topology_file="", db_path=db_file)
    reg._hosts = [{"id": "rpi-displays", "addr": "192.168.1.234", "ssh_user": "pi"}]
    # Topology device deliberately LACKS the hardware fields (mirrors production).
    reg._devices = [
        {
            "id": "mcu-x",
            "host_id": "rpi-displays",
            "status": "available",
            "pool": "public",
            "kind": "microcontroller",
            "capabilities": [],
        }
    ]

    adapter = await reg.get_adapter("j-enrich")
    assert isinstance(adapter, FirmwareBenchAdapter)
    assert adapter.device["serial_port"] == "/dev/serial/by-path/the-port"
    assert adapter.device["hub_port_path"] == "1-1.2"
    assert adapter.device["solenoid_channel"] == 4
    assert adapter.device["build_target"] == "qtpy_esp32s3_n4r2"


@pytest.mark.asyncio
async def test_get_adapter_rejects_job_when_device_db_unavailable(tmp_path):
    """A device the DB has flagged unavailable (e.g. a wedged host pending
    reboot) must NOT get a real adapter — the job is rejected with the reason,
    so new jobs are gated off a wedged host."""
    from hil_controller.db.connection import get_db, init_db, insert_job
    from hil_controller.hosts.registry import _UnmatchedAdapter

    db_file = str(tmp_path / "reg2.db")
    await init_db(db_file)
    async with get_db(db_file) as db:
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, status, unavailable_kind, unavailable_reason) "
            "VALUES ('mcu-x','rpi-displays','microcontroller','unavailable','temporary',"
            "'host USB stack wedged — rpi-displays reboot required')"
        )
        await db.commit()
        await insert_job(
            db,
            job_id="j-gated",
            request_json={
                "script": "firmware-bench",
                "target": {"device": {"id": "mcu-x"}, "pool": "public"},
                "params": {"firmware": {"path": "/tmp/fw.bin"}},
                "payload": {},
            },
            secrets_profile="bench-protomq",
            exclusive_host=False,
        )

    reg = RealHostRegistry(topology_file="", db_path=db_file)
    reg._hosts = [{"id": "rpi-displays", "addr": "192.168.1.234", "ssh_user": "pi"}]
    reg._devices = [
        {
            "id": "mcu-x",
            "host_id": "rpi-displays",
            "status": "available",
            "pool": "public",
            "kind": "microcontroller",
            "capabilities": [],
        }
    ]

    adapter = await reg.get_adapter("j-gated")
    assert isinstance(adapter, _UnmatchedAdapter)
    with pytest.raises(RuntimeError, match="wedged"):
        await adapter.acquire()
