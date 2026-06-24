"""Composable firmware-bench stages.

The flash/erase/touch/verify/reset cycle is expressed as an **ordered list of
stages** rather than a hardcoded sequence, so the UI/API can offer each step as
a separate field and operators can add, drop, reorder, or repeat them (e.g.
flash several images at different offsets). Each stage is a small dict::

    {"type": "flash", "flasher": "esptool", "offset": "0x0", "after": "no_reset"}

and dispatches to an existing adapter — :class:`EsptoolFlasher`,
:class:`PioUploadFlasher`, :class:`SolenoidHubAdapter`, ... — so "use an
existing adapter" is just another stage type. New mechanisms (the MSC
secrets writer, tinyuf2-install, picotool, ...) drop in by registering a
handler in :data:`STAGE_HANDLERS` without touching the orchestrator.

A stage handler is ``async (stage: dict, ctx: BenchContext) -> None``; it raises
:class:`StageError` (or any flasher error) to abort the pipeline. Handlers log
human-readable progress through :meth:`BenchContext.log_line`, which the
orchestrator wires to job events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from typing import Any, Optional

from hil_controller.adapters.flashers.base import Artifact, FlasherError
from hil_controller.adapters.flashers.bossac import SAMD51_APP_OFFSET, BossacFlasher
from hil_controller.adapters.flashers.esptool import EsptoolFlasher, classify_boot_state
from hil_controller.adapters.flashers.pio_upload import PioUploadFlasher
from hil_controller.adapters.flashers.uf2_msc import Uf2MscFlasher
from hil_controller.adapters.msc_secrets import (
    MscError,
    read_msc_files,
    render_secrets_json,
    resolve_msc_device,
    write_secrets_to_msc,
)
from hil_controller.adapters.solenoid_hub import SolenoidHubAdapter, SolenoidHubError
from hil_controller.adapters.ws_i2c_inject import WsI2cInjectError, WsI2cProbeInjector
from hil_controller.adapters.ws_signal_inject import WsInjectError, WsSignalInjector
from hil_controller.redact import mask_values

#: secrets.json keys whose VALUES are credentials to mask in logs (last-4). The
#: username (anonymous / public playground) and routing fields stay visible.
_SECRET_KEY_HINTS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "PAT")

log = logging.getLogger(__name__)


def _now_iso() -> str:
    """UTC wall-clock to millisecond precision, shared format across all bench
    logs (serial.log / flash.log / protomq.log) so events line up."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


#: Allow-list of command-output substrings surfaced to the LIVE job log when a job
#: requests ``log_level: "filtered"`` (the default ``"all"`` streams everything).
#: A line is shown if it contains ANY of these. This is the aggressive
#: low-verbosity view — the downloadable flash.log asset is always complete, so
#: prune/extend this freely without losing data. **To update the filtered view,
#: edit this list** (see docs/firmware-bench-logging.md). Keep it to lines an
#: operator wants at a glance: chip identity, flash/erase milestones, verify.
NOTABLE_LIVE_LINES = (
    "Chip is",
    "Chip type",
    "MAC:",
    "Detected flash size",
    "Crystal",
    "Features:",
    "Stub",
    "Configuring flash",
    "Erasing",
    "erased successfully",
    "Flash memory erased",
    "Writing at",
    "Wrote ",
    "Compressed",
    "Hash of data verified",
    "Verifying",
    "verify OK",
    "Leaving",
    "Hard resetting",
    "Staying in bootloader",
)


class StageError(RuntimeError):
    """A bench stage failed or was misconfigured."""


class HostUsbWedgedError(StageError):
    """``enter_bootloader`` exhausted every recovery (1200-touch → USB-JTAG reset
    → generous hub power-cycle) and the device's by-path serial node still never
    appeared. That signature — a DUT absent from the bus that no power-cycle
    brings back — points at a wedged host USB stack (the Pi's ``dwc_otg``, which
    is NOT runtime-rebindable) rather than a stuck board, so the controller can
    react by flagging the host for a reboot instead of just failing the job."""


class _RecordingTransport:
    """Transport proxy that records every ``exec`` into a sink, then delegates.

    Wrapping ``dut_transport`` / ``hub_transport`` makes *every* CLI command a
    stage runs land in the flash.log transcript — not just esptool, but the
    solenoid power-cycle, the 1200-baud ``stty`` touch, ``udisksctl``/``mount``,
    and the ``tee`` of secrets.json. So the operator sees the equivalent
    command line for every step (the "verbose output" the bench promises),
    without each adapter having to thread its output back by hand. Non-``exec``
    attributes (``copy_to``, ``copy_from``, …) pass straight through.
    """

    def __init__(self, inner: Any, record: Callable[[list[str], Any], None]) -> None:
        self._inner = inner
        self._record = record

    async def exec(self, argv: list[str], **kwargs: Any) -> Any:
        result = await self._inner.exec(argv, **kwargs)
        try:
            self._record(list(argv), result)
        except Exception:  # noqa: BLE001 — a transcript sink must never break a stage
            log.warning("bench transcript sink raised", exc_info=True)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# --------------------------------------------------------------------------- #
