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
from hil_controller.adapters.flashers.bossac import SAMD51_APP_OFFSET
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
        bossac: str = "bossac",
        app_offset: int = SAMD51_APP_OFFSET,
    ) -> None:
        super().__init__(transport=transport, port=port, sudo=sudo)
        self.settle_s = settle_s
        self.mount_dir = mount_dir
        #: Optional volume-label fallback if the by-path lookup misses.
        self.msc_label = msc_label
        #: bossac binary used by :meth:`erase` to blank the app via the bootloader
        #: SAM-BA CDC. Defaults to PATH lookup — ``setup-hil-host.sh`` installs the
        #: Adafruit/Arduino fork at ``/usr/local/bin`` (ahead of Debian's broken
        #: one), so ``"bossac"`` resolves to the working build.
        self.bossac = bossac
        #: SAM application start (``0x4000`` on SAMD51); the erase stays above it
        #: so the bootloader region is never touched.
        self.app_offset = app_offset

    # ------------------------------------------------------------------ #
    # MSC drive location                                                  #
    # ------------------------------------------------------------------ #

    async def _locate_msc(self) -> str | None:
        """Resolve the **UF2 bootloader's** MSC block device, or None if absent.

        CRITICAL: a running WipperSnapper app ALSO exposes an MSC drive (label
        ``WIPPER``) at the *same* USB by-path as the bootloader's ``*BOOT`` drive
        (e.g. ``PORTALBOOT``) — but only the bootloader flashes a copied ``.uf2``;
        copying to ``WIPPER`` is a silent no-op. So we match the by-path scsi node
        AND require its volume label to look like a UF2 bootloader (``*BOOT``, or a
        configured ``msc_label``). When only ``WIPPER`` is present we return None,
        so :meth:`is_in_bootloader` is False and the caller 1200-touches into the
        actual bootloader before flashing.
        """
        token = usb_path_token(self.port)
        if not token:
            return None
        want = (self.msc_label or "").upper()
        # For each by-path scsi node, resolve the block dev + read its FAT label;
        # accept it only if the label is a bootloader (endswith BOOT) or matches a
        # configured msc_label. Reject WIPPER (the running app's data drive).
        script = (
            f"for p in /dev/disk/by-path/*{token}*-scsi-*; do "
            f'[ -e "$p" ] || continue; '
            f'dev=$(readlink -f "$p"); '
            f'lbl=$(lsblk -no LABEL "$dev" 2>/dev/null | head -1 | tr -d " "); '
            f'lu=$(echo "$lbl" | tr "a-z" "A-Z"); '
            f'case "$lu" in *BOOT) echo "$dev"; break;; esac; '
            f'[ -n "{want}" ] && [ "$lu" = "{want}" ] && {{ echo "$dev"; break; }}; '
            f"done"
        )
        res = await self._run(["bash", "-c", script], check=False, timeout=10)
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        return lines[0] if lines else None

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

    async def _catch_and_touch(self, *, wait_s: float) -> bool:
        """Tight 1200-touch HAMMER that breaks a (boot)looping SAMD into its bootloader.

        A fast-crashing / reboot-looping SAMD shows its app CDC for only a fraction
        of a second per cycle — far too briefly for a poll-then-touch Python→SSH
        round trip to land. So this runs a tight loop ON THE HOST that, every
        ~50 ms, (a) 1200-baud touches the app CDC the instant it enumerates (both
        ``usb``/``usbv2`` by-path variants of interface :1.0) and (b) checks for the
        ``*BOOT`` bootloader MSC drive — returning as soon as a touch has flipped the
        board into the *stable* UF2 bootloader. Proven to catch the PyPortal Titano
        in ~6 s. (``samd51_uf2``'s **power** double-tap cannot work: the SAMD
        double-tap keys off a RAM magic value that a power-off clears — only a
        *reset* preserves it, and the solenoid only controls power.)
        """
        token = usb_path_token(self.port)
        if not token:
            return False
        script = (
            f"end=$(( $(date +%s) + {int(wait_s)} )); "
            f'while [ "$(date +%s)" -lt "$end" ]; do '
            f"for p in /dev/serial/by-path/*{token}:1.0; do "
            f'[ -e "$p" ] && stty -F "$p" 1200 2>/dev/null; done; '
            f'for q in /dev/disk/by-path/*{token}*-scsi-*; do [ -e "$q" ] || continue; '
            f'd=$(readlink -f "$q"); '
            f'l=$(lsblk -no LABEL "$d" 2>/dev/null | head -1 | tr -d " " | tr "a-z" "A-Z"); '
            f'case "$l" in *BOOT) echo "BOOT:$d"; exit 0;; esac; done; '
            f"sleep 0.05; done; exit 7"
        )
        res = await self._run(["bash", "-c", script], check=False, timeout=wait_s + 15)
        return getattr(res, "exit_status", 7) == 0

    async def enter_bootloader(
        self,
        *,
        attempts: int = 3,
        settle_s: float | None = None,  # noqa: ARG002 — kept for call-site compat
        on_line: Any | None = None,
        catch_s: float = 30.0,
        **_ignored: Any,  # swallow legacy kwargs (e.g. pre_on_channel)
    ) -> None:
        """Hammer the 1200-touch until the device is in the UF2 bootloader.

        Assumes the device is powered (the stage handles power/power-cycling). Each
        round runs the tight :meth:`_catch_and_touch` hammer for up to ``catch_s``;
        it returns the instant the ``*BOOT`` drive appears. Raises
        :class:`FlasherError` if the bootloader isn't reached after ``attempts``.
        """

        def _log(msg: str) -> None:
            if on_line is not None:
                try:
                    on_line(msg)
                except Exception:  # noqa: BLE001
                    pass

        if await self.is_in_bootloader():
            _log("UF2 bootloader already present")
            return
        for i in range(max(1, attempts)):
            _log(f"1200-touch hammer round {i + 1}/{attempts} (≤{catch_s:.0f}s)")
            if await self._catch_and_touch(wait_s=catch_s):
                _log("device is in the UF2 bootloader (hammer caught it)")
                return
            _log("  hammer round did not reach the bootloader")
        if await self.is_in_bootloader():
            _log("UF2 bootloader present after hammer")
            return
        raise FlasherError(
            f"UF2 bootloader not reached after {attempts} 1200-touch hammer rounds "
            f"({catch_s:.0f}s each)"
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

    async def _bootloader_tty(self) -> str | None:
        """Resolve the bootloader's SAM-BA CDC to a bare ``ttyACMn`` for bossac.

        The Adafruit UF2 bootloader is a **composite CDC+MSC** device: alongside
        the ``*BOOT`` mass-storage drive it exposes a CDC interface (``:1.0``, the
        same USB by-path the running app's CDC uses) that speaks the SAM-BA
        protocol — which the Adafruit-fork ``bossac`` can *erase* even though its
        *write* applet is incompatible with Debian's build (see
        :class:`BossacFlasher`). Prefers :attr:`port` when it resolves, else globs
        the ``<token>:1.0`` by-path; returns the basename (bossac's POSIX backend
        resolves it under ``/dev``), or None if no CDC node is present.
        """
        token = usb_path_token(self.port)
        script = (
            f'p={shlex.quote(self.port)}; '
            f'[ -e "$p" ] || for q in /dev/serial/by-path/*{token}:1.0; do '
            f'[ -e "$q" ] && p="$q" && break; done; '
            f'[ -e "$p" ] && readlink -f "$p"'
        )
        res = await self._run(["bash", "-c", script], check=False, timeout=10)
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        dev = lines[0] if lines else ""
        return dev.rsplit("/", 1)[-1] if dev else None

    async def erase(self) -> None:
        """Blank the SAMD app region via the UF2 bootloader's SAM-BA CDC.

        Runs ``bossac --erase --offset=0x4000`` against the bootloader's CDC tty.
        Erasing leaves the app region blank **without resetting**, so the
        composite bootloader (and its ``*BOOT`` MSC drive) stays up for the
        following :meth:`flash` copy. The point is reliability: with a genuinely
        blank app, a flash that silently no-ops (or fails to take) leaves a board
        that drops back to the bootloader — a clear FAIL — instead of booting the
        **stale previous firmware** and reporting a false PASS. That false-PASS is
        exactly what made a v128 bisection job "pass" while still running the old
        image, so the bisection erases before every flash.

        Requires the device in the bootloader (enters it if not). Raises
        :class:`FlasherError` if the erase can't run, so the caller treats it as
        an INFRA failure (recover + retry) rather than flashing onto an unknown
        state. Uses the Adafruit-fork ``bossac`` (:attr:`bossac`); Debian's build
        is unusable for SAMD51.
        """
        if not await self.is_in_bootloader():
            await self.enter_bootloader()
        tty = await self._bootloader_tty()
        if not tty:
            raise FlasherError("uf2-msc.erase(): no bootloader CDC tty found to run bossac against")
        await self._run(
            [self.bossac, "--port", tty, "--erase", f"--offset=0x{self.app_offset:X}"]
        )

    async def flash(self, artifact: Artifact, *, attempts: int = 4) -> FlashResult:
        """Mount the bootloader MSC drive, copy ``artifact.path`` (.uf2), sync.

        The bootloader writes flash as the file streams in and resets into the
        app, so the drive (and our mount) vanishes — the trailing ``umount`` is
        best-effort. ``offset`` is ignored: a UF2 carries its own target address.

        **Re-enters the bootloader on every failed round.** The Adafruit UF2
        bootloader auto-boots the resident app after a few seconds, so if a slow
        preceding stage (e.g. ``launch_protomq`` cloning+building) sits between
        bootloader entry and this flash, the drive is gone by the time we mount
        (``mount: Can't open blockdev``). Each round therefore (re)enters the
        bootloader via the 1200-touch loop, clears any stale mount, then
        mount→cp→sync; a flapping marginal port is retried up to ``attempts``.
        """
        mnt = self.mount_dir
        last: Exception | None = None
        for i in range(max(1, attempts)):
            dev = await self._locate_msc()
            if not dev:
                try:
                    await self.enter_bootloader()
                except FlasherError as exc:
                    last = exc
                    continue
                dev = await self._locate_msc()
            if not dev:
                last = FlasherError("UF2 bootloader MSC drive not found after bootloader entry")
                continue
            await self._run(["mkdir", "-p", mnt], check=False)
            await self._run(["umount", mnt], check=False)  # clear any stale mount
            t0 = time.monotonic()
            try:
                await self._run(["mount", dev, mnt])  # needs root → sudo prefix
            except FlasherError as exc:
                # Drive flapped / booted the app between locate and mount — retry
                # the whole round (which re-enters the bootloader).
                last = exc
                await asyncio.sleep(2)
                continue
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
        raise last or FlasherError("uf2-msc.flash(): exhausted attempts")

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
