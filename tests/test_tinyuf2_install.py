"""TinyUf2Installer orchestration tests (M3.5).

The installer composes TinyUf2Fetcher + UsbipBridge + EsptoolFlasher.
We stub the fetcher with a fake that writes a real combined.bin to a
tmp_path, then route bind/attach/exec calls through AsyncMock transports
so the command sequence is fully observable without any network or
hardware.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.tinyuf2_fetcher import TinyUf2Fetched
from hil_controller.adapters.tinyuf2_install import (
    TinyUf2Installer,
    TinyUf2InstallError,
)
from hil_controller.hosts.base import ExecResult

# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


class _FakeFetcher:
    """TinyUf2Fetcher stand-in: records its fetch() args, returns a real path."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[dict] = []

    async def fetch(
        self, *, board_name: str, tag: str = "latest", fallback_board: str | None = None
    ) -> TinyUf2Fetched:
        self.calls.append({"board_name": board_name, "tag": tag, "fallback_board": fallback_board})
        return TinyUf2Fetched(
            path=self.path,
            tag="0.22.0",
            asset_name=f"tinyuf2-{board_name}-0.22.0.zip",
            digest_sha256="deadbeef" * 8,
            raw_size=self.path.stat().st_size,
        )


def _make_bridge_aware_transports() -> tuple[AsyncMock, AsyncMock]:
    """Return (controller_tp, dut_tp) wired for a happy-path usbip attach."""
    controller = AsyncMock()
    dut = AsyncMock()

    # First `ls /dev/ttyACM*` returns empty; after attach it returns a new port.
    ls_count = {"n": 0}

    async def controller_exec(argv: list[str], **kw: object) -> MagicMock:
        if argv[0] == "bash" and "ttyACM" in argv[-1]:
            ls_count["n"] += 1
            return _result(0, stdout="" if ls_count["n"] <= 1 else "/dev/ttyACM0\n")
        if argv[:3] == ["sudo", "usbip", "port"]:
            return _result(
                0,
                stdout=(
                    "Port 00: <Port in Use>\n       1-1 -> usbip://192.168.1.234:3240/1-1.1.1.4\n"
                ),
            )
        # esptool erase / write_flash — synthesise plausible stdout
        if any("erase_flash" in a for a in argv):
            return _result(0, stdout="Chip erase completed successfully in 8.5 seconds.")
        if any("write_flash" in a for a in argv):
            return _result(
                0,
                stdout=(
                    "Wrote 1234567 bytes (compressed 567890 bytes) at 0x00000000 "
                    "in 12.3 seconds (effective 800.0 kbit/s)\n"
                ),
            )
        return _result(0)

    controller.exec = AsyncMock(side_effect=controller_exec)
    dut.exec = AsyncMock(return_value=_result(0))
    return controller, dut


def _all_argvs(mock_transport: AsyncMock) -> list[list[str]]:
    return [c.args[0] for c in mock_transport.exec.call_args_list]


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_install_runs_fetch_then_bind_attach_erase_flash_then_teardown(
    tmp_path: Path,
) -> None:
    combined = tmp_path / "combined.bin"
    combined.write_bytes(b"\x55" * 1024)

    controller, dut = _make_bridge_aware_transports()
    installer = TinyUf2Installer(
        controller_transport=controller,
        dut_transport=dut,
        server_addr="192.168.1.234",
        busid="1-1.1.1.4",
        board_name="feather_esp32s3_reverse_tft",
        fetcher=_FakeFetcher(combined),
        esptool_chip="esp32s3",
        settle_s=0,
    )

    result = await installer.install(tag="0.22.0", fallback_board="feather_esp32s3")

    # Result carries fetcher + flash data for the UI
    assert result.board_name == "feather_esp32s3_reverse_tft"
    assert result.tag == "0.22.0"
    assert result.asset_name == "tinyuf2-feather_esp32s3_reverse_tft-0.22.0.zip"
    assert result.serial_port == "/dev/ttyACM0"
    assert result.bytes_written == 1234567

    # Bind ran on the hub host
    dut_argvs = _all_argvs(dut)
    assert ["sudo", "usbip", "bind", "-b", "1-1.1.1.4"] in dut_argvs
    assert ["sudo", "usbip", "unbind", "-b", "1-1.1.1.4"] in dut_argvs

    # Attach ran on the controller
    controller_argvs = _all_argvs(controller)
    assert any(a[:3] == ["sudo", "usbip", "attach"] for a in controller_argvs)
    assert any(a[:4] == ["sudo", "usbip", "detach", "-p"] for a in controller_argvs)

    # esptool erase + write_flash both ran against /dev/ttyACM0 with esp32s3
    esptool_argvs = [a for a in controller_argvs if "esptool" in a]
    assert any("erase_flash" in a for a in esptool_argvs)
    write_argvs = [a for a in esptool_argvs if "write_flash" in a]
    assert write_argvs, "expected a write_flash invocation"
    flash_argv = write_argvs[0]
    assert "/dev/ttyACM0" in flash_argv
    assert "esp32s3" in flash_argv
    assert "0x0" in flash_argv
    assert str(combined) in flash_argv


