"""Phase 1 tests: usbip bridge helper (bind/attach/detach/unbind + port discovery).

All command sequences are asserted against a fake transport — no hardware,
no real usbip. The bridge brokers a USB device from a *server* host (the host
physically holding the device, e.g. rpi-displays) onto a *client* host (where
flashing runs, e.g. the controller Tachyon).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.usbip_bridge import (
    UsbipBridge,
    diff_serial_ports,
    parse_usbip_port,
)
from hil_controller.hosts.base import ExecResult


def _result(exit_status=0, stdout="", stderr=""):
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


def _transport(default=None):
    t = AsyncMock()
    t.exec = AsyncMock(return_value=default if default is not None else _result(0))
    return t


def _argvs(mock_transport):
    """Flatten the argv lists passed to transport.exec."""
    return [c.args[0] for c in mock_transport.exec.call_args_list]


# --------------------------------------------------------------------------- #
# Pure parsers                                                                  #
# --------------------------------------------------------------------------- #


def test_diff_serial_ports_returns_the_one_new_port():
    before = ["/dev/ttyACM0"]
    after = ["/dev/ttyACM0", "/dev/ttyACM1"]
    assert diff_serial_ports(before, after) == "/dev/ttyACM1"


def test_diff_serial_ports_none_when_nothing_appeared():
    assert diff_serial_ports(["/dev/ttyACM0"], ["/dev/ttyACM0"]) is None


def test_parse_usbip_port_finds_port_for_busid():
    text = (
        "Imported USB devices\n"
        "====================\n"
        "Port 00: <Port in Use> at High Speed(480Mbps)\n"
        "       Adafruit Industries Feather ESP32-S3\n"
        "       3-1 -> usbip://192.168.1.234:3240/1-1.1.1.4\n"
    )
    assert parse_usbip_port(text, "1-1.1.1.4") == 0


def test_parse_usbip_port_none_when_busid_absent():
    text = (
        "Imported USB devices\n"
        "Port 02: <Port in Use> at High Speed(480Mbps)\n"
        "       7-1 -> usbip://192.168.1.234:3240/9-9.9\n"
    )
    assert parse_usbip_port(text, "1-1.1.1.4") is None


# --------------------------------------------------------------------------- #
# Command sequences                                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def bridge_pair():
    server = _transport()
    client = _transport()
    bridge = UsbipBridge(
        server_tp=server,
        client_tp=client,
        server_addr="192.168.1.234",
        busid="1-1.1.1.4",
        settle_s=0,
    )
    return bridge, server, client


@pytest.mark.asyncio
async def test_bind_runs_usbip_bind_on_server(bridge_pair):
    bridge, server, _ = bridge_pair
    await bridge.bind()
    assert ["sudo", "usbip", "bind", "-b", "1-1.1.1.4"] in _argvs(server)


@pytest.mark.asyncio
async def test_unbind_runs_usbip_unbind_on_server(bridge_pair):
    bridge, server, _ = bridge_pair
    await bridge.unbind()
    assert ["sudo", "usbip", "unbind", "-b", "1-1.1.1.4"] in _argvs(server)


@pytest.mark.asyncio
async def test_ensure_vhci_modprobes_on_client(bridge_pair):
    bridge, _, client = bridge_pair
    await bridge.ensure_vhci()
    assert ["sudo", "modprobe", "vhci-hcd"] in _argvs(client)


@pytest.mark.asyncio
async def test_attach_runs_usbip_attach_on_client(bridge_pair):
    bridge, _, client = bridge_pair
    await bridge.attach()
    assert [
        "sudo",
        "usbip",
        "attach",
        "-r",
        "192.168.1.234",
        "-b",
        "1-1.1.1.4",
    ] in _argvs(client)


@pytest.mark.asyncio
async def test_bind_raises_on_nonzero_exit(bridge_pair):
    bridge, server, _ = bridge_pair
    server.exec.return_value = _result(1, stderr="bind error")
    with pytest.raises(RuntimeError, match="usbip bind"):
        await bridge.bind()


@pytest.mark.asyncio
async def test_detach_parses_port_then_detaches(bridge_pair):
    bridge, _, client = bridge_pair

    async def fake_exec(argv, **kw):
        if argv[:3] == ["sudo", "usbip", "port"]:
            return _result(
                0,
                stdout=(
                    "Port 03: <Port in Use>\n       3-1 -> usbip://192.168.1.234:3240/1-1.1.1.4\n"
                ),
            )
        return _result(0)

    client.exec.side_effect = fake_exec
    await bridge.detach()
    assert ["sudo", "usbip", "detach", "-p", "03"] in _argvs(client)


@pytest.mark.asyncio
async def test_attached_context_binds_attaches_then_tears_down(bridge_pair):
    bridge, server, client = bridge_pair

    serial_seen = []

    async def client_exec(argv, **kw):
        # ls before -> empty; ls after -> one port appeared
        if argv[0] == "bash" and "ttyACM" in argv[-1]:
            n = sum(1 for c in client.exec.call_args_list if c.args[0][0] == "bash")
            return _result(0, stdout="" if n <= 1 else "/dev/ttyACM0\n")
        if argv[:3] == ["sudo", "usbip", "port"]:
            return _result(0, stdout="Port 00:\n   x -> usbip://192.168.1.234:3240/1-1.1.1.4\n")
        return _result(0)

    client.exec.side_effect = client_exec

    async with bridge.attached() as port:
        serial_seen.append(port)

    server_cmds = _argvs(server)
    client_cmds = _argvs(client)
    # bound + unbound on the server
    assert ["sudo", "usbip", "bind", "-b", "1-1.1.1.4"] in server_cmds
    assert ["sudo", "usbip", "unbind", "-b", "1-1.1.1.4"] in server_cmds
    # attached + detached on the client
    assert any(c[:3] == ["sudo", "usbip", "attach"] for c in client_cmds)
    assert any(c[:4] == ["sudo", "usbip", "detach", "-p"] for c in client_cmds)
    # discovered the freshly-enumerated serial port
    assert serial_seen == ["/dev/ttyACM0"]


@pytest.mark.asyncio
async def test_attached_tears_down_even_on_body_exception(bridge_pair):
    bridge, server, client = bridge_pair

    async def client_exec(argv, **kw):
        if argv[:3] == ["sudo", "usbip", "port"]:
            return _result(0, stdout="Port 00:\n  x -> usbip://h/1-1.1.1.4\n")
        return _result(0)

    client.exec.side_effect = client_exec

    with pytest.raises(ValueError):
        async with bridge.attached():
            raise ValueError("boom")

    assert ["sudo", "usbip", "unbind", "-b", "1-1.1.1.4"] in _argvs(server)
    assert any(c[:4] == ["sudo", "usbip", "detach", "-p"] for c in _argvs(client))
