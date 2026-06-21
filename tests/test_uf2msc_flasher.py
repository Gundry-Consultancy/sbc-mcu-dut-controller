"""Uf2MscFlasher tests — no hardware, transport is a fake routing mock.

Locks the by-path MSC location, the mount→cp→sync→umount sequence, and the
1200-baud bootloader-entry behaviour.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hil_controller.adapters.flashers import Artifact, Uf2MscFlasher
from hil_controller.adapters.flashers.uf2_msc import usb_path_token
from hil_controller.hosts.base import ExecResult

PORT = "/dev/serial/by-path/platform-3f980000.usb-usb-0:1.1.4:1.0"
MSC_DEV = "/dev/sda"
INFO_UF2 = (
    "UF2 Bootloader v3.15.0 SFHWRO\nModel: PyPortal M4 Express\nBoard-ID: SAMD51J20A-PyPortal-v0\n"
)


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


class _RoutingTransport:
    """Fake transport routing exec() by argv; records calls. MSC present by default."""

    def __init__(self, *, msc_present: bool = True, hammer_succeeds: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.msc_present = msc_present  # whether _locate_msc finds the *BOOT drive
        self.hammer_succeeds = hammer_succeeds  # whether the 1200-touch hammer lands

    async def exec(self, argv, *, cwd=None, env=None):  # noqa: ANN001
        self.calls.append(list(argv))
        a = argv[1:] if argv and argv[0] == "sudo" else argv
        joined = " ".join(a)
        if a[:2] == ["bash", "-c"]:
            # The 1200-touch HAMMER (carries stty + 1200): exit 0 + "BOOT:" on a catch.
            if "stty" in joined and "1200" in joined:
                if self.hammer_succeeds:
                    return _result(0, stdout="BOOT:/dev/sda")
                return _result(7)
            # _locate_msc by-path scsi glob (no stty) → /dev/sda when "present".
            if "by-path" in joined and "-scsi-" in joined:
                return _result(0, stdout=(MSC_DEV + "\n") if self.msc_present else "")
            # _bootloader_tty: resolves the bootloader CDC by-path (":1.0", not a
            # scsi node) to a bare tty for bossac --erase.
            if "readlink -f" in joined and ":1.0" in joined:
                return _result(0, stdout="/dev/ttyACM0\n")
            if "INFO_UF2.TXT" in joined:
                return _result(0, stdout=INFO_UF2)
            if "stat -c %s" in joined:
                return _result(0, stdout="410224\n")
        return _result(0)

    def cmds(self) -> list[str]:
        return [" ".join(c[1:] if c and c[0] == "sudo" else c) for c in self.calls]

    def has_sudo(self, needle: str) -> bool:
        return any(c and c[0] == "sudo" and needle in " ".join(c) for c in self.calls)


def test_usb_path_token() -> None:
    assert usb_path_token(PORT) == "1.1.4"
    assert usb_path_token("/dev/ttyACM0") is None


@pytest.mark.asyncio
async def test_is_in_bootloader_true_when_msc_present() -> None:
    f = Uf2MscFlasher(transport=_RoutingTransport(msc_present=True), port=PORT)
    assert await f.is_in_bootloader() is True


@pytest.mark.asyncio
async def test_is_in_bootloader_false_when_absent() -> None:
    f = Uf2MscFlasher(transport=_RoutingTransport(msc_present=False), port=PORT)
    assert await f.is_in_bootloader() is False


@pytest.mark.asyncio
async def test_flash_mounts_copies_syncs_unmounts() -> None:
    tp = _RoutingTransport(msc_present=True)
    f = Uf2MscFlasher(transport=tp, port=PORT, mount_dir="/tmp/hil-uf2mnt")
    res = await f.flash(Artifact(path="/tmp/fw.uf2", kind="uf2"))
    cmds = tp.cmds()
    assert any(c == f"mount {MSC_DEV} /tmp/hil-uf2mnt" for c in cmds), cmds
    assert any("cp /tmp/fw.uf2 /tmp/hil-uf2mnt/ && sync" in c for c in cmds), cmds
    assert any(c == "umount /tmp/hil-uf2mnt" for c in cmds), cmds
    # mount/cp/umount run under sudo (root needed to mount).
    assert tp.has_sudo(f"mount {MSC_DEV}")
    assert res.bytes_written == 410224


@pytest.mark.asyncio
async def test_flash_raises_when_no_msc_and_cannot_enter() -> None:
    tp = _RoutingTransport(msc_present=False)
    f = Uf2MscFlasher(transport=tp, port=PORT, settle_s=0)
    with pytest.raises(Exception):  # FlasherError — drive never appears
        await f.flash(Artifact(path="/tmp/fw.uf2"))


@pytest.mark.asyncio
async def test_probe_reads_board_id() -> None:
    f = Uf2MscFlasher(transport=_RoutingTransport(msc_present=True), port=PORT)
    info = await f.probe()
    assert info.family == "SAMD51J20A-PyPortal-v0"


@pytest.mark.asyncio
async def test_reset_bootloader_issues_1200_touch() -> None:
    tp = _RoutingTransport()
    f = Uf2MscFlasher(transport=tp, port=PORT, settle_s=0)
    await f.reset(into="bootloader")
    assert any("stty" in c and "1200" in c for c in tp.calls), tp.calls


@pytest.mark.asyncio
async def test_erase_runs_bossac_on_bootloader_cdc() -> None:
    # In the bootloader (msc_present) → resolve the CDC tty and bossac --erase it.
    tp = _RoutingTransport(msc_present=True)
    f = Uf2MscFlasher(transport=tp, port=PORT)
    await f.erase()
    cmds = tp.cmds()
    assert any(c == "bossac --port ttyACM0 --erase --offset=0x4000" for c in cmds), cmds
    # The erase runs as root (the flasher forces sudo).
    assert tp.has_sudo("bossac --port ttyACM0 --erase")


@pytest.mark.asyncio
async def test_erase_raises_when_no_cdc_tty() -> None:
    # No CDC node resolves (readlink yields nothing) → erase raises rather than
    # silently leaving the (possibly stale) app in place.
    class _NoTty(_RoutingTransport):
        async def exec(self, argv, *, cwd=None, env=None):  # noqa: ANN001
            self.calls.append(list(argv))
            a = argv[1:] if argv and argv[0] == "sudo" else argv
            joined = " ".join(a)
            if a[:2] == ["bash", "-c"]:
                if "stty" in joined and "1200" in joined:
                    return _result(0, stdout="BOOT:/dev/sda")
                if "by-path" in joined and "-scsi-" in joined:
                    return _result(0, stdout=MSC_DEV + "\n")
                if "readlink -f" in joined and ":1.0" in joined:
                    return _result(1, stdout="")  # no CDC tty present
            return _result(0)

    f = Uf2MscFlasher(transport=_NoTty(msc_present=True), port=PORT)
    with pytest.raises(Exception):  # FlasherError — no CDC tty for bossac
        await f.erase()


def _is_hammer_script(argv: list[str]) -> bool:
    a = argv[1:] if argv and argv[0] == "sudo" else argv
    j = " ".join(a)
    # The 1200-touch hammer is the only bash -c carrying stty + 1200.
    return a[:2] == ["bash", "-c"] and "stty" in j and "1200" in j


@pytest.mark.asyncio
async def test_enter_bootloader_hammers_until_caught() -> None:
    # Not already in bootloader (locate finds nothing) → must hammer; hammer lands.
    tp = _RoutingTransport(msc_present=False, hammer_succeeds=True)
    f = Uf2MscFlasher(transport=tp, port=PORT, settle_s=0)
    await f.enter_bootloader(attempts=3, catch_s=1)
    assert any(_is_hammer_script(c) for c in tp.calls), tp.calls


@pytest.mark.asyncio
async def test_enter_bootloader_raises_when_hammer_never_lands() -> None:
    tp = _RoutingTransport(msc_present=False, hammer_succeeds=False)
    f = Uf2MscFlasher(transport=tp, port=PORT, settle_s=0)
    with pytest.raises(Exception):  # FlasherError — bootloader never reached
        await f.enter_bootloader(attempts=2, catch_s=1)
    # it actually tried the hammer (didn't just give up)
    assert sum(_is_hammer_script(c) for c in tp.calls) >= 2


@pytest.mark.asyncio
async def test_enter_bootloader_skips_hammer_when_already_in_bootloader() -> None:
    tp = _RoutingTransport(msc_present=True)  # *BOOT drive already present
    f = Uf2MscFlasher(transport=tp, port=PORT, settle_s=0)
    await f.enter_bootloader(attempts=3, catch_s=1)
    assert not any(_is_hammer_script(c) for c in tp.calls), tp.calls


@pytest.mark.asyncio
async def test_hammer_script_touches_and_detects_boot_label() -> None:
    """The hammer must both 1200-touch the CDC and detect the *BOOT drive."""
    tp = _RoutingTransport(msc_present=False, hammer_succeeds=True)
    f = Uf2MscFlasher(transport=tp, port=PORT)
    assert await f._catch_and_touch(wait_s=1) is True
    script = next(" ".join(c) for c in tp.calls if _is_hammer_script(c))
    assert "stty -F" in script and "1200" in script  # touches the CDC
    assert "-scsi-" in script and "BOOT" in script  # detects the bootloader drive
    assert "1.1.4:1.0" in script  # uses the device's USB by-path token