# Shared context                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class BenchContext:
    """Resources shared across stages in one firmware-bench run.

    ``dut_transport`` runs the flash/serial/MSC steps (the host physically
    holding the DUT). ``hub_transport`` runs the solenoid/MCP23017 power
    control — usually the same host, but kept separate so a future split bench
    can differ.

    A board enumerates *differently* in flash mode versus running its
    application, so two serial paths are tracked separately:

    * ``flash_serial_port`` — the bootloader/flash-mode ``/dev/serial/by-id/...``
      esptool/pio drive (used by the flash/erase/verify/touch stages).
    * ``log_serial_port`` — the application-mode CDC path that appears *after*
      flash + reboot, which the orchestrator attaches serial capture to.

    ``msc_filter`` locates the device's FAT mass-storage volume under
    ``/dev/disk/by-id`` (the ``write_secrets_msc`` stage resolves it live, since
    the volume only enumerates once the app is running). Each filter is supplied
    by the job (override) or the DUT profile assigned from the usbip page; per
    ``feedback_never_filter_usb_by_vid`` they match iSerial / by-id / label,
    never VID. ``artifact`` is the default image; individual ``flash`` stages
    may override ``path``/``offset`` to write additional images.
    """

    dut_transport: Any
    hub_transport: Any
    flash_serial_port: str
    log_serial_port: str = ""
    msc_filter: str = ""
    device: dict[str, Any] = field(default_factory=dict)
    artifact: Artifact | None = None
    workspace_dir: str = ""
    pio_env: str = ""
    esptool_chip: str = "auto"
    esptool_baud: int = 921600
    #: SAM application offset for the bossac flasher (0x4000 SAMD51 / 0x2000 SAMD21).
    bossac_offset: int = SAMD51_APP_OFFSET
    sudo: bool = False
    emit: Callable[[str], None] | None = None
    # secrets.json inputs — credentials are flat values keyed by convention
    # (IO_USERNAME / IO_KEY / WIFI_SSID / WIFI_PASSWORD); protomq_host/port are
    # the broker the freshly-flashed firmware should connect to (the port is
    # filled in after protomq launches and reports it).
    secrets: dict[str, str] = field(default_factory=dict)
    protomq_host: str = ""
    protomq_port: int = 0
    #: Full command transcript (argv + stdout/stderr + exit) of every command a
    #: stage runs, for a downloadable, self-verifiable flash.log.
    transcript: list[dict[str, Any]] = field(default_factory=list)
    #: Orchestrator-supplied hooks the pipeline can fire mid-cycle so resources
    #: spin up at the right moment: ``launch_protomq`` stands up the broker
    #: (returning ``(host, port)``) only once the chip is erased and about to be
    #: flashed; ``start_serial`` attaches serial capture *before* the reboot so
    #: the boot log is caught. Both are optional — the matching stage no-ops if
    #: unset.
    launch_protomq: Callable[[], Awaitable[tuple[str, int]]] | None = None
    start_serial: Callable[[], Awaitable[None]] | None = None
    #: Release / re-take the serial port around an esptool op that needs it. On a
    #: no-solenoid host the power-cycle fallback is an esptool reset over the DUT's
    #: SINGLE CDC port — which the running serial capture is holding, so they fight
    #: ("Could not configure port"). pause_serial drops the capture's port lock for
    #: the reset; resume_serial re-attaches it after, catching the boot/checkin log.
    #: No-ops when no capture is running (e.g. solenoid hosts never need them).
    pause_serial: Callable[[], Awaitable[None]] | None = None
    resume_serial: Callable[[], Awaitable[None]] | None = None
    #: Controller-local path of the live serial.log the capture writes to. When
    #: set, the inject stage watches it for a reset banner as a fast, unambiguous
    #: reboot signal (the device resets within ~1-2s of a crash, long before it
    #: can reconnect WiFi+MQTT to re-checkin).
    serial_log_path: str = ""
    #: Verbosity of the LIVE streaming job log (the event feed CI tails). One of:
    #:   "all"      — emit every stdout+stderr line of every command (default).
    #:   "filtered" — emit only the :data:`NOTABLE_LIVE_LINES` allow-list + a summary.
    #: The downloadable flash.log transcript is ALWAYS full verbosity regardless.
    #: Set per job via the ``log_level`` param (see docs/firmware-bench-logging.md).
    stream_log_level: str = "all"

    def __post_init__(self) -> None:
        # Route both transports through the recorder so every command — esptool,
        # solenoid, stty, udisksctl/mount, tee — is captured in the transcript.
        self.dut_transport = _RecordingTransport(self.dut_transport, self.record)
        self.hub_transport = _RecordingTransport(self.hub_transport, self.record)

    def _secret_values(self) -> list[str]:
        """Credential VALUES to mask in any logged text (read live from secrets).

        A value is sensitive if its key name looks like a credential
        (``*KEY``/``*PASSWORD``/``*TOKEN``/…) — so IO_KEY and WIFI_PASSWORD are
        masked, while IO_USERNAME / WIFI_SSID / routing stay readable. Computed
        per-call so anonymous creds derived just before the run are covered.
        """
        return [
            v
            for k, v in (self.secrets or {}).items()
            if v and any(h in k.upper() for h in _SECRET_KEY_HINTS)
        ]

    def log_line(self, msg: str) -> None:
        # Keep the message (and any command/arg it carries) intact, but mask any
        # credential value down to its last 4 chars before it reaches the log,
        # the live event stream, the API, or a downloaded asset.
        msg = mask_values(msg, self._secret_values())
        log.info("[bench] %s", msg)
        if self.emit is not None:
            try:
                self.emit(msg)
            except Exception:  # noqa: BLE001 — a log sink must never break a stage
                log.warning("bench log sink raised", exc_info=True)

    def make_flasher(self, which: str) -> Any:
        """Build the flasher backing a stage: ``esptool`` (default), ``pio``, or ``bossac``.

        The flasher runs on the recording ``dut_transport``, so every command
        it issues lands in the transcript without any per-flasher wiring.
        """
        if which == "pio":
            if not self.workspace_dir:
                raise StageError("pio-upload stage needs a workspace_dir (PlatformIO project root)")
            flasher: Any = PioUploadFlasher(
                transport=self.dut_transport,
                port=self.flash_serial_port,
                workspace_dir=self.workspace_dir,
                pio_env=self.pio_env,
                sudo=self.sudo,
            )
        elif which == "bossac":
            flasher = BossacFlasher(
                transport=self.dut_transport,
                port=self.flash_serial_port,
                sudo=self.sudo,
                app_offset=self.bossac_offset,
            )
        elif which == "uf2-msc":
            # Mounting the bootloader MSC volume always needs root, regardless of
            # the job's sudo flag, so force sudo here.
            flasher = Uf2MscFlasher(
                transport=self.dut_transport,
                port=self.flash_serial_port,
                sudo=True,
                app_offset=self.bossac_offset,
            )
        elif which in ("esptool", "", None):
            flasher = EsptoolFlasher(
                transport=self.dut_transport,
                port=self.flash_serial_port,
                chip=self.esptool_chip,
                baud=self.esptool_baud,
                sudo=self.sudo,
            )
        else:
            raise StageError(
                f"unknown flasher {which!r} (expected 'esptool', 'pio', 'bossac', or 'uf2-msc')"
            )
        return flasher

    def record(self, argv: list[str], result: Any) -> None:
        """Capture one command's argv+stdout+stderr; stream it per ``stream_log_level``.

        The flash.log transcript ALWAYS gets the full output. The LIVE feed (CI logs)
        gets either everything (``"all"``, default) or just the
        :data:`NOTABLE_LIVE_LINES` allow-list (``"filtered"``). Either way it's one
        UTC-ms-timestamped event per command.
        """
        import shlex

        # Mask credential values to last-4 in BOTH the stored transcript (→ the
        # downloadable flash.log asset) and the live stream below. The command +
        # its args are preserved; only secret values are reduced — including the
        # secrets.json body that ``tee`` echoes to stdout.
        secrets = self._secret_values()
        cmd = mask_values(" ".join(shlex.quote(a) for a in argv), secrets)
        out = mask_values((getattr(result, "stdout", "") or "").rstrip(), secrets)
        err = mask_values((getattr(result, "stderr", "") or "").rstrip(), secrets)
        rc = getattr(result, "exit_status", 0)
        self.transcript.append(
            {"at": _now_iso(), "cmd": cmd, "exit": rc, "stdout": out, "stderr": err}
        )

        head = f"{_now_iso()}  $ {cmd}  → exit {rc}"
        if self.stream_log_level == "filtered":
            # Aggressive allow-list: only the lines worth seeing at a glance, plus
            # the failing stderr tail. (Full detail is in the flash.log asset.)
            notable = [ln for ln in out.splitlines() if any(k in ln for k in NOTABLE_LIVE_LINES)]
            lines = [head]
            lines += [f"    {ln}" for ln in notable[:12]]
            if rc != 0 and err:
                lines.append("    [stderr] " + err.splitlines()[-1][:160])
        else:  # "all" (default) — everything
            lines = [head]
            lines += [f"    {ln}" for ln in out.splitlines()]
            lines += [f"    [stderr] {ln}" for ln in err.splitlines()]
        self.log_line("\n".join(lines))

    def transcript_text(self) -> str:
        """Render the full transcript as a human-readable flash.log."""
        blocks = []
        for i, e in enumerate(self.transcript, 1):
            at = e.get("at", "")
            head = f"===== command {i}{f' [{at}]' if at else ''}: exit {e['exit']} ====="
            b = [head, f"$ {e['cmd']}"]
            if e["stdout"]:
                b.append(e["stdout"])
            if e["stderr"]:
                b.append("--- stderr ---\n" + e["stderr"])
            blocks.append("\n".join(b))
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    def stage_artifact(self, stage: dict[str, Any]) -> Artifact:
        """Resolve the image a flash/verify stage targets.

        A stage may carry its own ``path``/``offset`` (so successive stages can
        write different images); otherwise it falls back to the context's
        default ``artifact``.
        """
        path = stage.get("path") or (self.artifact.path if self.artifact else None)
        if not path:
            raise StageError(
                f"{stage.get('type')!r} stage has no image: set 'path' or a default artifact"
            )
        offset = stage.get("offset")
        if offset is None:
            offset = self.artifact.offset if self.artifact else 0
        elif isinstance(offset, str):
            offset = int(offset, 0)  # accept "0x10000" or "65536"
        return Artifact(
            path=path, kind=stage.get("kind", "bin"), offset=offset, label=stage.get("label")
        )


Handler = Callable[[dict[str, Any], BenchContext], Awaitable[None]]


# --------------------------------------------------------------------------- #
# Stage handlers (each reuses an existing adapter)                            #
# --------------------------------------------------------------------------- #


