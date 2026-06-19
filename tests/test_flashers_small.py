"""Tests for the two small M3.5 flashers: PioUploadFlasher + NoOpFlasher.

Both are intentionally tiny — they exist so the rest of the system has a
uniform ``flasher.flash(artifact)`` call site whether the artifact is a
PlatformIO project, a raw `.bin`, or "this device doesn't need flashing."
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.flashers import (
    Artifact,
    FlasherProtocol,
    FlasherToolFailed,
    FlasherUnsupported,
)
from hil_controller.adapters.flashers.noop import NoOpFlasher
from hil_controller.adapters.flashers.pio_upload import PioUploadFlasher
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
# PioUploadFlasher                                                            #
# --------------------------------------------------------------------------- #


def test_pio_upload_satisfies_flasher_protocol() -> None:
    flasher = PioUploadFlasher(
        transport=_transport(),
        port="/dev/ttyACM0",
        workspace_dir="/tmp/proj",
        pio_env="esp32s3",
    )
    assert isinstance(flasher, FlasherProtocol)


@pytest.mark.asyncio
async def test_pio_upload_flash_runs_pio_in_workspace_with_env_and_port() -> None:
    tp = _transport(_result(0, stdout="Build OK"))
    flasher = PioUploadFlasher(
        transport=tp,
        port="/dev/ttyACM1",
        workspace_dir="/tmp/proj",
        pio_env="esp32s3",
    )
    result = await flasher.flash(Artifact(path="/tmp/firmware.bin"))

    # cwd is the workspace
    assert tp.exec.call_args.kwargs["cwd"] == "/tmp/proj"
    # argv is `bash -c '... pio run -e <env> --target upload --upload-port <port>'`
    argv = _argvs(tp)[0]
    assert argv[:2] == ["bash", "-c"]
    shell_cmd = argv[2]
    assert "pio run" in shell_cmd
    assert "-e esp32s3" in shell_cmd
    assert "--target upload" in shell_cmd
    assert "--upload-port /dev/ttyACM1" in shell_cmd
    # FlashResult is populated; bytes_written is 0 (pio doesn't report it).
    assert result.bytes_written == 0
    assert result.elapsed_s >= 0.0
    assert result.raw_stdout == "Build OK"


@pytest.mark.asyncio
async def test_pio_upload_flash_raises_on_failure() -> None:
    tp = _transport(_result(1, stderr="link error"))
    flasher = PioUploadFlasher(
        transport=tp, port="/dev/ttyACM0", workspace_dir="/tmp/proj", pio_env="x"
    )
    with pytest.raises(FlasherToolFailed, match="pio-upload"):
        await flasher.flash(Artifact(path="/tmp/x.bin"))


@pytest.mark.asyncio
async def test_pio_upload_erase_runs_pio_target_erase() -> None:
    tp = _transport()
    flasher = PioUploadFlasher(
        transport=tp,
        port="/dev/ttyACM0",
        workspace_dir="/tmp/proj",
        pio_env="esp32s3",
    )
    await flasher.erase()
    shell_cmd = _argvs(tp)[0][2]
    assert "--target erase" in shell_cmd


@pytest.mark.asyncio
async def test_pio_upload_probe_is_unsupported() -> None:
    flasher = PioUploadFlasher(
        transport=_transport(),
        port="/dev/ttyACM0",
        workspace_dir="/tmp/proj",
        pio_env="esp32s3",
    )
    with pytest.raises(FlasherUnsupported):
        await flasher.probe()


@pytest.mark.asyncio
async def test_pio_upload_reset_is_unsupported() -> None:
    flasher = PioUploadFlasher(
        transport=_transport(),
        port="/dev/ttyACM0",
        workspace_dir="/tmp/proj",
        pio_env="esp32s3",
    )
    with pytest.raises(FlasherUnsupported, match="SolenoidHubAdapter"):
        await flasher.reset(into="bootloader")


# --------------------------------------------------------------------------- #
# NoOpFlasher                                                                 #
# --------------------------------------------------------------------------- #


def test_noop_satisfies_flasher_protocol() -> None:
    assert isinstance(NoOpFlasher(), FlasherProtocol)


@pytest.mark.asyncio
async def test_noop_probe_returns_chip_info_with_configured_family() -> None:
    flasher = NoOpFlasher(family="pre-provisioned-sbc")
    info = await flasher.probe()
    assert info.family == "pre-provisioned-sbc"


@pytest.mark.asyncio
async def test_noop_erase_returns_without_touching_anything() -> None:
    tp = _transport()
    flasher = NoOpFlasher(transport=tp, port="/dev/null")
    await flasher.erase()
    assert tp.exec.await_count == 0


@pytest.mark.asyncio
async def test_noop_flash_returns_zero_byte_flash_result() -> None:
    flasher = NoOpFlasher()
    result = await flasher.flash(Artifact(path="/tmp/anything.bin"))
    assert result.bytes_written == 0
    assert result.elapsed_s == 0.0


@pytest.mark.asyncio
async def test_noop_reset_is_a_noop_for_both_targets() -> None:
    flasher = NoOpFlasher()
    await flasher.reset(into="bootloader")
    await flasher.reset(into="application")
