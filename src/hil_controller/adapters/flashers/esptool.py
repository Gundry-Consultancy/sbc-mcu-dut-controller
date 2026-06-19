"""EsptoolFlasher — concrete CliFlasher for ESP32 family.

Wraps the ``esptool.py`` CLI. Used by:

* ``ArduinoWsExecAdapter`` flash phase for the raw-``.bin`` ``raw-firmware-smoke``
  case (PlatformIO upload still goes through ``pio run --target upload``).
* ``UsbFingerprintAdapter`` for a ``probe()`` confirmation step gated by
  ``usb_fingerprint.confirm_with_esptool=true`` in the host config.
* The TinyUF2 install flow (``erase()`` then ``flash(combined.bin)``).

Runs on whichever host currently owns the serial port — that's rpi-displays
in ship-artifacts mode, and the controller after a ``UsbipBridge.attached()``
context manager has entered for usbip mode. The same class serves both: it
takes ``(transport, port)`` at construction time, not a device record.
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
    FlasherToolFailed,
    FlashResult,
)

# esptool output fragments that indicate a TRANSIENT connect/port glitch (the
# ESP32-S3 USB-Serial/JTAG re-enumerates when reset from a running app), worth
# retrying. NOT matched: digest/verify mismatches or bad-image errors.
_ESPTOOL_TRANSIENT = (
    "Could not configure port",
    "Input/output error",
    "Failed to connect",
    "No serial data received",
    "could not open port",
    "Timed out waiting for packet",
    "Errno 5",
)

# Valid values for esptool's global ``--after`` reset behaviour. ``no_reset``
# leaves the chip in the download ROM (so a follow-up erase→flash→verify chain
# doesn't bounce the USB device between each step); ``hard_reset`` (esptool's
# own default) toggles RTS to reboot into the application; ``watchdog_reset``
# triggers an on-chip RTC-watchdog reboot — the ONLY reliable reset for a
# native-USB ESP32-S2/-S3/-C3 whose RTS/DTR are not wired to EN/IO0 (so
# ``hard_reset`` is a no-op there). Used as the power-cycle fallback when a DUT
# has no solenoid channel mapped.
ESPTOOL_AFTER_MODES = frozenset(
    {"hard_reset", "watchdog_reset", "no_reset", "soft_reset", "no_reset_no_sync"}
)
ESPTOOL_BEFORE_MODES = frozenset({"default_reset", "usb_reset", "no_reset", "no_reset_no_sync"})


def stty_1200_touch_argv(port: str) -> list[str]:
    """Build the argv for a 1200-baud bootloader touch on *port*.

    Native-USB boards (CircuitPython/UF2, and ESP32-S3 in USB-CDC mode) drop
    into their ROM/UF2 bootloader when the host opens the CDC port at 1200 baud
    and closes it. ``stty`` performs exactly that open-at-1200 then close, which
    is why the operator-facing knob is spelled ``stty -F <port> 1200``.
    """
    return ["stty", "-F", port, "1200"]


# --------------------------------------------------------------------------- #
# Pure parsers (top-level so they're directly unit-testable)                  #
# --------------------------------------------------------------------------- #

# "Chip is ESP32-S3 (revision v0.2)" → "ESP32-S3"
_CHIP_RE = re.compile(r"Chip is\s+([\w\-]+)", re.IGNORECASE)
# "MAC: 7c:df:a1:b2:c3:d4" — 6 colon-separated hex pairs
_MAC_RE = re.compile(r"^MAC:\s+([0-9a-fA-F:]{17})\s*$", re.MULTILINE)
# "Detected flash size: 8MB"
_FLASH_SIZE_RE = re.compile(r"Detected flash size:\s*(\d+)\s*MB", re.IGNORECASE)
# "Wrote 123456 bytes ..." (one per write_flash segment)
_WROTE_RE = re.compile(r"Wrote\s+(\d+)\s+bytes")


def parse_chip_family(text: str) -> str | None:
    """Return e.g. ``"ESP32-S3"`` from esptool stdout, or ``None`` if absent."""
    m = _CHIP_RE.search(text or "")
    return m.group(1) if m else None


def parse_mac(text: str) -> str | None:
    """Return the (lowercased) MAC address from esptool output, or ``None``."""
    m = _MAC_RE.search(text or "")
    return m.group(1).lower() if m else None


def parse_flash_size_bytes(text: str) -> int | None:
    """Return flash size in bytes (parsed from ``Detected flash size: NMB``)."""
    m = _FLASH_SIZE_RE.search(text or "")
    return int(m.group(1)) * 1024 * 1024 if m else None


def parse_wrote_bytes(text: str) -> int:
    """Sum of all ``Wrote N bytes`` lines across multi-segment write_flash."""
    return sum(int(m.group(1)) for m in _WROTE_RE.finditer(text or ""))


# --------------------------------------------------------------------------- #
# Boot-state classification (the "detect the failure state" diagnostic)        #
# --------------------------------------------------------------------------- #

# "rst:0x7 (TG0WDT_SYS_RST)" / "rst:0x15 (USB_UART_CHIP_RESET)" → the reason tag.
_RST_RE = re.compile(r"rst:0x[0-9a-fA-F]+\s*\(([A-Z0-9_]+)\)")

# Known ROM/bootloader/app serial signatures, in priority order. The first
# group whose needle appears in the captured boot log names the state. Used to
# decide rectification: a ``blank_or_corrupt_flash`` board boot-loops in normal
# mode (the 1200-touch can't help — no app to honour it), so it needs the
# USB-Serial/JTAG reset (``--before default_reset``) to enter the download
# loader; an ``app_running`` board takes the normal 1200-touch route.
_BOOT_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("download_mode", ("waiting for download", "DOWNLOAD(USB", "Stub flasher running")),
    (
        "app_panic",
        (
            "Guru Meditation",
            "assert failed",
            "Backtrace:",
            "abort() was called",
            "Panic",
            "rst:0x3 (RTC_SW",
        ),
    ),
    (
        "blank_or_corrupt_flash",
        ("invalid header: 0xffffffff", "invalid header", "no bootable app partition"),
    ),
    ("bootloader_error", ("checksum error", "image corrupted", "boot: Fatal", " flash read err")),
    (
        "app_running",
        ("boot_out.txt", "WipperSnapper", "CircuitPython", "MicroPython", "Adafruit", "cpu_start:"),
    ),
)


def parse_reset_reason(text: str) -> str | None:
    """Return the ESP32 ROM reset-reason tag (e.g. ``TG0WDT_SYS_RST``) or None."""
    m = _RST_RE.search(text or "")
    return m.group(1) if m else None


def classify_boot_state(text: str) -> dict[str, str | None]:
    """Classify a board's captured serial boot log into a known state.

    Returns ``{"state": <name>, "reset_reason": <tag-or-None>}`` where state is
    one of ``download_mode``, ``app_panic``, ``blank_or_corrupt_flash``,
    ``bootloader_error``, ``app_running``, or ``unknown``. Drives the
    detect→rectify flow in :func:`bench_stages._stage_enter_bootloader`.
    """
    t = text or ""
    state = "unknown"
    for name, needles in _BOOT_SIGNATURES:
        if any(n in t for n in needles):
            state = name
            break
    return {"state": state, "reset_reason": parse_reset_reason(t)}


# Map ChipInfo.family ↔ esptool --chip argument.
_FAMILY_TO_CHIP_ARG = {
    "ESP32": "esp32",
    "ESP32-S2": "esp32s2",
    "ESP32-S3": "esp32s3",
    "ESP32-C3": "esp32c3",
    "ESP32-C6": "esp32c6",
    "ESP32-H2": "esp32h2",
    "ESP32-P4": "esp32p4",
    "ESP8266": "esp8266",
}


def family_to_chip_arg(family: str | None) -> str:
    """Translate a ``ChipInfo.family`` value to esptool's ``--chip`` argument.

    Returns ``"auto"`` when unknown so esptool's own detection runs.
    """
    if not family:
        return "auto"
    return _FAMILY_TO_CHIP_ARG.get(family.upper(), "auto")


# --------------------------------------------------------------------------- #
# Flasher                                                                       #
# --------------------------------------------------------------------------- #


class EsptoolFlasher(CliFlasher):
    """Concrete flasher driving ``esptool.py``.

    Set ``chip="auto"`` to let esptool detect, or pass an explicit family
    (e.g. ``"esp32s3"``) when the device record knows. ``baud`` defaults to
    921600 — esptool's standard fast-upload rate; drop to 460800 for flaky
    USB-IP attachments.
    """

    name = "esptool"
    tool = "esptool.py"

    def __init__(
        self,
        *,
        transport: Any,
        port: str,
        chip: str = "auto",
        baud: int = 921600,
        sudo: bool = False,
        connect_retries: int = 3,
        connect_backoff_s: float = 3.0,
        python: str = "python3",
    ) -> None:
        super().__init__(transport=transport, port=port, sudo=sudo)
        self.chip = chip
        self.baud = baud
        self.connect_retries = connect_retries
        self.connect_backoff_s = connect_backoff_s
        # Invoke as ``python3 -m esptool`` rather than the ``esptool.py`` console
        # script: the latter is deprecated in esptool v5 (noisy warnings) and the
        # wrapper can truncate captured output; ``-m`` runs the module directly.
        self.python = python

    async def _run_esptool(self, argv: list[str], **kw: Any) -> Any:
        """Run esptool, retrying transient connect/port glitches.

        ESP32-S3 USB-Serial/JTAG flashing intermittently fails to open/sync when
        the chip is reset from a running app; a retry usually catches it. Bad
        images / verify mismatches are not transient and fail immediately.
        """
        attempts = max(1, self.connect_retries + 1)
        for i in range(attempts):
            try:
                return await self._run(argv, **kw)
            except FlasherToolFailed as exc:
                blob = (exc.stdout or "") + "\n" + (exc.stderr or "")
                # A failure BEFORE chip detection is a connect-phase glitch
                # (esptool stalls at "Connecting..."; the fatal line is often
                # lost). A failure AFTER connecting (chip detected, then e.g. a
                # verify/MD5 mismatch) is a real error — don't retry that.
                connected = any(m in blob for m in ("Chip type", "Chip is", "Stub flasher running"))
                transient = (not connected) or any(m in blob for m in _ESPTOOL_TRANSIENT)
                if transient and i < attempts - 1:
                    await asyncio.sleep(self.connect_backoff_s)
                    continue
                raise

    def _base_argv(self, *, after: str | None = None, before: str | None = None) -> list[str]:
        argv = [
            self.python,
            "-m",
            "esptool",
            "--chip",
            self.chip,
            "--port",
            self.port,
            "--baud",
            str(self.baud),
        ]
        if before is not None:
            if before not in ESPTOOL_BEFORE_MODES:
                raise ValueError(
                    f"esptool --before must be one of {sorted(ESPTOOL_BEFORE_MODES)}, got {before!r}"  # noqa: E501
                )
            argv += ["--before", before]
        if after is not None:
            if after not in ESPTOOL_AFTER_MODES:
                raise ValueError(
                    f"esptool --after must be one of {sorted(ESPTOOL_AFTER_MODES)}, got {after!r}"
                )
            argv += ["--after", after]
        return argv

    async def _free_serial_port(self) -> None:
        """Best-effort: kill any process still holding ``self.port``.

        :meth:`is_in_download_mode` bounds esptool with ``asyncio.wait_for``; on a
        transport that runs esptool remotely (SSH), the timeout cancels the local
        await but the *remote* esptool keeps the serial port open. The next probe
        or 1200-baud touch then fails with "port is busy" and the touch loop
        stalls. ``fuser -k`` (which follows the by-path/by-id symlink to the tty)
        clears the stale holder. Never raises — cleanup must not break the caller.
        """
        try:
            await self._run(["fuser", "-k", self.port], check=False, timeout=10)
        except Exception:  # noqa: BLE001
            pass

    async def is_in_download_mode(self, *, timeout: float = 15.0) -> bool:
        """True if the ROM bootloader is listening (``--before no_reset`` syncs).

        Uses ``no_reset`` so the check itself doesn't toggle reset — it only
        succeeds if the chip is *already* in the download ROM. ``default_reset``
        is deliberately avoided: a native-USB S3 running a TinyUSB app ignores
        the DTR/RTS reset (the probe would falsely report "not reachable"), and
        once a 1200-baud touch *has* dropped it into the USB-Serial/JTAG ROM a
        reset toggle would knock it back out. ``--after no_reset`` likewise: the
        probe must NOT bounce the chip out of download mode on its way out — the
        only reboot in the cycle is the deliberate power-cycle stage. Bounded by
        ``timeout`` so the
        touch loop iterates briskly instead of waiting out esptool's own
        ~10s connect retries on a port that isn't there yet; on a timeout the
        stale (remote) esptool is killed so it can't wedge the next attempt.
        """
        try:
            result = await self._run(
                self._base_argv(before="no_reset", after="no_reset") + ["flash_id"],
                check=False,
                timeout=timeout,
            )
        except TimeoutError:
            await self._free_serial_port()
            return False
        except FlasherError:
            return False
        text = (result.stdout or "") + (result.stderr or "")
        return result.exit_status == 0 and ("Chip" in text or "MAC:" in text)

    async def enter_download_mode(
        self,
        *,
        attempts: int = 10,
        settle_s: float = 2.0,
        on_line: Any | None = None,
    ) -> None:
        """Loop a 1200-baud touch until the ROM download mode is reachable.

        For native-USB ESP32-S3 boards a 1200bps touch (open at 1200, close)
        asks a running app to reboot into the bootloader; it can take a few
        tries to land. Returns once :meth:`is_in_download_mode` succeeds, else
        raises :class:`FlasherError`.
        """

        def _log(msg: str) -> None:
            if on_line is not None:
                try:
                    on_line(msg)
                except Exception:  # noqa: BLE001
                    pass

        for i in range(max(1, attempts)):
            if await self.is_in_download_mode():
                _log(f"download mode reachable after {i} touch(es)")
                return
            _log(f"1200-baud touch {i + 1}/{attempts} (settle {settle_s}s)")
            await self.bootloader_touch_1200(settle_s=settle_s)
        if await self.is_in_download_mode():
            _log(f"download mode reachable after {attempts} touch(es)")
            return
        raise FlasherError(f"device did not enter download mode after {attempts} 1200-baud touches")

    async def read_boot_log(
        self, *, seconds: float = 15.0, baud: int = 115200, max_bytes: int = 16000
    ) -> str:
        """Capture the board's serial boot log across its reset cycles.

        A board that boot-loops (e.g. blank flash → bootloader watchdog) drops
        its USB-Serial/JTAG every couple of seconds, so a single ``cat`` would
        die immediately. This reconnecting loop (50ms poll) re-opens the port
        whenever it reappears and reads each up-window for ``seconds`` total,
        capped at ``max_bytes`` of output. The text is fed to
        :func:`classify_boot_state` to identify the failure (``invalid header``,
        a panic, a reset reason, ...). Best-effort — returns whatever was read.
        """
        port = self.port
        script = (
            f"end=$(( $(date +%s) + {int(seconds)} )); "
            f"while [ $(date +%s) -lt $end ]; do "
            f'if [ -e "{port}" ]; then '
            f'stty -F "{port}" {int(baud)} raw -echo clocal 2>/dev/null; '
            f'timeout 2 cat "{port}" 2>/dev/null; '
            f"fi; sleep 0.05; done | head -c {int(max_bytes)}"
        )
        try:
            result = await self._run(["bash", "-c", script], check=False, timeout=seconds + 15)
        except TimeoutError:
            return ""
        return result.stdout or ""

    async def force_download_via_reset(
        self,
        *,
        attempts: int = 40,
        timeout: float = 8.0,
        poll_s: float = 0.05,
        on_line: Any | None = None,
    ) -> bool:
        """Drive a boot-looping native-USB chip into the ROM download loader.

        For the ``blank_or_corrupt_flash`` / boot-loop state the 1200-baud touch
        is useless (there's no app to honour it) and ``--before no_reset`` can
        never sync (the ROM never sits in the download loader on its own — it
        normal-boots, fails, watchdog-resets ~every 2s). The fix is esptool's
        USB-Serial/JTAG reset (``--before default_reset``), which pulls IO0 low
        at reset so the chip comes up in the download loader and *holds* there
        (``--after no_reset`` — the watchdog stops). Tight retry loop (50ms
        poll) because each attempt must open the port inside a ~2s up-window;
        the reset itself is sub-second so it lands quickly. Returns True once a
        ``flash_id`` syncs (chip now held in download), else False.

        This is the *rectification for that one state* — the normal app-mode
        route stays the 1200-baud touch; default_reset is not used there.
        """

        def _log(msg: str) -> None:
            if on_line is not None:
                try:
                    on_line(msg)
                except Exception:  # noqa: BLE001
                    pass

        for i in range(max(1, attempts)):
            result = None
            try:
                result = await self._run(
                    self._base_argv(before="default_reset", after="no_reset")
                    + ["--connect-attempts", "1", "flash_id"],
                    check=False,
                    timeout=timeout,
                )
            except TimeoutError:
                await self._free_serial_port()
            if result is not None:
                text = (result.stdout or "") + (result.stderr or "")
                if result.exit_status == 0 and ("Chip" in text or "MAC:" in text):
                    _log(f"download mode entered via USB-JTAG reset (attempt {i + 1})")
                    return True
            await asyncio.sleep(poll_s)
        _log(f"USB-JTAG reset did not catch the chip in {attempts} attempts")
        return False

    async def probe(self) -> ChipInfo:
        """Run ``flash_id`` and parse chip family + MAC + flash size from one call."""
        result = await self._run_esptool(self._base_argv() + ["flash_id"])
        text = result.stdout or ""
        family = parse_chip_family(text) or "ESP32"
        return ChipInfo(
            family=family,
            mac=parse_mac(text),
            flash_bytes=parse_flash_size_bytes(text),
            raw={"flash_id": text},
        )

    async def erase(self, *, before: str | None = None, after: str | None = None) -> None:
        """Run ``erase_flash``.

        Pass ``after="no_reset"`` to keep the chip in the download ROM so a
        follow-up ``flash()`` doesn't have to re-enter the bootloader (and the
        USB device doesn't re-enumerate) between the two steps. Pass
        ``before="no_reset"`` when the chip is *already* in the ROM (e.g. after
        ``enter_download_mode``): a native-USB S3's USB-Serial/JTAG drops out of
        download mode if esptool toggles its default reset, so the erase→flash→
        verify chain must all skip the reset.
        """
        await self._run_esptool(self._base_argv(before=before, after=after) + ["erase_flash"])

    async def flash(
        self, artifact: Artifact, *, before: str | None = None, after: str | None = None
    ) -> FlashResult:
        """Write ``artifact.path`` to flash at ``artifact.offset`` (default 0x0).

        For multi-segment images (bootloader + partitions + app), call this
        once per segment with the appropriate offset, OR pass a single
        ``combined_bin`` artifact at offset 0 (esptool merges those
        automatically). ``after`` overrides the post-write reset behaviour
        (default = esptool's ``hard_reset``); use ``"no_reset"`` to leave the
        chip in the bootloader for a subsequent ``verify()``.
        """
        offset = artifact.offset if artifact.offset is not None else 0x0
        offset_hex = f"0x{offset:X}"
        argv = self._base_argv(before=before, after=after) + [
            "write_flash",
            offset_hex,
            artifact.path,
        ]
        t0 = time.monotonic()
        result = await self._run_esptool(argv)
        elapsed = time.monotonic() - t0
        return FlashResult(
            bytes_written=parse_wrote_bytes(result.stdout or ""),
            elapsed_s=elapsed,
            raw_stdout=result.stdout or "",
            raw_stderr=result.stderr or "",
        )

    async def verify(
        self, artifact: Artifact, *, before: str | None = None, after: str | None = None
    ) -> str:
        """Run ``verify_flash`` to confirm flash contents match ``artifact``.

        Returns the tool stdout on success; raises :class:`FlasherToolFailed`
        (via ``_run``'s ``check=True``) when the on-chip digest does not match,
        which the orchestrator treats as a flash-verification failure. Pass
        ``before="no_reset"`` / ``after="no_reset"`` to read back without
        resetting the chip out of the ROM (matches the erase/flash steps in a
        no-reset cycle — the only reboot is the deliberate power-cycle stage).
        """
        offset = artifact.offset if artifact.offset is not None else 0x0
        offset_hex = f"0x{offset:X}"
        argv = self._base_argv(before=before, after=after) + [
            "verify_flash",
            offset_hex,
            artifact.path,
        ]
        result = await self._run_esptool(argv)
        return result.stdout or ""

    async def soft_reset(self) -> None:
        """Reboot the chip into its application via esptool (power-cycle fallback
        for a DUT with no solenoid channel mapped).

        Prefers ``--after watchdog_reset``: the on-chip RTC-watchdog reboot works on
        native-USB ESP32-S2/-S3/-C3 boards (e.g. the QT Py S3) whose RTS/DTR are NOT
        wired to EN/IO0 — there ``hard_reset`` toggles lines that go nowhere and the
        chip never reboots (so the freshly-written secrets are never re-read and the
        device never checks in). Falls back to ``--after hard_reset`` for boards
        where the watchdog reset isn't supported / doesn't take. ``flash_id`` is just
        the carrier command that makes esptool connect and then apply ``--after``.
        Not a true power cycle (PSRAM/peripheral state survives), but it boots the
        freshly-flashed firmware.
        """
        try:
            await self._run_esptool(self._base_argv(after="watchdog_reset") + ["flash_id"])
        except FlasherError:
            # watchdog_reset unsupported / didn't take → classic RTS/DTR reset.
            # (Both attempts are captured in the flash.log transcript.)
            await self._run_esptool(self._base_argv(after="hard_reset") + ["flash_id"])

    async def bootloader_touch_1200(self, *, settle_s: float = 2.0) -> None:
        """Issue a 1200-baud touch on ``self.port`` to request bootloader entry.

        Runs ``stty -F <port> 1200`` then waits ``settle_s`` for the device to
        re-enumerate. No-op-safe to call on chips that ignore it. ``check=False``:
        a touch on a device mid-re-enumeration fails with an "Input/output error"
        that must NOT abort the surrounding touch loop — the next probe decides.
        """
        await self._run(stty_1200_touch_argv(self.port), check=False)
        if settle_s > 0:
            await asyncio.sleep(settle_s)

    async def reset(self, *, into: Literal["bootloader", "application"]) -> None:
        """esptool drives DTR/RTS inline — bootloader entry needs no external step.

        After every esptool invocation the chip resets back into the
        application via ``--after hard_reset`` (esptool's default), so
        ``into="application"`` is also a no-op once a flash or erase has
        run. Implemented as a stub to satisfy the Protocol; callers can
        still invoke it without worrying about which family they have.
        """
        return None
