"""BossacFlasher tests (SAM / SAMD51) — no hardware, transport is a fake.

Locks the bossac argv shape (the SAM-BA flags + the offset safety net), the
output parsers, and the 1200-baud bootloader-entry behaviour.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hil_controller.adapters.flashers import Artifact, BossacFlasher
from hil_controller.adapters.flashers.bossac import (
    SAMD21_APP_OFFSET,
    SAMD51_APP_OFFSET,
    parse_bossac_device,
    parse_bossac_written,
)
from hil_controller.hosts.base import ExecResult

# Representative bossac output fragments.
_INFO_OUT = """Atmel SMART device 0x60060006 found
Device       : ATSAMD51J20A
Chip ID      : 60060006
Version      : v2.0 [Arduino:XYZ] Jul 11 2019 ...
Address      : 0x4000
Pages        : 1024
Page Size    : 512 bytes
Total Size   : 512KB
Planes       : 1
Lock Regions : 32
Locked       : none
Security     : false
BOD          : true
BOR          : true
"""

_FLASH_OUT = """Erase flash
Write 196608 bytes to flash (384 pages)
[==============================] 100% (384/384 pages)
Verify 196608 bytes of flash
[==============================] 100% (384/384 pages)
Verify successful
Set boot flash true
CPU reset.
"""


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


class _RoutingTransport:
    """Fake transport that routes exec() by argv and records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        # by-path resolves to /dev/ttyACM0; bossac -i reports a SAM device;
        # everything else (write, stty, reset) succeeds quietly.
        self.info_result = _result(0, stdout=_INFO_OUT)
        self.flash_result = _result(0, stdout=_FLASH_OUT)

    async def exec(self, argv, *, cwd=None, env=None):  # noqa: ANN001
        self.calls.append(list(argv))
        # strip a leading sudo for matching
        a = argv[1:] if argv and argv[0] == "sudo" else argv
        if a[:2] == ["readlink", "-f"]:
            return _result(0, stdout="/dev/ttyACM0\n")
        if a and a[0] == "bossac":
            if "--info" in a:
                return self.info_result
            if "--write" in a:
                return self.flash_result
            return _result(0)
        return _result(0)  # stty touch, etc.

    def last_bossac(self, *, needle: str | None = None) -> list[str]:
        for argv in reversed(self.calls):
            a = argv[1:] if argv and argv[0] == "sudo" else argv
            if a and a[0] == "bossac" and (needle is None or needle in a):
                return a
        raise AssertionError(f"no bossac call matching {needle!r} in {self.calls}")


# --------------------------------------------------------------------------- #
# Parsers                                                                     #
# --------------------------------------------------------------------------- #


def test_parse_bossac_device() -> None:
    assert parse_bossac_device(_INFO_OUT) == "ATSAMD51J20A"
    assert parse_bossac_device("no device here") is None


def test_parse_bossac_written_sums_write_lines() -> None:
    assert parse_bossac_written(_FLASH_OUT) == 196608
    assert parse_bossac_written("nothing written") == 0


def test_offset_constants() -> None:
    assert SAMD51_APP_OFFSET == 0x4000
    assert SAMD21_APP_OFFSET == 0x2000


# --------------------------------------------------------------------------- #
# Port resolution                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bare_port_resolves_by_path_symlink() -> None:
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/serial/by-path/platform-x-usb-0:1.1.4:1.0")
    assert await f._bare_port() == "ttyACM0"


# --------------------------------------------------------------------------- #
# flash() argv + offset safety net                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_flash_argv_has_sam_ba_flags_and_app_offset() -> None:
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0")
    res = await f.flash(Artifact(path="/tmp/fw.bin", offset=0x4000))
    argv = tp.last_bossac(needle="--write")
    for flag in ("--erase", "--write", "--verify", "--boot", "--reset"):
        assert flag in argv, f"missing {flag} in {argv}"
    assert "--offset=0x4000" in argv
    assert argv[-1] == "/tmp/fw.bin"
    assert argv[1:3] == ["--port", "ttyACM0"]
    assert res.bytes_written == 196608


@pytest.mark.asyncio
async def test_flash_coerces_zero_offset_to_app_offset() -> None:
    """An ESP-shaped offset:0x0 must NOT overwrite the SAM bootloader."""
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0", app_offset=0x4000)
    await f.flash(Artifact(path="/tmp/fw.bin", offset=0x0))
    argv = tp.last_bossac(needle="--write")
    assert "--offset=0x4000" in argv
    assert "--offset=0x0" not in argv


@pytest.mark.asyncio
async def test_flash_respects_samd21_app_offset() -> None:
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0", app_offset=SAMD21_APP_OFFSET)
    await f.flash(Artifact(path="/tmp/fw.bin"))  # offset None → app_offset
    assert "--offset=0x2000" in tp.last_bossac(needle="--write")


# --------------------------------------------------------------------------- #
# probe / erase / reset                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_probe_returns_family() -> None:
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0")
    info = await f.probe()
    assert info.family == "ATSAMD51J20A"


@pytest.mark.asyncio
async def test_erase_targets_app_offset() -> None:
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0")
    await f.erase()
    argv = tp.last_bossac(needle="--erase")
    assert "--erase" in argv
    assert "--offset=0x4000" in argv


@pytest.mark.asyncio
async def test_reset_bootloader_issues_1200_touch() -> None:
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0", settle_s=0)
    await f.reset(into="bootloader")
    assert any(c[:1] == ["stty"] and "1200" in c for c in tp.calls), tp.calls


@pytest.mark.asyncio
async def test_reset_application_sets_boot_and_resets() -> None:
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0")
    await f.reset(into="application")
    argv = tp.last_bossac(needle="--reset")
    assert "--boot" in argv and "--reset" in argv


# --------------------------------------------------------------------------- #
# Bootloader entry                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_is_in_bootloader_true_when_info_succeeds() -> None:
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0")
    assert await f.is_in_bootloader() is True


@pytest.mark.asyncio
async def test_is_in_bootloader_false_when_info_fails() -> None:
    tp = _RoutingTransport()
    tp.info_result = _result(1, stderr="No device found on ttyACM0")
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0")
    assert await f.is_in_bootloader() is False


@pytest.mark.asyncio
async def test_enter_bootloader_returns_immediately_when_already_in_sam_ba() -> None:
    tp = _RoutingTransport()
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0", settle_s=0)
    await f.enter_bootloader(attempts=3)
    # No stty touch needed since info succeeds on the first probe.
    assert not any(c[:1] == ["stty"] for c in tp.calls)


@pytest.mark.asyncio
async def test_enter_bootloader_touches_then_succeeds() -> None:
    tp = _RoutingTransport()
    # Fail the first info probe (app mode), succeed after the touch.
    seq = [_result(1, stderr="No device found"), _result(0, stdout=_INFO_OUT)]

    async def exec_(argv, *, cwd=None, env=None):  # noqa: ANN001
        tp.calls.append(list(argv))
        a = argv[1:] if argv and argv[0] == "sudo" else argv
        if a[:2] == ["readlink", "-f"]:
            return _result(0, stdout="/dev/ttyACM0\n")
        if a and a[0] == "bossac" and "--info" in a:
            return seq.pop(0) if seq else _result(0, stdout=_INFO_OUT)
        return _result(0)

    tp.exec = exec_  # type: ignore[assignment]
    f = BossacFlasher(transport=tp, port="/dev/ttyACM0", settle_s=0)
    await f.enter_bootloader(attempts=3)
    assert any(c[:1] == ["stty"] and "1200" in c for c in tp.calls), tp.calls
