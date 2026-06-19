"""FlasherProtocol + CliFlasher base tests (M3.5).

No hardware: every transport is an AsyncMock. The point of these tests
is to lock the contract every concrete flasher (esptool, picotool,
bossac, uf2-msc, ...) will share.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.flashers import (
    Artifact,
    ChipInfo,
    CliFlasher,
    FlasherProtocol,
    FlasherToolFailed,
    FlasherToolMissing,
    FlasherUnsupported,
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


# --------------------------------------------------------------------------- #
# Protocol shape                                                              #
# --------------------------------------------------------------------------- #


def test_cli_flasher_satisfies_flasher_protocol_at_runtime() -> None:
    flasher = CliFlasher(transport=_transport(), port="/dev/ttyACM0")
    assert isinstance(flasher, FlasherProtocol)


def test_artifact_defaults() -> None:
    a = Artifact(path="/tmp/firmware.bin")
    assert a.kind == "bin"
    assert a.offset is None
    assert a.label is None


def test_artifact_with_combined_bin_offset() -> None:
    a = Artifact(path="/tmp/combined.bin", kind="combined_bin", offset=0x0, label="tinyuf2")
    assert a.kind == "combined_bin"
    assert a.offset == 0
    assert a.label == "tinyuf2"


def test_chip_info_defaults() -> None:
    c = ChipInfo(family="ESP32-S3")
    assert c.mac is None
    assert c.flash_bytes is None
    assert c.unique_id is None
    assert c.raw == {}


# --------------------------------------------------------------------------- #
# _locate                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_locate_returns_resolved_path_and_caches() -> None:
    tp = _transport(_result(0, stdout="/usr/bin/esptool.py\n"))
    flasher = CliFlasher(transport=tp, port="/dev/ttyACM0")
    flasher.tool = "esptool.py"
    path = await flasher._locate()
    assert path == "/usr/bin/esptool.py"
    # Cached: second call still returns the same value.
    path2 = await flasher._locate()
    assert path2 == path
    # Second _locate did NOT re-invoke the transport.
    assert tp.exec.await_count == 1


@pytest.mark.asyncio
async def test_locate_raises_when_command_v_returns_empty() -> None:
    tp = _transport(_result(0, stdout=""))
    flasher = CliFlasher(transport=tp, port="/dev/ttyACM0")
    flasher.tool = "no-such-tool"
    with pytest.raises(FlasherToolMissing, match="no-such-tool"):
        await flasher._locate()


@pytest.mark.asyncio
async def test_locate_raises_when_subclass_forgot_to_set_tool() -> None:
    flasher = CliFlasher(transport=_transport(), port="/dev/ttyACM0")
    flasher.tool = ""  # explicit
    with pytest.raises(FlasherToolMissing, match="no `tool` set"):
        await flasher._locate()


# --------------------------------------------------------------------------- #
# _run                                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_returns_exec_result_on_success() -> None:
    tp = _transport(_result(0, stdout="ok"))
    flasher = CliFlasher(transport=tp, port="/dev/ttyACM0")
    result = await flasher._run(["esptool.py", "chip_id"])
    assert result.exit_status == 0
    assert result.stdout == "ok"


@pytest.mark.asyncio
async def test_run_wraps_nonzero_exit_in_flasher_tool_failed() -> None:
    tp = _transport(_result(1, stderr="bad happened"))
    flasher = CliFlasher(transport=tp, port="/dev/ttyACM0")
    flasher.name = "esptool"
    with pytest.raises(FlasherToolFailed) as exc_info:
        await flasher._run(["esptool.py", "chip_id"])
    err = exc_info.value
    assert err.tool == "esptool"
    assert err.exit_status == 1
    assert "bad happened" in err.stderr
    assert "esptool.py" in err.argv


@pytest.mark.asyncio
async def test_run_check_false_returns_failure_instead_of_raising() -> None:
    tp = _transport(_result(2, stderr="ignored"))
    flasher = CliFlasher(transport=_transport(), port="/dev/ttyACM0")
    flasher.transport = tp
    result = await flasher._run(["foo"], check=False)
    assert result.exit_status == 2


@pytest.mark.asyncio
async def test_run_prepends_sudo_when_sudo_true() -> None:
    tp = _transport()
    flasher = CliFlasher(transport=tp, port="/dev/ttyACM0", sudo=True)
    await flasher._run(["esptool.py", "chip_id"])
    argv = tp.exec.call_args.args[0]
    assert argv[0] == "sudo"
    assert argv[1:] == ["esptool.py", "chip_id"]


@pytest.mark.asyncio
async def test_run_no_sudo_by_default() -> None:
    tp = _transport()
    flasher = CliFlasher(transport=tp, port="/dev/ttyACM0")
    await flasher._run(["esptool.py", "chip_id"])
    argv = tp.exec.call_args.args[0]
    assert argv == ["esptool.py", "chip_id"]


@pytest.mark.asyncio
async def test_run_passes_cwd_and_env_through() -> None:
    tp = _transport()
    flasher = CliFlasher(transport=tp, port="/dev/ttyACM0")
    await flasher._run(["foo"], cwd="/tmp/x", env={"FOO": "bar"})
    kwargs = tp.exec.call_args.kwargs
    assert kwargs["cwd"] == "/tmp/x"
    assert kwargs["env"] == {"FOO": "bar"}


# --------------------------------------------------------------------------- #
# Default verbs                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_default_probe_raises_flasher_unsupported() -> None:
    flasher = CliFlasher(transport=_transport(), port="/dev/ttyACM0")
    with pytest.raises(FlasherUnsupported, match="probe"):
        await flasher.probe()


@pytest.mark.asyncio
async def test_default_erase_raises_flasher_unsupported() -> None:
    flasher = CliFlasher(transport=_transport(), port="/dev/ttyACM0")
    with pytest.raises(FlasherUnsupported, match="erase"):
        await flasher.erase()


@pytest.mark.asyncio
async def test_default_flash_raises_flasher_unsupported() -> None:
    flasher = CliFlasher(transport=_transport(), port="/dev/ttyACM0")
    with pytest.raises(FlasherUnsupported, match="flash"):
        await flasher.flash(Artifact(path="/tmp/x.bin"))


@pytest.mark.asyncio
async def test_default_reset_raises_flasher_unsupported() -> None:
    flasher = CliFlasher(transport=_transport(), port="/dev/ttyACM0")
    with pytest.raises(FlasherUnsupported, match="reset"):
        await flasher.reset(into="bootloader")
