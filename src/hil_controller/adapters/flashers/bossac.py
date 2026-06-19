"""BossacFlasher — concrete CliFlasher for Microchip SAM (SAMD21/SAMD51) MCUs.

Drives the ``bossac`` CLI (BOSSA — Basic Open Source SAM-BA Application) over
the host transport to flash boards whose UF2/SAM-BA bootloader speaks the
SAM-BA protocol: the Adafruit PyPortal (M4 / Titano), Feather/Metro M0/M4, and
other SAMD CircuitPython/Arduino boards. On Debian/Raspberry Pi OS the binary
ships in the ``bossa-cli`` apt package (``setup-hil-host.sh`` installs it).

**Bootloader entry is the SAME 1200-baud "double-tap" touch** the ESP /
native-USB boards use (``stty -F <port> 1200``): a running app reboots into the
SAM-BA bootloader, which re-enumerates (Adafruit ``239a:0035`` for the M4) and
exposes a CDC port bossac talks to. The physical USB position is unchanged, so a
``by-path`` serial name stays valid across the flip — but bossac's ``-p`` wants a
bare ``ttyACMn`` (its POSIX backend resolves the name under ``/dev``), so we
``readlink -f`` the by-path to the live tty right before each invocation.

**SAM application offset.** A SAMD51 reserves the first 16 KiB (``0x4000``) for
the bootloader; a SAMD21 the first 8 KiB (``0x2000``). The application image is
written at that offset — **never ``0x0``**, which would clobber the bootloader.
So :meth:`flash` defaults a ``0``/``None`` artifact offset to ``app_offset``
(``0x4000``) rather than ``0``, a guard against the ESP-shaped ``offset: "0x0"``
in :data:`DEFAULT_FLASH_STAGES` quietly bricking the bootloader.

Runs on whichever host owns the serial port (rpi-displays for ship-artifacts
mode, the controller after a ``UsbipBridge.attached()``). Like
:class:`EsptoolFlasher` it takes ``(transport, port)`` at construction, not a
device record, so the same class serves both.
"""

from __future__ import annotations

import asyncio
import re
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

#: SAMD51 application start (16 KiB bootloader); SAMD21 boards use 0x2000.
SAMD51_APP_OFFSET = 0x4000
SAMD21_APP_OFFSET = 0x2000


# --------------------------------------------------------------------------- #
# Pure parsers (top-level so they're directly unit-testable)                  #
# --------------------------------------------------------------------------- #

# "Device       : ATSAMD51J20A" → "ATSAMD51J20A"
_DEVICE_RE = re.compile(r"Device\s*:\s*(\S+)", re.IGNORECASE)
# "Write 196608 bytes to flash (3072 pages)" → 196608
_WRITE_RE = re.compile(r"Write\s+(\d+)\s+bytes", re.IGNORECASE)


def parse_bossac_device(text: str) -> str | None:
    """Return the SAM device name (e.g. ``ATSAMD51J20A``) from ``bossac -i``."""
    m = _DEVICE_RE.search(text or "")
    return m.group(1) if m else None


def parse_bossac_written(text: str) -> int:
    """Sum of all ``Write N bytes`` lines in bossac's flash output."""
    return sum(int(m.group(1)) for m in _WRITE_RE.finditer(text or ""))


# --------------------------------------------------------------------------- #
# Flasher                                                                       #
# --------------------------------------------------------------------------- #


