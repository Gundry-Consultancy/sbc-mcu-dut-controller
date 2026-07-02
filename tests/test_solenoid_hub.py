"""SolenoidHubAdapter tests (M3.5).

No hardware: AsyncMock transport. The adapter shells out to a CLI on
the bench host; tests assert the exact argv shape that hits the wire.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.solenoid_hub import (
    DEFAULT_CLI_PATH,
    SolenoidHubAdapter,
    SolenoidHubError,
    _channel_arg,
)
from hil_controller.hosts.base import ExecResult


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


def _transport(default: MagicMock | None = None) -> AsyncMock:
    t = AsyncMock()
    t.exec = AsyncMock(return_value=default if default is not None else _result(0))
    return t


def _argvs(mock_transport: AsyncMock) -> list[list[str]]:
    return [c.args[0] for c in mock_transport.exec.call_args_list]


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def test_channel_arg_accepts_valid_range() -> None:
    # Port A (0..7) = power-latch solenoids; port B (8..15) = BOOTSEL/aux
    # presses (B = A + 8), stored per-device as bootsel_channel.
    assert _channel_arg(0) == "0"
    assert _channel_arg(7) == "7"
    assert _channel_arg(8) == "8"
    assert _channel_arg(15) == "15"


def test_channel_arg_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="out of range"):
        _channel_arg(-1)
    with pytest.raises(ValueError, match="out of range"):
        _channel_arg(16)


def test_default_cli_path_is_opt_hil() -> None:
    assert DEFAULT_CLI_PATH == "/opt/hil/solenoid_hub_cli.py"


# --------------------------------------------------------------------------- #
# Verb argv shape                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_all_off_argv() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp)
    await hub.all_off()
    assert _argvs(tp) == [["python3", DEFAULT_CLI_PATH, "all_off"]]


@pytest.mark.asyncio
async def test_port_on_argv() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp)
    await hub.port_on(3)
    assert _argvs(tp) == [["python3", DEFAULT_CLI_PATH, "port_on", "3"]]


@pytest.mark.asyncio
async def test_port_off_argv_includes_off_duration() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp)
    await hub.port_off(5)  # default hold_s=1.0; latch double-press (presses=2)
    off = ["python3", DEFAULT_CLI_PATH, "port_off", "5", "--off-duration", "1.0"]
    assert _argvs(tp) == [off, off]


@pytest.mark.asyncio
async def test_port_off_custom_hold() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp)
    await hub.port_off(0, hold_s=0.3, presses=1)  # single press for argv-content focus
    assert _argvs(tp) == [["python3", DEFAULT_CLI_PATH, "port_off", "0", "--off-duration", "0.3"]]


@pytest.mark.asyncio
async def test_port_off_double_press_with_gap_and_post_off_on_last(monkeypatch) -> None:
    import asyncio

    gaps: list[float] = []

    async def fake_sleep(s):
        gaps.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp)
    await hub.port_off(4, hold_s=3.0, post_off_s=2.0)  # defaults: presses=2, gap=0.12
    argvs = _argvs(tp)
    assert len(argvs) == 2  # pressed twice for a reliable latch
    assert gaps == [0.12]  # one 120 ms gap between the presses
    assert "--post-off-s" not in argvs[0]  # depower settle only after the LAST press
    assert argvs[1][-2:] == ["--post-off-s", "2.0"]


@pytest.mark.asyncio
async def test_samd51_uf2_argv() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp)
    await hub.samd51_uf2(2)
    assert _argvs(tp) == [["python3", DEFAULT_CLI_PATH, "samd51_uf2", "2"]]


@pytest.mark.asyncio
async def test_sudo_prefix_when_sudo_true() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp, sudo=True)
    await hub.port_on(1)
    assert _argvs(tp)[0][0] == "sudo"
    assert _argvs(tp)[0][1:] == ["python3", DEFAULT_CLI_PATH, "port_on", "1"]


@pytest.mark.asyncio
async def test_custom_cli_path_and_python() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(
        transport=tp,
        cli_path="/home/pi/dev/solenoid.py",
        python="python3.11",
    )
    await hub.all_off()
    assert _argvs(tp) == [["python3.11", "/home/pi/dev/solenoid.py", "all_off"]]


# --------------------------------------------------------------------------- #
# Error wrapping                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_port_on_raises_solenoid_hub_error_on_nonzero_exit() -> None:
    tp = _transport(_result(2, stderr="I2C: no ack on 0x20"))
    hub = SolenoidHubAdapter(transport=tp)
    with pytest.raises(SolenoidHubError, match="port_on"):
        await hub.port_on(0)


@pytest.mark.asyncio
async def test_error_message_includes_stderr_summary() -> None:
    tp = _transport(_result(2, stderr="missing adafruit_mcp230xx"))
    hub = SolenoidHubAdapter(transport=tp)
    try:
        await hub.all_off()
    except SolenoidHubError as exc:
        assert "missing adafruit_mcp230xx" in str(exc)
    else:
        pytest.fail("expected SolenoidHubError")


@pytest.mark.asyncio
async def test_invalid_channel_raises_before_transport_call() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp)
    with pytest.raises(ValueError, match="out of range"):
        await hub.port_on(99)
    assert tp.exec.await_count == 0


# --------------------------------------------------------------------------- #
# power_cycle convenience                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_power_cycle_calls_port_off_then_port_on() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp)
    await hub.power_cycle(4)
    calls = _argvs(tp)
    # port_off is now a latch double-press (2x) then port_on once.
    assert [c[2] for c in calls] == ["port_off", "port_off", "port_on"]
    assert all(c[3] == "4" for c in calls)


@pytest.mark.asyncio
async def test_power_cycle_passes_off_s_through() -> None:
    tp = _transport()
    hub = SolenoidHubAdapter(transport=tp)
    await hub.power_cycle(2, off_s=0.3)
    off_argv = _argvs(tp)[0]
    assert "--off-duration" in off_argv
    assert off_argv[off_argv.index("--off-duration") + 1] == "0.3"