@pytest.mark.asyncio
async def test_install_passes_fetcher_kwargs_through(tmp_path: Path) -> None:
    combined = tmp_path / "combined.bin"
    combined.write_bytes(b"\x00" * 16)

    controller, dut = _make_bridge_aware_transports()
    fake = _FakeFetcher(combined)
    installer = TinyUf2Installer(
        controller_transport=controller,
        dut_transport=dut,
        server_addr="192.168.1.234",
        busid="1-1.1.1.4",
        board_name="metro_esp32s2",
        fetcher=fake,
        settle_s=0,
    )
    await installer.install(tag="0.21.0", fallback_board="generic_esp32s2")

    assert fake.calls == [
        {"board_name": "metro_esp32s2", "tag": "0.21.0", "fallback_board": "generic_esp32s2"}
    ]


# --------------------------------------------------------------------------- #
# Failure paths                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_install_raises_when_no_serial_port_appeared(tmp_path: Path) -> None:
    combined = tmp_path / "combined.bin"
    combined.write_bytes(b"\x00" * 8)

    controller = AsyncMock()
    dut = AsyncMock()

    async def controller_exec(argv: list[str], **kw: object) -> MagicMock:
        # ls always empty -> the bridge sees no new port
        if argv[0] == "bash" and "ttyACM" in argv[-1]:
            return _result(0, stdout="")
        if argv[:3] == ["sudo", "usbip", "port"]:
            return _result(0, stdout="")
        return _result(0)

    controller.exec = AsyncMock(side_effect=controller_exec)
    dut.exec = AsyncMock(return_value=_result(0))

    installer = TinyUf2Installer(
        controller_transport=controller,
        dut_transport=dut,
        server_addr="192.168.1.234",
        busid="1-1.1.1.4",
        board_name="feather_esp32s3",
        fetcher=_FakeFetcher(combined),
        settle_s=0,
    )
    with pytest.raises(TinyUf2InstallError, match="no new serial port"):
        await installer.install()

    # Teardown still ran (unbind on the dut host) even though the install failed.
    assert ["sudo", "usbip", "unbind", "-b", "1-1.1.1.4"] in _all_argvs(dut)


@pytest.mark.asyncio
async def test_install_propagates_esptool_failure_and_still_unbinds(
    tmp_path: Path,
) -> None:
    combined = tmp_path / "combined.bin"
    combined.write_bytes(b"\x00" * 8)

    controller, dut = _make_bridge_aware_transports()
    # Override the controller exec so erase_flash fails.
    original = controller.exec.side_effect

    async def fail_on_erase(argv: list[str], **kw: object) -> MagicMock:
        if any("erase_flash" in a for a in argv):
            return _result(1, stderr="MD5 mismatch on chip")
        return await original(argv, **kw)

    controller.exec = AsyncMock(side_effect=fail_on_erase)

    installer = TinyUf2Installer(
        controller_transport=controller,
        dut_transport=dut,
        server_addr="192.168.1.234",
        busid="1-1.1.1.4",
        board_name="feather_esp32s3",
        fetcher=_FakeFetcher(combined),
        settle_s=0,
    )

    from hil_controller.adapters.flashers import FlasherToolFailed

    with pytest.raises(FlasherToolFailed, match="esptool"):
        await installer.install()

    # Even though erase blew up, the bridge teardown unbinds the busid.
    assert ["sudo", "usbip", "unbind", "-b", "1-1.1.1.4"] in _all_argvs(dut)