async def _diagnose_boot_state(
    stage: dict[str, Any], ctx: BenchContext, flasher: Any = None
) -> str:
    """Read the DUT's serial boot log and classify the failure state.

    Captures serial across the board's reset cycles (reconnecting, 50ms poll)
    and matches known ROM/bootloader/app strings — ``invalid header`` (blank/
    corrupt flash), a panic, the reset reason (``TG0WDT_SYS_RST``, ...), an app
    banner, or "waiting for download". Returns the state name and logs it; this
    is what tells the recovery which rectification to apply.
    """
    flasher = flasher or ctx.make_flasher("esptool")
    seconds = float(stage.get("diagnose_s", 15.0))
    # Boot-log baud is the ROM/app console rate (115200), NOT esptool_baud.
    baud = int(stage.get("serial_baud", 115200))
    ctx.log_line(f"diagnose: reading boot serial for {seconds:.0f}s to classify state")
    text = await flasher.read_boot_log(seconds=seconds, baud=baud)
    info = classify_boot_state(text)
    rr = info.get("reset_reason")
    ctx.log_line(f"detected boot state: {info['state']}" + (f" (reset reason: {rr})" if rr else ""))
    return str(info["state"])


async def _stage_diagnose(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Standalone detection stage: classify the board's current boot state."""
    await _diagnose_boot_state(stage, ctx)


async def _stage_enter_bootloader(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Get the DUT into the ROM download mode for esptool — detect, then rectify.

    Order matters and is the opposite of "reboot first":

    1. If esptool already syncs (``--before no_reset``), we're done.
    2. **App route** — loop the **1200-baud touch**, which flips a running
       TinyUSB app into the USB-Serial/JTAG ROM (same ``by-path``, different
       ``by-id``). This is the normal path and the *only* one used for healthy
       app-mode boards.
    3. If the touch never lands, **diagnose**: read the boot serial and classify
       (``invalid header`` = blank/corrupt flash boot-loop, a panic, etc.).
    4. **Rectify the blank/boot-loop state with the USB-Serial/JTAG reset**
       (``--before default_reset``): the touch can't help a board with no app,
       but esptool's reset pulls IO0 low so the chip enters the download loader
       and *holds* (the watchdog stops). Scoped to this state — not the app
       route.
    5. Only if that also fails do we power-cycle via the hub and retry.

    All esptool work that follows uses ``--before no_reset`` (the ROM drops out
    of download mode if reset is toggled once it's there), which the default
    stages set.

    For ``flasher: bossac`` / ``uf2-msc`` (SAM/SAMD51) the entry is the simpler
    1200-baud double-tap into the UF2/SAM-BA bootloader — no ROM/JTAG concept —
    handled by :func:`_enter_bootloader_sam`.
    """
    if stage.get("flasher") in ("bossac", "uf2-msc"):
        await _enter_bootloader_sam(stage, ctx)
        return

    flasher = ctx.make_flasher("esptool")
    attempts = int(stage.get("attempts", 8))
    settle_s = float(stage.get("settle_s", 3.0))

    if await flasher.is_in_download_mode():
        ctx.log_line("already in ROM download mode — no touch needed")
        return

    ctx.log_line(f"app mode: 1200-baud touch loop to enter download mode (≤{attempts} tries)")
    try:
        await flasher.enter_download_mode(
            attempts=attempts, settle_s=settle_s, on_line=ctx.log_line
        )
        ctx.log_line("device is in ROM download mode")
        return
    except FlasherError as exc:
        ctx.log_line(f"touch loop did not reach download mode ({exc})")

    # Detect why the touch failed, then rectify the blank/boot-loop state via the
    # USB-Serial/JTAG reset (the touch is useless on a board with no app).
    if stage.get("diagnose", True):
        state = await _diagnose_boot_state(stage, ctx, flasher)
    else:
        state = "unknown"
    if state in ("blank_or_corrupt_flash", "bootloader_error", "app_panic", "unknown"):
        ctx.log_line("rectify: forcing download via USB-JTAG reset (--before default_reset)")
        if await flasher.force_download_via_reset(
            attempts=int(stage.get("reset_attempts", 40)), on_line=ctx.log_line
        ):
            ctx.log_line("device is in ROM download mode (via USB-JTAG reset)")
            return

    if not stage.get("power_cycle", True):
        raise StageError(
            "could not enter ROM download mode via touch or USB-JTAG reset (recovery disabled)"
        )
    await _recover_download_via_hub(stage, ctx, reason="touch + USB-JTAG reset failed")


async def _recover_download_via_hub(
    stage: dict[str, Any], ctx: BenchContext, *, reason: str
) -> None:
    """Cold-boot via the hub switch, then re-enter the ROM download mode.

    Used both when the 1200-baud touch never lands and to recover a wedged
    USB-Serial/JTAG write channel mid-flash: a power-cycle gives esptool a fresh
    USB endpoint. An *erased* S3 with no valid app comes back up directly in the
    ROM, so the follow-up ``enter_download_mode`` usually finds it already there;
    otherwise the 1200-touch flips it. Raises :class:`StageError` if the DUT has
    no solenoid channel to cycle.
    """
    channel = ctx.device.get("solenoid_channel")
    if channel is None:
        raise StageError(f"{reason}: no solenoid channel to power-cycle for recovery")
    # Recovery means clearing a wedge, so depower generously (long OFF hold +
    # depower settle) rather than a quick latch toggle — a brief off often won't
    # drop a wedged native-USB board's power. Tunable via the stage.
    off_s = float(stage.get("recover_off_s", stage.get("off_s", 3.0)))
    post_off = float(stage.get("recover_post_off_s", 3.0))
    # Default ~0 (was a legacy 5.0 that defeated the tight-loop ROM-window catch
    # below): only the app-mode 1200-touch fallback uses this. Raise it only for a
    # board that must finish booting before that touch.
    boot_settle = float(stage.get("boot_settle_s", 0.01))
    attempts = int(stage.get("attempts", 8))
    settle_s = float(stage.get("settle_s", 3.0))
    hub = SolenoidHubAdapter(transport=ctx.hub_transport, sudo=ctx.sudo)
    ctx.log_line(
        f"recovery ({reason}): generous power-cycle solenoid ch {channel} "
        f"(off {off_s}s + depower {post_off}s, boot settle {boot_settle}s)"
    )
    try:
        # NO post-on settle here: the USB-Serial/JTAG reset below must catch the
        # ~1-2s ROM up-window that opens immediately after power-on, BEFORE any app
        # (CircuitPython, a healthy WS) boots and closes it. force_download_via_reset
        # is itself a tight retry loop that does the timing; sleeping boot_settle
        # first would miss the window on a native-USB board that boots an app in
        # ~1.6s. boot_settle is reserved for the app-mode 1200-touch fallback.
        await hub.power_cycle(int(channel), off_s=off_s, settle_s=0.0, post_off_s=post_off)
    except SolenoidHubError as exc:
        raise StageError(f"hub power-cycle failed on channel {channel}: {exc}") from exc
    flasher = ctx.make_flasher("esptool")
    # A freshly power-cycled erased/blank board boot-loops in normal mode — the
    # USB-Serial/JTAG reset catches it; fall back to the 1200-touch for an app.
    if await flasher.force_download_via_reset(
        attempts=int(stage.get("reset_attempts", 40)), on_line=ctx.log_line
    ):
        ctx.log_line("device is in ROM download mode (after hub recovery, via USB-JTAG reset)")
        return
    # Not caught in the ROM window → it booted an app. Give it boot_settle to come
    # up fully, then take the app-mode 1200-touch route.
    if boot_settle > 0:
        await asyncio.sleep(boot_settle)
    try:
        await flasher.enter_download_mode(
            attempts=attempts, settle_s=settle_s, on_line=ctx.log_line
        )
    except FlasherError as exc:
        # Recovery fully exhausted. If the device's serial node never even
        # appeared on the host, this is a wedged host USB stack (dwc_otg) — a
        # power-cycle can't fix it, only a host reboot can — so raise the distinct
        # signal instead of a generic StageError.
        port = ctx.flash_serial_port or ctx.log_serial_port
        if port and not await _serial_node_present(ctx, port):
            raise HostUsbWedgedError(
                f"{reason}: serial node {port!r} absent after full recovery "
                "(touch + USB-JTAG reset + hub power-cycle) — host USB stack likely wedged"
            ) from exc
        raise
    ctx.log_line("device is in ROM download mode (after hub recovery)")


async def _enter_bootloader_sam(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Get a SAM (SAMD51) DUT into its UF2 / SAM-BA bootloader.

    Shared by ``flasher: bossac`` (SAM-BA, confirmed via ``bossac -i``) and
    ``flasher: uf2-msc`` (UF2 bootloader, confirmed via the MSC drive appearing).
    SAM boards have no ROM-download / USB-JTAG mode: the only entry is the
    1200-baud double-tap, which reboots a running app into the resident
    bootloader (a freshly-erased board is already there). If the touch loop never
    lands and a solenoid channel exists, power-cycle once and retry — there is no
    esptool-style ``default_reset`` fallback for SAM.
    """
    which = stage.get("flasher", "uf2-msc")
    flasher = ctx.make_flasher(which)
    attempts = int(stage.get("attempts", 3))
    settle_s = float(stage.get("settle_s", 2.0))
    catch_s = float(stage.get("catch_s", 30.0))
    channel = ctx.device.get("solenoid_channel")
    # uf2-msc takes the tight 1200-touch hammer (catch_s); bossac (SAM-BA) the plain loop.
    supports_catch = which == "uf2-msc"

    def _kw() -> dict[str, Any]:
        kw: dict[str, Any] = {"attempts": attempts, "settle_s": settle_s, "on_line": ctx.log_line}
        if supports_catch:
            kw["catch_s"] = catch_s
        return kw

    if await flasher.is_in_bootloader():
        ctx.log_line(f"already in {which} bootloader — no touch needed")
        return

    # Tier 1: hammer the running/booting (possibly bootlooping) device into the
    # bootloader — the tight continuous 1200-touch catches its brief CDC window.
    ctx.log_line(f"tier1: 1200-touch hammer into {which} bootloader")
    try:
        await flasher.enter_bootloader(**_kw())
        ctx.log_line(f"device is in {which} bootloader")
        return
    except FlasherError as exc:
        ctx.log_line(f"tier1 hammer did not reach {which} bootloader ({exc})")

    if not stage.get("power_cycle", True) or channel is None:
        raise StageError(
            f"could not enter {which} bootloader via 1200-touch hammer "
            "(recovery disabled or no solenoid channel to power-cycle)"
        )
    hub = SolenoidHubAdapter(transport=ctx.hub_transport, sudo=ctx.sudo)
    off_s = float(stage.get("recover_off_s", 3.0))

    # Tier 2: power-cycle for a fresh boot window, then hammer (the hammer starts
    # immediately so it's effectively "power on into a running touch loop"). A few
    # rounds. NOTE: samd51_uf2's power double-tap is deliberately NOT used — a
    # power-off clears the RAM magic value the SAMD double-tap relies on (only a
    # reset preserves it, and the solenoid controls power, not reset).
    for r in range(int(stage.get("power_cycle_rounds", 2))):
        ctx.log_line(f"tier2 round {r + 1}: power-cycle solenoid ch {channel}, then hammer")
        try:
            await hub.power_cycle(int(channel), off_s=off_s, settle_s=0.0)
        except SolenoidHubError as exc:
            raise StageError(f"hub power-cycle failed on channel {channel}: {exc}") from exc
        try:
            await flasher.enter_bootloader(**_kw())
            ctx.log_line(f"device is in {which} bootloader (after power-cycle round {r + 1})")
            return
        except FlasherError as exc:
            ctx.log_line(f"tier2 round {r + 1} did not reach {which} bootloader ({exc})")
    raise StageError(f"could not enter {which} bootloader after hammer + power-cycle recovery")


async def _stage_bootloader_touch(stage: dict[str, Any], ctx: BenchContext) -> None:
    flasher = ctx.make_flasher("esptool")  # the touch is a serial op, esptool owns the port
    settle = float(stage.get("settle_s", 2.0))
    ctx.log_line(f"1200-baud bootloader touch on {ctx.flash_serial_port} (settle {settle}s)")
    await flasher.bootloader_touch_1200(settle_s=settle)


async def _retry_step(
    ctx: BenchContext, stage: dict[str, Any], *, label: str, do: Callable[[], Awaitable[Any]]
) -> Any:
    """Run ``do()``, retrying on :class:`FlasherError` until it succeeds or at
    least ``min_retry_s`` (default 10s) has elapsed since the first attempt.

    Always makes at least one attempt; the device's USB-Serial/JTAG glitches
    transiently, so a quick re-try usually catches it. Re-raises the last error
    once the window closes (the flash stage then escalates to a power-cycle).
    """
    min_s = float(stage.get("min_retry_s", 10.0))
    backoff = float(stage.get("retry_backoff_s", 2.0))
    deadline = time.monotonic() + min_s
    attempt = 0
    while True:
        attempt += 1
        try:
            return await do()
        except FlasherError as exc:
            if time.monotonic() >= deadline:
                raise
            ctx.log_line(
                f"{label} attempt {attempt} failed ({exc}); retrying (≥{min_s:.0f}s window)"
            )
            await asyncio.sleep(backoff)


def _reset_flags(stage: dict[str, Any]) -> str:
    """Human-readable ``--before/--after`` suffix for a stage's log line."""
    parts = []
    if stage.get("before"):
        parts.append(f"--before {stage['before']}")
    if stage.get("after"):
        parts.append(f"--after {stage['after']}")
    return f" ({' '.join(parts)})" if parts else ""


async def _stage_erase(stage: dict[str, Any], ctx: BenchContext) -> None:
    which = stage.get("flasher", "esptool")
    before, after = stage.get("before"), stage.get("after")
    ctx.log_line(f"erase via {which}" + _reset_flags(stage))

    async def _do() -> None:
        flasher = ctx.make_flasher(which)
        if isinstance(flasher, EsptoolFlasher):
            await flasher.erase(before=before, after=after)
        else:
            await flasher.erase()

    await _retry_step(ctx, stage, label="erase", do=_do)


async def _stage_flash(stage: dict[str, Any], ctx: BenchContext) -> None:
    which = stage.get("flasher", "esptool")
    artifact = ctx.stage_artifact(stage)
    before, after = stage.get("before"), stage.get("after")

    async def _do() -> Any:
        flasher = ctx.make_flasher(which)
        if isinstance(flasher, EsptoolFlasher):
            return await flasher.flash(artifact, before=before, after=after)
        return await flasher.flash(artifact)

    # Two layers of resilience: _retry_step re-tries transient glitches for the
    # ≥min_retry_s window; if the write is genuinely wedged ("Write timeout" on
    # the USB-Serial/JTAG — re-running on the same dead endpoint won't help),
    # power-cycle (fresh USB endpoint) → re-enter download → retry, up to
    # recover_attempts. pio / channel-less DUTs just surface the error.
    recover_attempts = int(stage.get("recover_attempts", 2))
    attempt = 0
    while True:
        ctx.log_line(
            f"flash {artifact.path} @ 0x{(artifact.offset or 0):X} via {which}"
            + _reset_flags(stage)
        )
        try:
            result = await _retry_step(ctx, stage, label="flash", do=_do)
            if result.bytes_written:
                ctx.log_line(f"wrote {result.bytes_written} bytes in {result.elapsed_s:.1f}s")
            return
        except FlasherError as exc:
            attempt += 1
            recoverable = (
                which == "esptool"
                and attempt <= recover_attempts
                and ctx.device.get("solenoid_channel") is not None
                and stage.get("recover", True)
            )
            if not recoverable:
                raise
            ctx.log_line(
                f"flash failed after retries ({exc}); recovering (retry {attempt}/{recover_attempts})"  # noqa: E501
            )
            await _recover_download_via_hub(stage, ctx, reason="flash write wedged")


async def _stage_verify(stage: dict[str, Any], ctx: BenchContext) -> None:
    artifact = ctx.stage_artifact(stage)
    ctx.log_line(f"verify {artifact.path} @ 0x{(artifact.offset or 0):X}" + _reset_flags(stage))

    async def _do() -> None:
        flasher = ctx.make_flasher("esptool")  # only esptool implements verify_flash
        await flasher.verify(artifact, before=stage.get("before"), after=stage.get("after"))

    await _retry_step(ctx, stage, label="verify", do=_do)
    ctx.log_line("verify OK")


async def _stage_launch_protomq(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Stand up the per-session protomq broker, recording its ports on the ctx.

    Placed *after* erase and just before flash so a broker is never started for
    a run that fails to even erase the chip (which used to orphan a node on the
    MQTT port). The ``write_secrets_msc`` stage reads the host/port set here.
    """
    if ctx.launch_protomq is None:
        ctx.log_line("launch_protomq: no launcher bound on context; skipping")
        return
    if ctx.protomq_port:
        ctx.log_line(f"protomq already up on {ctx.protomq_host}:{ctx.protomq_port}; skipping")
        return
    host, port = await ctx.launch_protomq()
    ctx.protomq_host, ctx.protomq_port = host, int(port)


async def _stage_start_serial_log(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Attach serial capture *now* (before a following reboot) to catch bootlog.

    Run before the final power-cycle so the very first lines the freshly-flashed
    app prints on boot are captured, rather than starting capture only after the
    device has already rebooted.
    """
    if ctx.start_serial is None:
        ctx.log_line("start_serial_log: no serial starter bound on context; skipping")
        return
    await ctx.start_serial()


async def _serial_node_present(ctx: BenchContext, path: str) -> bool:
    """True if the DUT's by-path serial node currently exists on the DUT host."""
    if not path:
        return False
    try:
        res = await ctx.dut_transport.exec(["test", "-e", path])
    except Exception:  # noqa: BLE001 — a transport hiccup means "can't confirm present"
        return False
    return getattr(res, "exit_status", 1) == 0


async def _await_serial_node(
    ctx: BenchContext, path: str, *, present: bool, timeout_s: float
) -> bool:
    """Poll until the by-path serial node reaches the desired presence, or time out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await _serial_node_present(ctx, path) == present:
            return True
        await asyncio.sleep(0.5)
    return False


async def _stage_power_cycle(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Cold-boot the DUT via its solenoid channel; fall back to esptool reset.

    Drives the cycle by **detection, not fixed timers**: if the DUT's by-path
    serial node was present, power off and *await it disappearing* (proves the
    rail actually dropped — a brief off won't always depower a native-USB board),
    then power on and *await it re-enumerating* before proceeding. Falls back to
    the old timed ``power_cycle`` when no serial node is known or detection is
    disabled (``await_enumeration: false``). Per the no-solenoid-channel
    decision, an unmapped device degrades to an esptool soft reset with a warning
    rather than failing the run.
    """
    channel = ctx.device.get("solenoid_channel")
    if channel is None:
        ctx.log_line(
            "WARNING: device has no solenoid_channel mapped; falling back to "
            "esptool soft reset (not a true power cycle)"
        )
        # The esptool reset needs the DUT's single CDC port, which a running serial
        # capture is holding — pause it for the reset, then resume to catch the
        # boot/checkin log (no-op when no capture is active, e.g. before stage 5).
        if ctx.pause_serial is not None:
            await ctx.pause_serial()
        try:
            await ctx.make_flasher("esptool").soft_reset()
        finally:
            if ctx.resume_serial is not None:
                await ctx.resume_serial()
        return

    off_s = float(stage.get("off_s", 1.0))
    settle_s = float(stage.get("settle_s", 2.0))
    post_off_s = float(stage.get("post_off_s", 0.0))
    port = ctx.log_serial_port or ctx.flash_serial_port
    detect = bool(stage.get("await_enumeration", True)) and bool(port)
    hub = SolenoidHubAdapter(transport=ctx.hub_transport, sudo=ctx.sudo)

    if not detect:
        ctx.log_line(f"power-cycle solenoid channel {channel} (off {off_s}s, settle {settle_s}s)")
        try:
            await hub.power_cycle(int(channel), off_s=off_s, settle_s=settle_s)
        except SolenoidHubError as exc:
            raise StageError(f"power-cycle failed on channel {channel}: {exc}") from exc
        return

    gone_timeout = float(stage.get("disappear_timeout_s", 10.0))
    back_timeout = float(stage.get("reappear_timeout_s", 30.0))
    was_present = await _serial_node_present(ctx, port)
    ctx.log_line(
        f"power-cycle ch {channel}: device {'present' if was_present else 'absent'} "
        f"at {port} pre-cycle"
    )
    try:
        await hub.port_off(int(channel), hold_s=off_s, post_off_s=post_off_s)
        if was_present:
            if await _await_serial_node(ctx, port, present=False, timeout_s=gone_timeout):
                ctx.log_line(f"device disappeared after power-off (within {gone_timeout:.0f}s)")
            else:
                ctx.log_line(
                    f"WARNING: device still enumerated {gone_timeout:.0f}s after power-off "
                    "— the rail may not have dropped (check off_s/post_off_s)"
                )
        await hub.port_on(int(channel))
    except SolenoidHubError as exc:
        raise StageError(f"power-cycle failed on channel {channel}: {exc}") from exc

    if await _await_serial_node(ctx, port, present=True, timeout_s=back_timeout):
        ctx.log_line(f"device re-enumerated after power-on (within {back_timeout:.0f}s)")
        await asyncio.sleep(settle_s)  # let the app finish coming up post-enumeration
    else:
        ctx.log_line(f"WARNING: device did not re-enumerate within {back_timeout:.0f}s of power-on")


async def _stage_write_secrets_msc(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Drop ``secrets.json`` onto the DUT's MSC volume, pointing at protomq.

    Runs after flash + reboot (so the FAT volume has enumerated). The broker
    host/port default to the launched protomq's reported values; ``msc_filter``
    falls back to the DUT profile's filter on the context.
    """
    msc_filter = stage.get("msc_filter") or ctx.msc_filter
    io_url = stage.get("io_url") or ctx.protomq_host
    io_port = stage.get("io_port") or ctx.protomq_port
    if not io_url or not io_port:
        raise StageError(
            "write_secrets_msc needs a broker host+port — launch protomq before "
            "this stage, or set io_url/io_port on the stage"
        )
    body = render_secrets_json(
        io_url=io_url,
        io_port=int(io_port),
        io_username=ctx.secrets.get("IO_USERNAME", ""),
        io_key=ctx.secrets.get("IO_KEY", ""),
        wifi_ssid=ctx.secrets.get("WIFI_SSID", ""),
        wifi_password=ctx.secrets.get("WIFI_PASSWORD", ""),
    )
    # Log only non-secret routing info (never credentials/wifi).
    ctx.log_line(
        f"write secrets.json → MSC (io_url={io_url}, io_port={io_port}, filter={msc_filter!r})"
    )
    # The FAT MSC volume only enumerates a few seconds AFTER the app boots (it
    # mounts, checks the drive, then re-presents CDC+MSC), so the preceding
    # power-cycle's settle is rarely enough — wait for the volume to appear.
    attempts = int(stage.get("attempts", 8))
    settle_s = float(stage.get("settle_s", 3.0))
    for i in range(1, attempts + 1):
        try:
            await resolve_msc_device(ctx.dut_transport, msc_filter)
            break
        except MscError as exc:
            if i == attempts:
                raise StageError(
                    f"MSC volume for {msc_filter!r} never enumerated after {attempts} tries: {exc}"
                ) from exc
            ctx.log_line(
                f"waiting for MSC volume {msc_filter!r} to enumerate (attempt {i}/{attempts})"
            )
            await asyncio.sleep(settle_s)
    fname = stage.get("filename", "secrets.json")
    dev, mnt = await write_secrets_to_msc(
        ctx.dut_transport, msc_filter=msc_filter, secrets_json=body, filename=fname
    )
    ctx.log_line(f"{fname} written to {dev} ({mnt}); volume unmounted")


async def _stage_print_boot_log(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Print the board's boot log from its MSC volume, if/when it's stable.

    WipperSnapper writes ``wipper_boot_out.txt`` and CircuitPython writes
    ``boot_out.txt``; the default glob catches both regardless of the volume
    label (WIPPER / CIRCUITPY). Best-effort and never fatal: a board with no
    secrets reboots every ~30s (faster if it crashes) and only exposes its MSC
    after it has checked the drive, so the volume comes and goes — we retry a few
    times and, if it never settles, log that and carry on rather than fail the
    run. (Tune the globs/attempts per board in future runs.)
    """
    msc_filter = stage.get("msc_filter") or ctx.msc_filter
    if not msc_filter:
        ctx.log_line("print_boot_log: no msc_filter set; skipping")
        return
    globs = tuple(stage.get("globs") or ("*boot_out.txt",))
    attempts = int(stage.get("attempts", 4))
    settle_s = float(stage.get("settle_s", 4.0))

    for i in range(1, attempts + 1):
        try:
            files = await read_msc_files(ctx.dut_transport, msc_filter=msc_filter, globs=globs)
        except MscError as exc:
            ctx.log_line(f"print_boot_log: drive not ready ({exc}); attempt {i}/{attempts}")
            await asyncio.sleep(settle_s)
            continue
        if files:
            for path, content in files.items():
                name = path.rsplit("/", 1)[-1]
                ctx.log_line(f"── boot log: {name} ──\n{content or '(empty)'}")
            return
        ctx.log_line(f"print_boot_log: no {list(globs)} on volume yet; attempt {i}/{attempts}")
        await asyncio.sleep(settle_s)
    ctx.log_line("print_boot_log: boot log unavailable (drive never stabilised); continuing")


#: Lines that only appear in the serial log when the ESP32 has actually reset —
#: ROM banner, a reset-reason line, a fresh app boot (WiFi re-scan / re-register),
#: or a bootloop header. Scanned only in serial content written *after* the
#: pixelWrite is fired, so a pre-inject boot never false-positives.
_SERIAL_REBOOT_MARKERS = re.compile(
    r"ESP-ROM:|rst:0x[0-9a-fA-F]|WipperSnapper found these WiFi networks|"
    r"invalid header|Registering hardware with WipperSnapper"
)


def _serial_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


async def _await_serial_marker(path: str, marker: re.Pattern[str], timeout_s: float) -> bool:
    """Poll the controller-local serial.log for *marker* (anywhere) until *timeout_s*.

    Used by the serial ``verify_checkin`` mode — the WS registration banner only
    prints on a fully-successful checkin, so matching it anywhere in the captured
    serial is a clean PASS signal; a reboot-looping (never-connects) device never
    prints it → timeout → FAIL.
    """
    if not path:
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with open(path, errors="ignore") as fh:
                if marker.search(fh.read()):
                    return True
        except OSError:
            pass
        await asyncio.sleep(0.5)
    return False


async def _await_serial_reboot(path: str, start_offset: int, timeout_s: float) -> bool:
    """Poll the controller-local serial.log for a reset banner past *start_offset*."""
    if not path:
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with open(path, errors="ignore") as fh:
                fh.seek(start_offset)
                if _SERIAL_REBOOT_MARKERS.search(fh.read()):
                    return True
        except OSError:
            pass
        await asyncio.sleep(0.5)
    return False


async def _detect_reboot(
    injector: WsSignalInjector,
    uid: str,
    *,
    serial_path: str,
    serial_offset: int,
    timeout_s: float,
) -> tuple[bool, str]:
    """Race two reboot signals; return (rebooted, how) as soon as either fires.

    A crash resets the chip within ~1-2s (visible in serial immediately), but the
    device cannot re-checkin over MQTT until it reconnects WiFi+broker (often
    15-40s). Watching only MQTT with a short window misreports a real crash as
    "survived" — so we also watch the serial reset banner and take whichever
    proves a reboot first.
    """
    mqtt = asyncio.create_task(injector.observe_reboot(uid, timeout=timeout_s))
    serial = asyncio.create_task(_await_serial_reboot(serial_path, serial_offset, timeout_s))
    pending = {mqtt, serial}
    rebooted, how = False, "neither (survived)"
    try:
        while pending and not rebooted:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                try:
                    res = bool(t.result())
                except Exception:  # noqa: BLE001
                    res = False
                if res:
                    rebooted = True
                    how = "serial reset banner" if t is serial else "MQTT re-checkin"
    finally:
        for t in (mqtt, serial):
            if not t.done():
                t.cancel()
        await asyncio.gather(mqtt, serial, return_exceptions=True)
    return rebooted, how


async def _stage_inject_pixelwrite(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Inject a v1 ``pixelWrite`` at the checked-in DUT; record crash-vs-graceful.

    Drives the pixelWrite-to-uninitialised-strand regression through protomq:
    wait for the device to check in, fire ``signal.v1.PixelsRequest{ pixelWrite
    pin="D0", colour=200 }`` via ``POST /api/echo`` (the broker publishes it to
    ``<user>/wprsnpr/<uid>/signals/broker/pixel``), then watch MQTT for a fresh
    re-checkin — a reboot means the firmware CRASHED (release 1.0.0-beta.127);
    silence means it handled it gracefully (the #927 fix, beta.129+).

    The stage does NOT pass/fail on the reboot itself (the crash build is
    *expected* to reboot) — it logs a machine-greppable ``PIXELWRITE_VERDICT``
    line and records the exact injection in the transcript; the harness compares
    the two firmware builds. Requires protomq up (run ``launch_protomq`` first)
    and the DUT booted with secrets pointing at it.
    """
    if not ctx.protomq_host or not ctx.protomq_port:
        raise StageError(
            "inject_pixelwrite needs protomq running (launch_protomq before this stage)"
        )
    pin = str(stage.get("pin", "D0"))
    color = int(stage.get("color", 200))
    io_user = ctx.secrets.get("IO_USERNAME") or "hil"
    api_url = stage.get("protomq_api_url") or getattr(ctx, "protomq_api_url", "") or None
    injector = WsSignalInjector(
        broker_host=ctx.protomq_host,
        mqtt_port=ctx.protomq_port,
        api_url=api_url,
        io_username=io_user,
    )
    checkin_timeout = float(stage.get("checkin_timeout_s", 120.0))
    observe_s = float(stage.get("observe_s", 30.0))
    ctx.log_line(
        f"inject_pixelwrite: waiting ≤{checkin_timeout:.0f}s for DUT checkin on {io_user}/wprsnpr/#"
    )
    try:
        uid = await injector.wait_for_checkin(timeout=checkin_timeout)
    except WsInjectError as exc:
        raise StageError(f"inject_pixelwrite: cannot observe checkin ({exc})") from exc
    if not uid:
        raise StageError(
            f"inject_pixelwrite: no DUT checkin on {io_user}/wprsnpr/# within {checkin_timeout:.0f}s "  # noqa: E501
            "(device booted with secrets pointing at protomq?)"
        )
    ctx.log_line(
        f"inject_pixelwrite: device checked in (uid={uid}); firing v1 pixelWrite pin={pin} colour={color}"  # noqa: E501
    )
    # Mark the serial.log position *before* firing so the reboot watcher only
    # scans crash output, never the pre-inject boot.
    serial_path = getattr(ctx, "serial_log_path", "") or ""
    serial_offset = _serial_size(serial_path)
    rec = await injector.fire_pixel_write(uid, pin=pin, color=color)
    ctx.log_line(
        f"$ POST {injector.api_url}/api/echo  topic={rec['topic']}  payload={rec['payload_hex']}"
    )
    ctx.transcript.append(
        {
            "at": _now_iso(),
            "cmd": f"protomq echo → {rec['topic']}",
            "exit": 0,
            "stdout": f"payload(hex)={rec['payload_hex']}\nresponse={rec['echo_response']}",
            "stderr": "",
        }
    )
    rebooted, how = await _detect_reboot(
        injector, uid, serial_path=serial_path, serial_offset=serial_offset, timeout_s=observe_s
    )
    verdict = "REBOOTED (crash)" if rebooted else "SURVIVED (graceful)"
    ctx.log_line(
        f"inject_pixelwrite: after pixelWrite the device {verdict} "
        f"(via {how}, watched ≤{observe_s:.0f}s)"
    )
    # Machine-greppable verdict for the CI/harness to assert on.
    ctx.log_line(
        f"PIXELWRITE_VERDICT rebooted={'true' if rebooted else 'false'} pin={pin} color={color} uid={uid}"  # noqa: E501
    )
    ctx.pixelwrite_rebooted = rebooted  # type: ignore[attr-defined]


async def _stage_inject_i2c_probe(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Drive a v2 I2C bus scan + per-mux-channel probe at the checked-in DUT.

    Proves the muxed-sensor read end-to-end over protomq: wait for the DUT to
    check in, register the TCA9548A on its bus (``Add`` ``pca9548``), then
    ``Probe`` the bare bus and each requested mux channel — the v2 firmware
    selects the channel itself (``SelectMuxChannel``) per Probe, so a different
    ``channel`` is a different scan with no CircuitPython mux-latch. Logs a
    machine-greppable ``I2C_PROBE_VERDICT channel=<n|bus> found=[0x..]`` per scan.

    Stage params: ``channels`` (list, default ``[0, 1]``), ``mux_address`` (int,
    default ``0x77``), ``pin_scl``/``pin_sda`` (ints, the bus GPIOs — QT Py S3
    STEMMA = 40/41), ``scan_bus`` (bool, default true — a bare-bus scan first),
    ``checkin_timeout_s``, ``observe_s``. Requires protomq up + secrets pointing
    at it.
    """
    if not ctx.protomq_host or not ctx.protomq_port:
        raise StageError(
            "inject_i2c_probe needs protomq running (launch_protomq before this stage)"
        )
    channels = [int(c) for c in stage.get("channels", [0, 1])]
    mux_address = int(stage.get("mux_address", 0x77))
    pin_scl = int(stage.get("pin_scl", 40))
    pin_sda = int(stage.get("pin_sda", 41))
    scan_bus = bool(stage.get("scan_bus", True))
    checkin_timeout = float(stage.get("checkin_timeout_s", 150.0))
    observe_s = float(stage.get("observe_s", 15.0))
    io_user = ctx.secrets.get("IO_USERNAME") or "hil"
    api_url = stage.get("protomq_api_url") or getattr(ctx, "protomq_api_url", "") or None
    injector = WsI2cProbeInjector(
        broker_host=ctx.protomq_host,
        mqtt_port=ctx.protomq_port,
        api_url=api_url,
        io_username=io_user,
    )
    ctx.log_line(
        f"inject_i2c_probe: waiting ≤{checkin_timeout:.0f}s for DUT checkin on {io_user}/ws-d2b/#"
    )
    try:
        uid = await injector.wait_for_checkin(timeout=checkin_timeout)
    except WsI2cInjectError as exc:
        raise StageError(f"inject_i2c_probe: cannot observe checkin ({exc})") from exc
    if not uid:
        raise StageError(
            f"inject_i2c_probe: no DUT checkin on {io_user}/ws-d2b/# within {checkin_timeout:.0f}s "
            "(device booted with secrets pointing at protomq?)"
        )
    ctx.log_line(f"inject_i2c_probe: device checked in (uid={uid})")
    results: dict[str, list[int]] = {}

    def _record(label: str, rec: dict[str, Any]) -> None:
        found = rec["found"]
        results[label] = found
        hexs = " ".join(f"0x{a:02x}" for a in found) or "(none)"
        # A verdict must be unambiguous: only report found/empty when the device
        # actually replied. no_reply means the probe got no Probed back (a
        # capture/transport miss) — NOT an empty bus.
        got_reply = rec.get("got_reply", True)
        status = "ok" if got_reply else "NO_REPLY"
        ctx.log_line(
            f"I2C_PROBE_VERDICT scan={label} status={status} found=[{hexs}] uid={uid}"
        )
        ctx.transcript.append(
            {
                "at": _now_iso(),
                "cmd": f"protomq echo → i2c Probe ({label})",
                "exit": 0 if got_reply else 1,
                "stdout": (
                    f"status={status} found=[{hexs}]\n"
                    f"payload(hex)={rec['payload_hex']}\nraw={rec['raw']}"
                ),
                "stderr": "" if got_reply else "no Probed reply received after retries",
            }
        )

    # 1) bare-bus scan (shows direct devices + the mux itself).
    if scan_bus:
        ctx.log_line("inject_i2c_probe: scanning bare bus (no mux)")
        rec = await injector.probe(
            uid, pin_scl=pin_scl, pin_sda=pin_sda, mux_address=0, observe_s=observe_s
        )
        _record("bus", rec)

    # 2) register the mux (required before any channel probe).
    ctx.log_line(f"inject_i2c_probe: registering pca9548 @ 0x{mux_address:02x}")
    add = await injector.add_mux(uid, mux_address=mux_address, pin_scl=pin_scl, pin_sda=pin_sda)
    ctx.log_line(f"$ POST {injector.api_url}/api/echo  topic={add['topic']}  (Add pca9548)")
    await asyncio.sleep(1.0)

    # 3) probe each requested channel — the firmware latches the channel per Probe.
    for ch in channels:
        ctx.log_line(f"inject_i2c_probe: selecting + scanning mux channel {ch}")
        rec = await injector.probe(
            uid,
            pin_scl=pin_scl,
            pin_sda=pin_sda,
            mux_address=mux_address,
            mux_channel=ch,
            observe_s=observe_s,
        )
        _record(f"ch{ch}", rec)

    ctx.i2c_probe_results = results  # type: ignore[attr-defined]


async def _stage_inject_i2c_scan_v1(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Drive a v1 I2C bus scan at the checked-in DUT, per TwoWire port.

    Uses the known-good v1 firmware's ``I2CBusScanRequest`` (which carries an
    explicit ``i2c_port_number`` + pins) to prove which port reaches the STEMMA
    sensors — settles 'devices unreachable' vs 'WS bus-instance bug'. Scans each
    port in ``ports`` (default ``[0, 1]``) on ``pin_scl``/``pin_sda`` and logs a
    machine-greppable ``I2C_SCAN_VERDICT port=<n> found=[0x..]`` per port.
    Requires protomq up + secrets pointing at it (v1 device)."""
    if not ctx.protomq_host or not ctx.protomq_port:
        raise StageError("inject_i2c_scan_v1 needs protomq running (launch_protomq before this)")
    ports = [int(p) for p in stage.get("ports", [0, 1])]
    pin_scl = int(stage.get("pin_scl", 40))
    pin_sda = int(stage.get("pin_sda", 41))
    freq = int(stage.get("freq", 100000))
    checkin_timeout = float(stage.get("checkin_timeout_s", 150.0))
    observe_s = float(stage.get("observe_s", 15.0))
    io_user = ctx.secrets.get("IO_USERNAME") or "hil"
    api_url = stage.get("protomq_api_url") or getattr(ctx, "protomq_api_url", "") or None
    injector = WsSignalInjector(
        broker_host=ctx.protomq_host,
        mqtt_port=ctx.protomq_port,
        api_url=api_url,
        io_username=io_user,
    )
    ctx.log_line(f"inject_i2c_scan_v1: waiting ≤{checkin_timeout:.0f}s for DUT checkin")
    try:
        uid = await injector.wait_for_checkin(timeout=checkin_timeout)
    except WsInjectError as exc:
        raise StageError(f"inject_i2c_scan_v1: cannot observe checkin ({exc})") from exc
    if not uid:
        raise StageError(f"inject_i2c_scan_v1: no DUT checkin within {checkin_timeout:.0f}s")
    ctx.log_line(f"inject_i2c_scan_v1: device checked in (uid={uid})")
    results: dict[int, list[int]] = {}
    for port in ports:
        ctx.log_line(f"inject_i2c_scan_v1: scanning port {port} (SCL={pin_scl} SDA={pin_sda})")
        rec = await injector.i2c_scan(
            uid, port=port, scl=pin_scl, sda=pin_sda, freq=freq, observe_s=observe_s
        )
        found = rec["found"]
        results[port] = found
        hexs = " ".join(f"0x{a:02x}" for a in found) or "(none)"
        ctx.log_line(f"I2C_SCAN_VERDICT port={port} found=[{hexs}] uid={uid}")
        ctx.transcript.append(
            {
                "at": _now_iso(),
                "cmd": f"protomq echo → v1 I2C scan (port {port})",
                "exit": 0,
                "stdout": f"found=[{hexs}]\nreq(hex)={rec['payload_hex']}\nresp(hex)={rec['response_hex']}",
                "stderr": "",
            }
        )
    ctx.i2c_scan_v1_results = results  # type: ignore[attr-defined]


async def _stage_verify_checkin(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Verify the freshly-flashed+configured DUT checks in to the broker.

    The lightweight smoke test: after flash → secrets → power-cycle, watch MQTT
    for the device's pinConfigComplete on ``<user>/wprsnpr/#`` and log a
    machine-greppable ``CHECKIN_VERDICT ok=true|false``. No signal is injected —
    this just proves the end-to-end path (flash, secrets, WiFi, broker) works,
    which is the right default gate while the pixelWrite regression is parked.
    Requires protomq up (``launch_protomq``) and the DUT booted with secrets.
    """
    # ``via: serial`` — verify the checkin from the SERIAL log instead of the local
    # protomq broker. Needed when the DUT registers with a broker the controller
    # can't observe (e.g. an AirLift board on an isolated WiFi checking in to the
    # io.adafruit.com CLOUD, since the strict local protomq rejects the AirLift's
    # MQTT CONNECT). Watches the captured serial.log for the WS registration banner.
    if stage.get("via") == "serial":
        marker = re.compile(
            stage.get("marker", r"Registration and configuration complete"), re.IGNORECASE
        )
        timeout = float(stage.get("checkin_timeout_s", 180.0))
        ctx.log_line(
            f"verify_checkin (serial): waiting ≤{timeout:.0f}s for {marker.pattern!r} on serial"
        )
        ok = await _await_serial_marker(ctx.serial_log_path, marker, timeout)
        ctx.log_line(f"CHECKIN_VERDICT ok={'true' if ok else 'false'}")
        ctx.checkin_ok = ok  # type: ignore[attr-defined]
        if not ok and not stage.get("soft", False):
            raise StageError(
                f"verify_checkin (serial): {marker.pattern!r} not seen within {timeout:.0f}s "
                "(device booted with secrets pointing at the right broker?)"
            )
        return

    if not ctx.protomq_host or not ctx.protomq_port:
        raise StageError("verify_checkin needs protomq running (launch_protomq before this stage)")
    io_user = ctx.secrets.get("IO_USERNAME") or "hil"
    checkin_timeout = float(stage.get("checkin_timeout_s", 120.0))
    injector = WsSignalInjector(
        broker_host=ctx.protomq_host, mqtt_port=ctx.protomq_port, io_username=io_user
    )
    ctx.log_line(
        f"verify_checkin: waiting ≤{checkin_timeout:.0f}s for DUT checkin on {io_user}/wprsnpr/#"
    )
    try:
        uid = await injector.wait_for_checkin(timeout=checkin_timeout)
    except WsInjectError as exc:
        raise StageError(f"verify_checkin: cannot observe checkin ({exc})") from exc
    ok = bool(uid)
    ctx.log_line(f"CHECKIN_VERDICT ok={'true' if ok else 'false'} uid={uid or ''}")
    ctx.checkin_ok = ok  # type: ignore[attr-defined]
    # ``soft``: log the verdict but DON'T fail the job on a no-checkin. This lets a
    # caller (e.g. the version-bisection runner) distinguish a *broken firmware*
    # that flashed+booted but never connected (job finishes, CHECKIN_VERDICT
    # ok=false) from an *infrastructure* failure that errored before this stage
    # (no verdict line at all → flash/boot/host problem → recover + retry, not a
    # firmware verdict). The default stays strict (raise) for smoke-test gating.
    if not ok and not stage.get("soft", False):
        raise StageError(
            f"verify_checkin: no DUT checkin on {io_user}/wprsnpr/# within {checkin_timeout:.0f}s "
            "(device booted with secrets pointing at protomq?)"
        )


#: Registry of stage type → handler. Extend (don't edit the orchestrator) to
#: add new mechanisms — e.g. ``tinyuf2_install``, ``picotool``.
STAGE_HANDLERS: dict[str, Handler] = {
    "diagnose": _stage_diagnose,
    "inject_pixelwrite": _stage_inject_pixelwrite,
    "inject_i2c_probe": _stage_inject_i2c_probe,
    "inject_i2c_scan_v1": _stage_inject_i2c_scan_v1,
    "verify_checkin": _stage_verify_checkin,
    "enter_bootloader": _stage_enter_bootloader,
    "bootloader_touch": _stage_bootloader_touch,
    "erase": _stage_erase,
    "launch_protomq": _stage_launch_protomq,
    "flash": _stage_flash,
    "verify": _stage_verify,
    "start_serial_log": _stage_start_serial_log,
    "power_cycle": _stage_power_cycle,
    "write_secrets_msc": _stage_write_secrets_msc,
    "print_boot_log": _stage_print_boot_log,
}


#: A sensible default cycle for a combined ``.bin`` at 0x0: enter the ROM, then
#: stay in it across erase→flash→verify (every step ``--before no_reset`` so the
#: native-USB JTAG doesn't drop out between steps), then cold-boot. The UI seeds
#: its per-stage toggles from this; the API may send any list.
DEFAULT_FLASH_STAGES: list[dict[str, Any]] = [
    {"type": "enter_bootloader"},
    {"type": "erase", "before": "no_reset", "after": "no_reset"},
    {"type": "flash", "offset": "0x0", "before": "no_reset", "after": "no_reset"},
    {"type": "verify", "before": "no_reset", "after": "no_reset"},
    {"type": "power_cycle"},
]


#: Default cycle for a SAM (SAMD51) board — **UF2-MSC primary**. The firmware is a
#: ``.uf2`` (the WipperSnapper release asset), so the cycle is: 1200-baud
#: double-tap into the UF2 bootloader, **erase the app region** (``bossac --erase``
#: over the bootloader's SAM-BA CDC), copy the .uf2 onto the bootloader MSC drive
#: (the bootloader writes flash + resets into the app), then a clean power-cycle
#: (which also triggers the injected ``print_boot_log``). The erase is NOT
#: optional: a copy that silently no-ops (e.g. onto the wrong drive) used to leave
#: STALE firmware booting and reporting a false PASS — blanking the app first means
#: a failed flash drops back to the bootloader instead. A firmware-bench job for
#: the PyPortal/Titano sends this as ``params.stages`` (or relies on the device's
#: ``flasher: uf2-msc``).
SAMD51_FLASH_STAGES: list[dict[str, Any]] = [
    {"type": "enter_bootloader", "flasher": "uf2-msc"},
    {"type": "erase", "flasher": "uf2-msc"},
    {"type": "flash", "flasher": "uf2-msc"},
    {"type": "power_cycle"},
]

#: Alternative SAM cycle using the Adafruit/Arduino-fork ``bossac`` (NOT Debian's,
#: whose SAMD51 write applet is broken). Needs a ``.bin`` at 0x4000 (BossacFlasher
#: coerces a 0 offset up to app_offset as a backstop). One ``bossac -e -w -v -b -R``
#: does erase + write + verify + boot-from-flash + reset.
SAMD51_BOSSAC_FLASH_STAGES: list[dict[str, Any]] = [
    {"type": "enter_bootloader", "flasher": "bossac"},
    {"type": "flash", "flasher": "bossac", "offset": "0x4000"},
    {"type": "power_cycle"},
]


def validate_stages(stages: list[dict[str, Any]]) -> None:
    """Raise :class:`StageError` if any stage has an unknown type.

    Called before a run starts so a typo'd stage fails fast at submit time
    rather than mid-flash.
    """
    for i, stage in enumerate(stages):
        stype = stage.get("type")
        if stype not in STAGE_HANDLERS:
            raise StageError(
                f"unknown bench stage type {stype!r} at index {i}; known: {sorted(STAGE_HANDLERS)}"
            )


async def run_stages(stages: list[dict[str, Any]], ctx: BenchContext) -> None:
    """Run each stage in order, logging progress. Aborts on the first failure."""
    validate_stages(stages)
    total = len(stages)
    for i, stage in enumerate(stages, start=1):
        stype = stage["type"]
        ctx.log_line(f"── stage {i}/{total}: {stype} ──")
        await STAGE_HANDLERS[stype](stage, ctx)
