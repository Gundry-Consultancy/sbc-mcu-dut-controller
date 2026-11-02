"""PicotoolFlasher — RP2040/RP2350 rescue verbs over the host's ``picotool`` CLI.

Runs ``picotool`` on the host that physically owns the DUT (same
``(transport, port)`` construction as :class:`EsptoolFlasher`). picotool talks
to the chip's BOOTSEL ROM over raw USB (no serial port), so most verbs need the
board *in* BOOTSEL mode — ``picotool reboot -f -u`` can flip a **running**
application there (if it links the picotool reset interface), and the
``enter_bootsel`` bench stage covers the dead-firmware case with the solenoid
BOOTSEL press. ``erase`` requires picotool >= 2.0 (install that on any bench
host that owns RP2040/RP2350 DUTs).

``port`` is the DUT's CDC serial node — picotool itself never opens it, but a
stale holder on it can block the device's re-enumeration dance, so the same
``fuser -k`` cleanup as esptool's is available on timeouts.
"""

from __future__ import annotations

import re
import time
from typing import Any

from hil_controller.adapters.flashers.base import (
    Artifact,
    ChipInfo,
    CliFlasher,
    FlashResult,
)

# picotool's "nothing in BOOTSEL mode" failure signatures across versions:
#   1.x: "No accessible RP2040 devices in BOOTSEL mode were found."
#   2.x: "No accessible RP-series devices in BOOTSEL mode were found."
# both optionally followed by "...but:  device at ... appears to be in
# application mode". Matched loosely so a wording tweak doesn't break the
# bootsel_fallback path.
_NO_BOOTSEL_RE = re.compile(r"No accessible RP\S* devices", re.IGNORECASE)

# "type:                 RP2040" (info -a Device Information section)
_PICO_TYPE_RE = re.compile(r"^\s*type:\s+(RP\S+)", re.MULTILINE)
# "flash size:           2048K"
_PICO_FLASH_RE = re.compile(r"flash size:\s+(\d+)K", re.IGNORECASE)
# "name:          blink" (Program Information section)
_PICO_NAME_RE = re.compile(r"^\s*name:\s+(\S.*)$", re.MULTILINE)


def needs_bootsel(text: str) -> bool:
    """True when picotool output says no BOOTSEL-mode device was reachable.

    This is the trigger for the ``picotool_rescue`` stage's ``bootsel_fallback``
    (hold BOOTSEL via the solenoid, power-cycle, retry once).
    """
    return bool(_NO_BOOTSEL_RE.search(text or "")) and "BOOTSEL" in (text or "")


def parse_picotool_info(text: str) -> dict[str, str | None]:
    """Parse ``picotool info -a`` output into the fields worth structuring.

    Returns ``{"family", "flash_k", "program"}`` (each ``None`` when absent) —
    enough identity for a rescue verdict without modelling the whole report.
    """
    t = text or ""
    m_type = _PICO_TYPE_RE.search(t)
    m_flash = _PICO_FLASH_RE.search(t)
    m_name = _PICO_NAME_RE.search(t)
    return {
        "family": m_type.group(1) if m_type else None,
        "flash_k": m_flash.group(1) if m_flash else None,
        "program": m_name.group(1).strip() if m_name else None,
    }


class PicotoolFlasher(CliFlasher):
    """Concrete flasher driving ``picotool`` for RP2040/RP2350 rescue."""

    name = "picotool"
    tool = "picotool"

    def __init__(
        self,
        *,
        transport: Any,
        port: str,
        sudo: bool = False,
        timeout_s: float = 180.0,
    ) -> None:
        super().__init__(transport=transport, port=port, sudo=sudo)
        self.timeout_s = timeout_s

    async def _free_serial_port(self) -> None:
        """Best-effort: kill any process still holding ``self.port``.

        Mirrors :meth:`EsptoolFlasher._free_serial_port` — a timed-out remote
        command can leave a holder on the DUT's tty that wedges the next
        attempt / the board's re-enumeration. Never raises.
        """
        if not self.port:
            return
        try:
            await self._run(["fuser", "-k", self.port], check=False, timeout=10)
        except Exception:  # noqa: BLE001 — cleanup must not break the caller
            pass

    async def _run_picotool(self, *args: str) -> Any:
        """Run one picotool command, bounded; frees the tty on a timeout."""
        try:
            return await self._run([self.tool, *args], timeout=self.timeout_s)
        except TimeoutError:
            await self._free_serial_port()
            raise

    # ------------------------------------------------------------------ #
    # Rescue verbs                                                        #
    # ------------------------------------------------------------------ #

    async def info(self) -> str:
        """``picotool info -a`` — full identity report; returns raw stdout."""
        result = await self._run_picotool("info", "-a")
        return result.stdout or ""

    async def reboot_bootsel(self) -> None:
        """``picotool reboot -f -u`` — force a *running* app into BOOTSEL (USB boot)."""
        await self._run_picotool("reboot", "-f", "-u")

    async def reboot_app(self) -> None:
        """``picotool reboot`` — reboot a BOOTSEL-mode chip into its application."""
        await self._run_picotool("reboot")

    # ------------------------------------------------------------------ #
    # FlasherProtocol verbs                                               #
    # ------------------------------------------------------------------ #

    async def probe(self) -> ChipInfo:
        """``info -a`` parsed into a :class:`ChipInfo` (family / flash size)."""
        text = await self.info()
        parsed = parse_picotool_info(text)
        flash_k = parsed["flash_k"]
        return ChipInfo(
            family=parsed["family"] or "RP2040",
            flash_bytes=int(flash_k) * 1024 if flash_k else None,
            raw={"info": text},
        )

    async def erase(self) -> None:
        """``picotool erase`` — blank the whole flash (picotool >= 2.0)."""
        await self._run_picotool("erase")

    async def flash(self, artifact: Artifact) -> FlashResult:
        """``picotool load -v -x <path>`` — write, verify, then execute.

        The image must already be staged on the DUT host (the firmware-bench
        orchestrator's ``_stage_firmware`` puts it at ``/tmp/hil/<job>/…``).
        UF2 files carry their own addresses; a plain ``.bin`` relies on
        picotool's default load address.
        """
        t0 = time.monotonic()
        result = await self._run_picotool("load", "-v", "-x", artifact.path)
        return FlashResult(
            bytes_written=0,  # picotool reports ranges, not byte totals
            elapsed_s=time.monotonic() - t0,
            raw_stdout=result.stdout or "",
            raw_stderr=result.stderr or "",
        )