class BossacFlasher(CliFlasher):
    """Concrete flasher driving ``bossac`` for SAM (SAMD21/SAMD51) boards.

    ``app_offset`` is the application start in flash (``0x4000`` for SAMD51,
    ``0x2000`` for SAMD21); it is used for erase/write/verify so the bootloader
    region below it is never touched. ``settle_s`` is how long to wait after a
    1200-baud touch for the device to re-enumerate as the SAM-BA bootloader.
    """

    name = "bossac"
    tool = "bossac"

    def __init__(
        self,
        *,
        transport: Any,
        port: str,
        sudo: bool = False,
        app_offset: int = SAMD51_APP_OFFSET,
        settle_s: float = 2.0,
    ) -> None:
        super().__init__(transport=transport, port=port, sudo=sudo)
        self.app_offset = app_offset
        self.settle_s = settle_s

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _bare_port(self) -> str:
        """Resolve ``self.port`` (usually a by-path symlink) to bossac's tty name.

        bossac's POSIX serial backend takes a bare device name and resolves it
        under ``/dev``; an absolute ``/dev/serial/by-path/...`` symlink confuses
        it. ``readlink -f`` follows the (stable, across the app→bootloader flip)
        by-path symlink to the live ``/dev/ttyACMn``; we hand bossac the
        basename. Falls back to the basename of ``self.port`` if readlink yields
        nothing (e.g. the port is already a bare name).
        """
        dev = ""
        try:
            res = await self._run(["readlink", "-f", self.port], check=False, timeout=10)
            lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
            dev = lines[0] if lines else ""
        except Exception:  # noqa: BLE001 — resolution is best-effort; fall back below
            dev = ""
        return (dev or self.port).rsplit("/", 1)[-1]

    @staticmethod
    def _offset_hex(offset: int) -> str:
        return f"0x{offset:X}"

    def _base_argv(self, bare_port: str) -> list[str]:
        return [self.tool, "--port", bare_port]

    # ------------------------------------------------------------------ #
    # Bootloader entry (1200-baud double-tap → SAM-BA)                    #
    # ------------------------------------------------------------------ #

    async def bootloader_touch_1200(self, *, settle_s: float | None = None) -> None:
        """Issue a 1200-baud touch to ask a running app to reboot into SAM-BA.

        Runs ``stty -F <port> 1200`` (against the stable by-path) then waits for
        re-enumeration. ``check=False``: a touch mid-re-enumeration fails with an
        I/O error that must NOT abort the surrounding loop — the next probe decides.
        """
        settle = self.settle_s if settle_s is None else settle_s
        await self._run(stty_1200_touch_argv(self.port), check=False)
        if settle > 0:
            await asyncio.sleep(settle)

    async def is_in_bootloader(self, *, timeout: float = 15.0) -> bool:
        """True if the SAM-BA bootloader answers ``bossac -i`` on this port.

        When the application is running the CDC port doesn't speak SAM-BA, so
        ``bossac -i`` fails — exactly the discriminator the touch loop needs.
        """
        port = await self._bare_port()
        try:
            result = await self._run(
                self._base_argv(port) + ["--info"], check=False, timeout=timeout
            )
        except TimeoutError:
            return False
        except FlasherError:
            return False
        text = (result.stdout or "") + (result.stderr or "")
        return result.exit_status == 0 and ("Device" in text or "SAM" in text or "Atmel" in text)

    async def enter_bootloader(
        self,
        *,
        attempts: int = 10,
        settle_s: float | None = None,
        on_line: Any | None = None,
    ) -> None:
        """Loop a 1200-baud touch until the SAM-BA bootloader is reachable.

        Returns once :meth:`is_in_bootloader` succeeds (which also covers a
        freshly-erased board whose bootloader is already resident), else raises
        :class:`FlasherError`.
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
                _log(f"SAM-BA bootloader reachable after {i} touch(es)")
                return
            _log(f"1200-baud touch {i + 1}/{attempts} (settle {settle}s)")
            await self.bootloader_touch_1200(settle_s=settle)
        if await self.is_in_bootloader():
            _log(f"SAM-BA bootloader reachable after {attempts} touch(es)")
            return
        raise FlasherError(
            f"device did not enter SAM-BA bootloader after {attempts} 1200-baud touches"
        )

    # ------------------------------------------------------------------ #
    # Four-verb contract                                                  #
    # ------------------------------------------------------------------ #

    async def probe(self) -> ChipInfo:
        """Run ``bossac -i`` and parse the SAM device name."""
        port = await self._bare_port()
        result = await self._run(self._base_argv(port) + ["--info"])
        text = (result.stdout or "") + (result.stderr or "")
        return ChipInfo(family=parse_bossac_device(text) or "SAM", raw={"info": text})

    async def erase(self) -> None:
        """Erase the application region (``-e`` starting at ``app_offset``).

        The offset keeps the erase above the bootloader so the SAM-BA loader
        survives. ``flash()`` already erases as part of its combined call, so a
        standalone erase stage is usually unnecessary for SAM boards.
        """
        port = await self._bare_port()
        await self._run(
            self._base_argv(port) + ["--erase", f"--offset={self._offset_hex(self.app_offset)}"]
        )

    async def flash(self, artifact: Artifact) -> FlashResult:
        """Erase + write + verify ``artifact`` at the app offset, then reset to run it.

        One combined ``bossac -e -w -v -b -R --offset=<app> <file>``: erase
        (accelerated when combined with write), write the image, verify it, set
        boot-from-flash, and reset the CPU into the freshly-flashed app. A
        ``0``/``None`` offset is coerced to ``app_offset`` so an ESP-shaped
        ``offset: 0x0`` can never overwrite the bootloader. Verify failure makes
        bossac exit non-zero → :class:`FlasherToolFailed`.
        """
        port = await self._bare_port()
        offset = artifact.offset if artifact.offset else self.app_offset
        argv = self._base_argv(port) + [
            "--erase",
            "--write",
            "--verify",
            "--boot",
            "--reset",
            f"--offset={self._offset_hex(offset)}",
            artifact.path,
        ]
        t0 = time.monotonic()
        result = await self._run(argv)
        elapsed = time.monotonic() - t0
        out = result.stdout or ""
        return FlashResult(
            bytes_written=parse_bossac_written(out),
            elapsed_s=elapsed,
            raw_stdout=out,
            raw_stderr=result.stderr or "",
        )

    async def reset(self, *, into: Literal["bootloader", "application"]) -> None:
        """Reset into the bootloader (1200-baud touch) or the application (``-R``)."""
        if into == "bootloader":
            await self.bootloader_touch_1200()
            return
        port = await self._bare_port()
        # Set boot-from-flash + reset the CPU so the application runs. Best-effort:
        # a power-cycle stage is the authoritative reboot when a solenoid exists.
        await self._run(self._base_argv(port) + ["--boot", "--reset"], check=False)
