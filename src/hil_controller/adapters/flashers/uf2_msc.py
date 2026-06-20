"""Uf2MscFlasher — flash a UF2 board by copying a .uf2 onto its bootloader drive.

The Adafruit-style UF2 bootloader (PyPortal M4/Titano, Feather/Metro M0/M4,
nRF52, RP2040, ...) exposes a USB mass-storage drive while in the bootloader;
dropping a ``.uf2`` onto it makes the bootloader write flash and reset into the
app. This is the **officially-supported** flash path and matches the WipperSnapper
release artifacts directly (`wippersnapper.<board>_tinyusb.<ver>.uf2`), so there
is no `.bin` conversion and no SAM-BA write applet to be incompatible with (which
is exactly where Debian's ``bossac`` fails on SAMD51 — see
:class:`BossacFlasher`).

**Bootloader entry** is the same 1200-baud double-tap as the ESP / SAM-BA paths
(``stty -F <port> 1200``): a running app reboots into the UF2 bootloader, which
re-enumerates and exposes the MSC drive.

**Locating the drive** is by the DUT's **USB by-path**, not by volume label — the
label is board-specific (the PyPortal Titano's bootloader labels it ``PORTALBOOT``,
not ``TITANOBOOT``), but the USB path is stable and unique to the port. We derive
the USB location token (e.g. ``1.1.4``) from the serial ``by-path`` and find the
matching ``/dev/disk/by-path/*<token>*-scsi*`` block device. Mount needs root, so
this flasher runs its mount/cp/umount under ``sudo`` (the bench user has
passwordless sudo).
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from typing import Any, Literal

from hil_controller.adapters.flashers.base import (
    Artifact,
    ChipInfo,
    CliFlasher,
    FlasherError,
    FlashResult,
)
from hil_controller.adapters.flashers.esptool import stty_1200_touch_argv

# "…/by-path/...usb-0:1.1.4:1.0" → "1.1.4" (the hub port chain of this DUT)
_USB_PATH_RE = re.compile(r"usb-\d+:([0-9.]+):")
_BOARD_ID_RE = re.compile(r"Board-ID:\s*(\S+)", re.IGNORECASE)


def usb_path_token(port: str) -> str | None:
    """Extract the USB hub-port token (e.g. ``1.1.4``) from a serial by-path."""
    m = _USB_PATH_RE.search(port or "")
    return m.group(1) if m else None


class Uf2MscFlasher(CliFlasher):
    """Flash a UF2 board by mounting its bootloader MSC drive and copying a .uf2.

    Subclasses :class:`CliFlasher` only to reuse ``_run`` (transport exec +
    transcript recording + sudo prefixing); there is no single CLI ``tool``.
    ``sudo`` defaults to True because mounting the FAT volume needs root.
    """

    name = "uf2-msc"
    tool = ""  # composite of mount/cp/sync/umount, not one CLI binary

    def __init__(
        self,
        *,
        transport: Any,
        port: str,
        sudo: bool = True,
        settle_s: float = 3.0,
        mount_dir: str = "/tmp/hil-uf2mnt",
        msc_label: str | None = None,
    ) -> None:
        super().__init__(transport=transport, port=port, sudo=sudo)
        self.settle_s = settle_s
        self.mount_dir = mount_dir
        #: Optional volume-label fallback if the by-path lookup misses.
        self.msc_label = msc_label

    # ------------------------------------------------------------------ #
    # MSC drive location                                                  #
    # ------------------------------------------------------------------ #

    async def _locate_msc(self) -> str | None:
        """Resolve the bootloader's MSC block device, or None if absent.

        Prefers the USB by-path scsi node (board-label-agnostic); falls back to
        a configured volume label. Returns a real ``/dev/sdX`` path.
        """
        token = usb_path_token(self.port)
        if token:
            # Glob the by-path scsi node for this DUT's USB port and resolve it.
            script = (
                f"for p in /dev/disk/by-path/*{token}*-scsi-*; do "
                f'[ -e "$p" ] && readlink -f "$p" && break; done'
            )
            res = await self._run(["bash", "-c", script], check=False, timeout=10)
            lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
            if lines:
                return lines[0]
        if self.msc_label:
            link = f"/dev/disk/by-label/{self.msc_label}"
            res = await self._run(["readlink", "-f", link], check=False, timeout=10)
            dev = (res.stdout or "").strip()
            if dev and dev != link:
                return dev
        return None

    # ------------------------------------------------------------------ #
    # Bootloader entry (1200-baud double-tap → UF2 bootloader)            #
    # ------------------------------------------------------------------ #

    async def bootloader_touch_1200(self, *, settle_s: float | None = None) -> None:
        """1200-baud touch to ask a running app to reboot into the UF2 bootloader."""
        settle = self.settle_s if settle_s is None else settle_s
        await self._run(stty_1200_touch_argv(self.port), check=False)
        if settle > 0:
            await asyncio.sleep(settle)

    async def is_in_bootloader(self, *, timeout: float = 10.0) -> bool:
        """True if the bootloader's MSC drive is currently present."""
        return bool(await self._locate_msc())

    async def _catch_and_touch(self, *, wait_s: float, pre_on_channel: int | None = None) -> bool:
        """In ONE remote shell, catch the board's brief app window and 1200-touch it.

        A board flapping on a marginal port — or reboot-looping on broken/no-secrets
        firmware — only shows its app CDC for a fraction of a second at a time. A
        per-attempt Python→SSH round trip is far too slow to hit that window, so this
        runs a tight ``sleep 0.2`` poll *on the host*: the instant the serial by-path
        node appears it issues ``stty <port> 1200`` and exits. The board reboots into
        the UF2 bootloader, which (unlike the crashing app) is stable.

        ``pre_on_channel`` implements the "power it on into an already-running tight
        loop" recovery: the loop starts FIRST (``turn_on.sh <ch>`` as the very first
        line, after which polling begins immediately), so a device whose firmware
        crashes the USB stack on boot is caught and forced into the bootloader before
        it can wedge the bus again. Returns True if the touch fired, False on timeout.
        """
        pre = ""
        if pre_on_channel is not None:
            pre = f"~/turn_on.sh {int(pre_on_channel)} >/dev/null 2>&1; "
        port = self.port
        script = (
            f"{pre}end=$(( $(date +%s) + {int(wait_s)} )); "
            f'while [ "$(date +%s)" -lt "$end" ]; do '
            f'if [ -e "{port}" ]; then stty -F "{port}" 1200 2>/dev/null && exit 0; fi; '
            f"sleep 0.2; done; exit 7"
        )
        res = await self._run(["bash", "-c", script], check=False, timeout=wait_s + 15)
        return getattr(res, "exit_status", 7) == 0

    async def enter_bootloader(
        self,
        *,
        attempts: int = 6,
        settle_s: float | None = None,
        on_line: Any | None = None,
        catch_s: float = 20.0,
        pre_on_channel: int | None = None,
    ) -> None:
        """Catch the app window and touch into the UF2 bootloader, until the MSC drive appears.

        Each round: if the MSC drive is already present we're done; otherwise run a
        tight :meth:`_catch_and_touch` (waits up to ``catch_s`` for the flapping
        board's app CDC, then 1200-touches it), settle, and re-check for the drive.
        ``pre_on_channel`` is forwarded to the FIRST round only (the
        power-on-into-the-loop recovery). Raises :class:`FlasherError` if the drive
        never appears.
        """

        def _log(msg: str) -> None:
            if on_line is not None:
                try:
                    on_line(msg)
                except Exception:  # noqa: BLE001
                    pass

        settle = self.settle_s if settle_s is None else settle_s
        for i in range(max(1, attempts)):
            if await self.is_in_bootloader():
                _log(f"UF2 bootloader drive present (round {i})")
                return
            ch = pre_on_channel if i == 0 else None
            on = " (power-on into loop)" if ch is not None else ""
            _log(f"catch-and-touch round {i + 1}/{attempts}{on} (≤{catch_s:.0f}s for app window)")
            touched = await self._catch_and_touch(wait_s=catch_s, pre_on_channel=ch)
            _log("  touched the app CDC" if touched else "  app window never appeared this round")
            if settle > 0:
                await asyncio.sleep(settle)
        if await self.is_in_bootloader():
            _log(f"UF2 bootloader drive present after {attempts} rounds")
            return
        raise FlasherError(
            f"UF2 bootloader MSC drive did not appear after {attempts} catch-and-touch rounds"
        )

    # ------------------------------------------------------------------ #
    # Four-verb contract                                                  #
    # ------------------------------------------------------------------ #

    async def probe(self) -> ChipInfo:
        """Read INFO_UF2.TXT off the bootloader drive for the Board-ID."""
        dev = await self._locate_msc()
        if not dev:
            raise FlasherError("uf2-msc.probe(): board is not in the UF2 bootloader")
        text = await self._read_info_uf2(dev)
        m = _BOARD_ID_RE.search(text)
        return ChipInfo(family=m.group(1) if m else "SAM-UF2", raw={"info": text})

    async def erase(self) -> None:
        """No-op: copying the .uf2 overwrites the app region via the bootloader."""
        return None

    async def flash(self, artifact: Artifact) -> FlashResult:
        """Mount the bootloader MSC drive, copy ``artifact.path`` (.uf2), sync.

        The bootloader writes flash as the file streams in and resets into the
        app, so the drive (and our mount) vanishes — the trailing ``umount`` is
        best-effort. ``offset`` is ignored: a UF2 carries its own target address.
        """
        dev = await self._locate_msc()
        if not dev:
            await self.enter_bootloader(on_line=None)
            dev = await self._locate_msc()
            if not dev:
                raise FlasherError(
                    "uf2-msc.flash(): UF2 bootloader MSC drive not found "
                    "(board not in bootloader / wrong USB by-path?)"
                )
        mnt = self.mount_dir
        t0 = time.monotonic()
        await self._run(["mkdir", "-p", mnt], check=False)
        await self._run(["mount", dev, mnt])  # needs root → sudo prefix
        try:
            await self._run(
                ["bash", "-c", f"cp {shlex.quote(artifact.path)} {shlex.quote(mnt)}/ && sync"]
            )
        finally:
            # The board reboots itself; the drive disappears → umount may fail.
            await self._run(["umount", mnt], check=False)
        elapsed = time.monotonic() - t0
        size = await self._run(
            ["bash", "-c", f"stat -c %s {shlex.quote(artifact.path)} 2>/dev/null || echo 0"],
            check=False,
        )
        try:
            written = int((size.stdout or "0").strip() or 0)
        except ValueError:
            written = 0
        return FlashResult(
            bytes_written=written,
            elapsed_s=elapsed,
            raw_stdout=f"copied {artifact.path} -> {dev} ({mnt})",
            raw_stderr="",
        )

    async def reset(self, *, into: Literal["bootloader", "application"]) -> None:
        """Bootloader: 1200-touch. Application: bootloader auto-resets after flash."""
        if into == "bootloader":
            await self.bootloader_touch_1200()
        # into="application": the UF2 bootloader resets into the app on a
        # successful copy; a power_cycle stage handles a clean cold boot.

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _read_info_uf2(self, dev: str) -> str:
        mnt = self.mount_dir
        await self._run(["mkdir", "-p", mnt], check=False)
        await self._run(["mount", dev, mnt], check=False)
        try:
            res = await self._run(
                ["bash", "-c", f"cat {shlex.quote(mnt)}/INFO_UF2.TXT 2>/dev/null"], check=False
            )
        finally:
            await self._run(["umount", mnt], check=False)
        return res.stdout or ""
