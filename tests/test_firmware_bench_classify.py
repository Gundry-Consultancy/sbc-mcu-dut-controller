"""Pure-function tests for firmware-bench error classification.

Kept separate from test_firmware_bench.py (whose async fixtures hang the
whole-suite run — see docs/HANDOFF.md §4) so this stays fast and importable.
"""

from __future__ import annotations

import pytest

from hil_controller.adapters.firmware_bench import FirmwareBenchAdapter, _is_host_unreachable_error


@pytest.mark.parametrize(
    "msg",
    [
        "[Errno 113] No route to host",
        "OSError: [Errno 113] No route to host",
        "[Errno 111] Connection refused",
        "Network is unreachable",
        "ssh: connect to host rpi-displays port 22: Connection reset by peer",
        "SSH connection lost",
        "Could not connect to host",
    ],
)
def test_host_unreachable_markers_match(msg):
    assert _is_host_unreachable_error(Exception(msg)) is True


@pytest.mark.parametrize(
    "msg",
    [
        "esptool: A fatal error occurred: Failed to write to target RAM",
        "PIXELWRITE_VERDICT rebooted=false",
        "stage flash: verification mismatch",
        "device absent after full recovery",  # a wedge — handled separately, not 'unreachable'
        "operation timed out",  # bare timeout is NOT classified as unreachable (slow flash)
    ],
)
def test_non_connection_errors_do_not_match(msg):
    assert _is_host_unreachable_error(Exception(msg)) is False


# --- on-demand power model -------------------------------------------------


class _FakeHub:
    def __init__(self):
        self.ons: list[int] = []
        self.offs: list[int] = []

    async def port_on(self, ch):
        self.ons.append(int(ch))

    async def port_off(self, ch, **kw):
        self.offs.append(int(ch))


class _FakeTransport:
    def __init__(self, present=True):
        self.present = present

    async def exec(self, argv):
        r = type("R", (), {})()
        r.exit_status = 0 if self.present else 1
        r.stdout = ""
        return r


def _adapter(device):
    return FirmwareBenchAdapter(
        controller_transport=_FakeTransport(),
        dut_transport=_FakeTransport(present=True),
        hub_transport=_FakeTransport(),
        job_id="j1",
        device=device,
        params={},
    )


@pytest.fixture
def _fast_sleep(monkeypatch):
    async def _noop(_s):
        return None

    monkeypatch.setattr("hil_controller.adapters.firmware_bench.asyncio.sleep", _noop)


@pytest.mark.asyncio
async def test_power_on_dut_energises_channel(monkeypatch, _fast_sleep):
    a = _adapter({"solenoid_channel": 4, "serial_port": "/dev/serial/by-path/qtpy"})
    hub = _FakeHub()
    monkeypatch.setattr(a, "_solenoid_hub", lambda: hub)
    await a._power_on_dut()
    assert hub.ons == [4]  # energised just this DUT's channel


@pytest.mark.asyncio
async def test_power_off_dut_deenergises_channel(monkeypatch, _fast_sleep):
    a = _adapter({"solenoid_channel": 4})
    hub = _FakeHub()
    monkeypatch.setattr(a, "_solenoid_hub", lambda: hub)
    await a._power_off_dut()
    assert hub.offs == [4]


@pytest.mark.asyncio
async def test_power_ops_noop_without_channel(monkeypatch, _fast_sleep):
    a = _adapter({"serial_port": "/dev/x"})  # channel-less / pio DUT
    hub = _FakeHub()
    monkeypatch.setattr(a, "_solenoid_hub", lambda: hub)
    await a._power_on_dut()
    await a._power_off_dut()
    assert hub.ons == [] and hub.offs == []
