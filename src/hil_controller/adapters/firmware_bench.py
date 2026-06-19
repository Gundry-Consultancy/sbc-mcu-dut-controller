"""FirmwareBenchAdapter — interactive flash + protomq hold session.

Composes the building blocks into one time-boxed, extendable hold on a DUT:

* **flash phase** (``flash()``): stage the uploaded ``.bin`` onto the DUT host,
  launch a per-session protomq broker on the controller (discovering the ports
  it bound), then run the composable :mod:`bench_stages` pipeline
  (touch → erase → flash → verify → write secrets.json to MSC → power-cycle).
* **hold phase** (``run()``): keep protomq + serial capture (on the *post-reboot*
  log port) alive, streaming both to job log events, until the lease's
  ``expires_at`` passes or the job is cancelled. The window is extended by the
  ``/extend`` endpoint bumping the lease — the loop just re-reads it.
* **teardown** (``release()``): stop serial capture and kill protomq.

Host split: flash/serial/MSC act on the DUT host (``dut_transport``); protomq
runs on the controller (``controller_transport``, the LAN address the
freshly-flashed firmware reaches the broker at).

Live streaming: stage/serial/protomq callbacks are synchronous, so they push
onto an :class:`asyncio.Queue` drained in order by a background task that calls
the worker's async ``emit`` — preserving log ordering without lost lines.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from hil_controller.adapters.bench_stages import (
    DEFAULT_FLASH_STAGES,
    BenchContext,
    HostUsbWedgedError,
    run_stages,
    validate_stages,
)
from hil_controller.adapters.flashers.base import Artifact
from hil_controller.adapters.flashers.bossac import SAMD51_APP_OFFSET
from hil_controller.adapters.protomq_launcher import ProtomqLauncher
from hil_controller.adapters.serial_capture import SerialCaptureAdapter

log = logging.getLogger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


def _parse_offset(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    return int(str(value), 0)  # accepts "0x10000" or "65536"


def _msc_filter_from_serial(serial_port: str) -> str:
    """Derive the MSC by-path filter from a device's by-path serial node.

    The DUT's CDC serial and its USB-MSC volume share the same physical hub
    port, so the by-path fragment that selects the serial node also selects the
    MSC volume under ``/dev/disk/by-path`` (e.g. serial
    ``/dev/serial/by-path/platform-…usb-0:1.2:1.0`` → MSC ``…usb-0:1.2:1.2-scsi-…``
    → shared filter ``usb-0:1.2:``). This lets the controller fill the
    bench-specific MSC filter itself (like it does protomq host:port) so callers
    (CI / the skill) stay device-agnostic. Returns "" if no by-path is present.
    """
    if not serial_port:
        return ""
    m = re.search(r"usb-\d+:[0-9.]+:", serial_port)
    return m.group(0) if m else ""


#: Substrings that mark a stage failure as a **network-unreachable DUT host**
#: (vs a flash/logic error) — the host is off the network (often mid-reboot), so
#: the outage is transient and self-recovering. Matched case-insensitively against
#: the exception text. Deliberately connection-level only (no bare "timeout",
#: which a slow flash can also raise).
_HOST_UNREACHABLE_MARKERS = (
    "no route to host",
    "errno 113",
    "host is unreachable",
    "network is unreachable",
    "errno 101",
    "connection refused",
    "errno 111",
    "connection reset",
    "connection closed",
    "connection lost",
    "ssh connection",
    "could not connect",
)


def _is_host_unreachable_error(exc: BaseException) -> bool:
    """True when *exc* looks like the DUT host went network-unreachable mid-stage."""
    text = str(exc).lower()
    return any(marker in text for marker in _HOST_UNREACHABLE_MARKERS)


class FirmwareBenchAdapter:
    def __init__(
        self,
        *,
        controller_transport: Any,
        dut_transport: Any,
        hub_transport: Any,
        job_id: str,
        device: dict[str, Any],
        params: dict[str, Any],
        payload: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        controller_ip: str = "",
        protomq_repo: str = "",
        protomq_ref: str = "",
        jobs_dir: str = "/tmp/hil-jobs",
    ) -> None:
        self.controller_transport = controller_transport
        self.dut_transport = dut_transport
        self.hub_transport = hub_transport
        self.job_id = job_id
        self.device = device
        self.params = params or {}
        self.payload = payload or {}
        self.secrets = secrets or {}
        self.controller_ip = controller_ip
        self.protomq_repo = protomq_repo
        self.protomq_ref = protomq_ref
        self.jobs_dir = jobs_dir

        fw = self.params.get("firmware") or self.payload.get("firmware") or {}
        self._fw: dict = dict(fw)
        self._fw_local_path: str = fw.get("path", "")
        self._fw_offset: int = _parse_offset(fw.get("offset"), 0)

        self.window_minutes: int = int(self.params.get("window_minutes", 30) or 30)
        self._poll_s: float = float(self.params.get("hold_poll_s", 5.0) or 5.0)

        # runtime context injected by the worker via bind_runtime()
        self._emit: EmitFn | None = None
        self._db_path: str | None = None

        # live-log plumbing + owned resources
        self._log_q: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=8192)
        self._drain_task: asyncio.Task[None] | None = None
        self._protomq: ProtomqLauncher | None = None
        self._serial: SerialCaptureAdapter | None = None
        self._observer: Any | None = None
        self._observe_task: asyncio.Task[None] | None = None

        # Local (controller-side) log files captured during the hold; registered
        # as downloadable 'log' assets at teardown.
        self._serial_log_path: Path | None = None
        self._protomq_log_path: Path | None = None
        self._flash_log_path: Path | None = None

        # surfaced to the worker's post-run log scrape (kept empty: we stream live)
        self._deploy_stdout = ""
        self._deploy_stderr = ""
        self._run_stdout = ""
        self._run_stderr = ""

    # ------------------------------------------------------------------ #
    # runtime binding (worker injects job-event sink + DB access)         #
    # ------------------------------------------------------------------ #

    def bind_runtime(self, *, emit: EmitFn, db_path: str | None, job_id: str) -> None:
        self._emit = emit
        self._db_path = db_path
        self.job_id = job_id

    def _sink(self, stream: str, msg: str) -> None:
        """Synchronous log sink for stage/serial/protomq callbacks."""
        try:
            self._log_q.put_nowait(("log", {"stream": stream, "msg": msg}))
        except asyncio.QueueFull:
            try:  # drop oldest to keep the newest line
                self._log_q.get_nowait()
                self._log_q.put_nowait(("log", {"stream": stream, "msg": msg}))
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def _drain_logs(self) -> None:
        while True:
            kind, payload = await self._log_q.get()
            if self._emit is not None:
                try:
                    await self._emit(kind, payload)
                except Exception:  # noqa: BLE001
                    log.warning("firmware-bench emit failed", exc_info=True)

    async def _flush_logs(self) -> None:
        """Emit any queued lines synchronously (used on teardown)."""
        while not self._log_q.empty() and self._emit is not None:
            kind, payload = self._log_q.get_nowait()
            try:
                await self._emit(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # DeviceAdapter protocol                                              #
    # ------------------------------------------------------------------ #

    async def acquire(self) -> None:
        return None

    async def reset(self) -> None:
        return None

    async def open_serial(self):  # noqa: ANN201
        return iter([])

    async def flash(self, artifact: dict | None = None) -> None:
        """Setup phase, guarded for host-reachability.

        Wraps the whole setup (firmware staging, serial resolution, pipeline) so a
        failure *anywhere* in it caused by the DUT host going network-unreachable
        (``No route to host`` / SSH drop — often mid auto-reboot) flags the host's
        devices temporary + ``retry_after``. The "No route" frequently strikes
        during ``_stage_firmware`` (a ``copy_to`` before any stage runs), which is
        why this guards the whole method, not just ``run_stages``. A
        ``HostUsbWedgedError`` is already flagged deeper in, so just re-raise it.
        """
        try:
            await self._run_flash(artifact)
        except HostUsbWedgedError:
            raise
        except Exception as exc:  # noqa: BLE001 — flag in passing, then re-raise
            if _is_host_unreachable_error(exc):
                await self._flag_host_unreachable(str(exc))
            raise

    async def _run_flash(self, artifact: dict | None = None) -> None:
        """Setup phase: stage firmware, run the pipeline (protomq + serial start
        as pipeline stages, at the right moment), persist the transcript."""
        self._drain_task = asyncio.create_task(
            self._drain_logs(), name=f"fwbench-logs-{self.job_id}"
        )

        # On-demand power: energise ONLY this DUT's channel and wait for it to
        # enumerate. Idle DUTs (incl. a flaky/bad board) stay off the bus, so a
        # single misbehaving device can't storm dwc_otg and wedge the whole hub.
        await self._power_on_dut()

        work_root = PurePosixPath(f"/tmp/hil/{self.job_id}")
        remote_bin = await self._stage_firmware(work_root)

        # Controller-side log files for this session (registered as assets on
        # teardown). The serial capture writes here live; protomq stdout is
        # appended as it streams.
        local_log_dir = Path(self.jobs_dir) / self.job_id
        local_log_dir.mkdir(parents=True, exist_ok=True)
        self._serial_log_path = local_log_dir / "serial.log"
        self._protomq_log_path = local_log_dir / "protomq.log"
        self._flash_log_path = local_log_dir / "flash.log"

        flash_port = await self._resolve_serial(
            explicit=self.params.get("flash_serial_port"),
            filt=self.params.get("flash_port_filter"),
            fallback=self.device.get("serial_port") or "",
        )
        log_port = await self._resolve_serial(
            explicit=self.params.get("log_serial_port"),
            filt=self.params.get("log_port_filter"),
            fallback=flash_port,
        )
        self._log_serial_port = log_port

        stages = self._build_stages(log_port=log_port)
        validate_stages(stages)

        ctx = BenchContext(
            dut_transport=self.dut_transport,
            hub_transport=self.hub_transport,
            flash_serial_port=flash_port,
            log_serial_port=log_port,
            msc_filter=(
                self.params.get("msc_filter")
                or self.device.get("msc_filter")
                or _msc_filter_from_serial(self.device.get("serial_port", ""))
            ),
            device=self.device,
            artifact=Artifact(path=str(remote_bin), kind="combined_bin", offset=self._fw_offset),
            workspace_dir=self.params.get("workspace_dir", ""),
            pio_env=self.params.get("pio_env", ""),
            esptool_chip=self.params.get("esptool_chip", "auto"),
            esptool_baud=int(self.params.get("esptool_baud", 921600)),
            bossac_offset=_parse_offset(self.params.get("bossac_offset"), SAMD51_APP_OFFSET),
            sudo=bool(self.params.get("sudo", False)),
            emit=lambda m: self._sink("bench", m),
            secrets=self.secrets,
            # protomq launches mid-pipeline (after erase) via the launch_protomq
            # stage; serial capture starts before the reboot via start_serial_log.
            protomq_host="",
            protomq_port=0,
            launch_protomq=self._do_launch_protomq,
            start_serial=lambda: self._start_serial(log_port),
            pause_serial=self._pause_serial,
            resume_serial=self._resume_serial,
            serial_log_path=str(self._serial_log_path) if self._serial_log_path else "",
            # Live-stream verbosity: "all" (default, everything) or "filtered"
            # (NOTABLE_LIVE_LINES allow-list). flash.log asset is always full.
            stream_log_level=str(self.params.get("log_level", "all")).lower(),
        )
        try:
            await run_stages(stages, ctx)
        except HostUsbWedgedError as exc:
            # The DUT never appeared even after full recovery → the host's USB
            # stack (dwc_otg) is wedged, not just this board. Flag the whole host
            # for reboot so /v1/targets reports it and new jobs are gated off it;
            # the reconciler reboots it (if HIL_AUTO_HOST_REBOOT) once jobs drain.
            await self._flag_host_wedged(str(exc))
            raise
        finally:
            # Always persist the command transcript, even if a stage failed —
            # the partial flash.log is exactly what's needed to diagnose.
            if self._flash_log_path is not None and ctx.transcript:
                try:
                    self._flash_log_path.write_text(ctx.transcript_text(), encoding="utf-8")
                except OSError:
                    pass

        # Fallback: if no start_serial_log stage ran (e.g. a flash-only cycle
        # with no power-cycle), attach serial capture now so the hold still logs.
        if self._serial is None:
            await self._start_serial(log_port)

    async def _flag_host_wedged(self, detail: str) -> None:
        """Flag the DUT's host reboot_required after a HostUsbWedgedError (best effort)."""
        host_id = self.device.get("hub_host_id") or self.device.get("host_id")
        if self._db_path is None or not host_id:
            return
        try:
            from hil_controller.config import get_settings
            from hil_controller.host_recovery import mark_host_wedged

            eta_s = get_settings().host_reboot_eta_s
            await mark_host_wedged(self._db_path, host_id, reboot_eta_s=eta_s)
            self._sink(
                "bench",
                f"HOST USB WEDGED: flagged {host_id} reboot_required "
                f"(retry_after ~{eta_s}s) — {detail}",
            )
        except Exception:  # noqa: BLE001 — flagging must never mask the original error
            log.warning("failed to flag host %s wedged", host_id, exc_info=True)

    async def _flag_host_unreachable(self, detail: str) -> None:
        """Flag the DUT's host devices temporary after a network-unreachable stage error."""
        host_id = self.device.get("hub_host_id") or self.device.get("host_id")
        if self._db_path is None or not host_id:
            return
        try:
            from hil_controller.config import get_settings
            from hil_controller.host_recovery import mark_host_unreachable

            eta_s = get_settings().host_reboot_eta_s
            await mark_host_unreachable(self._db_path, host_id, reboot_eta_s=eta_s)
            self._sink(
                "bench",
                f"HOST UNREACHABLE: flagged {host_id} temporary (retry_after ~{eta_s}s) — {detail}",
            )
        except Exception:  # noqa: BLE001 — flagging must never mask the original error
            log.warning("failed to flag host %s unreachable", host_id, exc_info=True)

    def _solenoid_hub(self) -> Any:
        from hil_controller.adapters.solenoid_hub import SolenoidHubAdapter

        return SolenoidHubAdapter(
            transport=self.hub_transport, sudo=bool(self.params.get("sudo", False))
        )

    async def _power_on_dut(self) -> None:
        """On-demand power: energise this DUT's solenoid channel and wait for its
        by-path node to enumerate (handles the transient ``-32`` retry). No-op for a
        channel-less / pio DUT (assumed already powered)."""
        channel = self.device.get("solenoid_channel")
        if channel is None or self.hub_transport is None:
            return
        node = self.device.get("serial_port") or ""
        self._sink("bench", f"on-demand power: energising solenoid ch {channel}")
        try:
            await self._solenoid_hub().port_on(int(channel))
        except Exception as exc:  # noqa: BLE001 — let the flow continue; enter_bootloader's
            self._sink(
                "bench", f"WARNING: port_on ch {channel} failed: {exc}"
            )  # recovery handles absence
            return
        if not node:
            await asyncio.sleep(3)
            return
        for _ in range(30):  # ~30s for the app CDC to enumerate
            try:
                res = await self.dut_transport.exec(["test", "-e", node])
                if getattr(res, "exit_status", 1) == 0:
                    self._sink("bench", f"on-demand power: DUT enumerated at {node}")
                    await asyncio.sleep(2)  # settle before enter_bootloader
                    return
            except Exception:  # noqa: BLE001 — keep polling
                pass
            await asyncio.sleep(1)
        self._sink("bench", f"WARNING: DUT did not enumerate at {node} ~30s after power-on")

    async def _power_off_dut(self) -> None:
        """On-demand power: de-energise this DUT's channel at teardown so it's idle-off."""
        channel = self.device.get("solenoid_channel")
        if channel is None or self.hub_transport is None:
            return
        try:
            await self._solenoid_hub().port_off(int(channel))
            self._sink("bench", f"on-demand power: de-energised solenoid ch {channel}")
        except Exception as exc:  # noqa: BLE001 — teardown best-effort
            log.warning("port_off ch %s at teardown failed: %s", channel, exc)

    async def _signout_host_git(self) -> None:
        """Best-effort: clear per-job git/gh auth on the DUT host after a job so the
        next job's supplied PAT is used cleanly. Bounded + never raises; a no-op when
        gh isn't logged in / installed."""
        if self.dut_transport is None:
            return
        try:
            await asyncio.wait_for(
                self.dut_transport.exec(
                    [
                        "bash",
                        "-lc",
                        "gh auth logout 2>/dev/null; git credential-cache exit 2>/dev/null; true",
                    ]
                ),
                timeout=10,
            )
        except Exception:  # noqa: BLE001 — teardown hygiene must never fail the job
            pass

    def _build_stages(self, *, log_port: str) -> list[dict[str, Any]]:
        """Resolve the stage list and inject protomq-launch / serial-start.

        ``launch_protomq`` is inserted just before the first ``flash`` (so the
        broker only stands up once the chip is erased and about to be written —
        never orphaned by an erase failure). ``start_serial_log`` is inserted
        before the first ``power_cycle`` so the reboot's boot log is captured.
        Both are skipped if the operator already placed them explicitly.
        """
        stages = [dict(s) for s in (self.params.get("stages") or DEFAULT_FLASH_STAGES)]

        need_protomq = any(s.get("type") == "write_secrets_msc" for s in stages) or bool(
            self.params.get("launch_protomq", False)
        )
        if need_protomq and not any(s.get("type") == "launch_protomq" for s in stages):
            idx = self._first_index(stages, "flash")
            if idx is None:
                idx = self._first_index(stages, "write_secrets_msc")
            stages.insert(idx if idx is not None else len(stages), {"type": "launch_protomq"})
        elif not need_protomq:
            self._sink("bench", "no secrets stage / launch_protomq → protomq will not be launched")

        if log_port and not any(s.get("type") == "start_serial_log" for s in stages):
            pc = self._first_index(stages, "power_cycle")
            if pc is not None:
                stages.insert(pc, {"type": "start_serial_log"})

        # After the final reboot the app comes up and (eventually) exposes its
        # MSC volume — dump the boot log then, so a no-secrets reboot loop or a
        # crash is visible. Skipped if no power-cycle boots the app.
        if not any(s.get("type") == "print_boot_log" for s in stages):
            last_pc = self._last_index(stages, "power_cycle")
            if last_pc is not None:
                stages.insert(last_pc + 1, {"type": "print_boot_log"})
        return stages

    @staticmethod
    def _first_index(stages: list[dict[str, Any]], stype: str) -> int | None:
        return next((i for i, s in enumerate(stages) if s.get("type") == stype), None)

    @staticmethod
    def _last_index(stages: list[dict[str, Any]], stype: str) -> int | None:
        idxs = [i for i, s in enumerate(stages) if s.get("type") == stype]
        return idxs[-1] if idxs else None

    async def _do_launch_protomq(self) -> tuple[str, int]:
        """Launch protomq and return ``(host, mqtt_port)`` for the secrets stage."""
        await self._launch_protomq()
        host = self.controller_ip or "127.0.0.1"
        port = self._protomq.mqtt_port if self._protomq else 0
        return host, int(port or 0)

    async def run(self) -> str:
        """Hold phase: keep protomq + serial alive until window end / cancel."""
        self._sink("bench", f"holding device for ~{self.window_minutes} min (extendable)")
        deadline_monotonic = time.monotonic() + self.window_minutes * 60
        while True:
            reason = await self._window_status(deadline_monotonic)
            if reason:
                self._sink("bench", f"ending hold: {reason}")
                break
            await asyncio.sleep(self._poll_s)
        return "pass"

    async def release(self) -> None:
        await self._teardown()

    async def cleanup(self) -> None:
        await self._teardown()

    # ------------------------------------------------------------------ #
    # helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _stage_firmware(self, work_root: PurePosixPath) -> PurePosixPath:
        # Resolve the firmware to a controller-local path: an explicit
        # params.firmware.path, OR download params.firmware.url (public release
        # assets), OR a prior POST /v1/firmware upload (which set .path). The
        # controller then copies that file to the bench.
        if not self._fw_local_path and self._fw.get("url"):
            import os as _os

            from hil_controller.adapters.firmware_fetch import (
                FirmwareFetchError,
                resolve_firmware_local,
            )

            dest_dir = str(Path(self.jobs_dir) / self.job_id / "firmware")
            try:
                self._fw_local_path = await resolve_firmware_local(
                    self._fw, dest_dir=dest_dir, token=_os.environ.get("HIL_FIRMWARE_FETCH_TOKEN")
                )
            except FirmwareFetchError as exc:
                raise RuntimeError(f"firmware-bench: {exc}") from exc
            self._sink("bench", f"fetched firmware from url → {self._fw_local_path}")
        if not self._fw_local_path:
            raise RuntimeError(
                "firmware-bench: no firmware (set params.firmware.path or .url, "
                "or upload via POST /v1/firmware)"
            )
        await self._link_firmware_asset(self._fw_local_path)
        remote_dir = work_root
        remote_bin = remote_dir / "firmware.bin"
        await self.dut_transport.exec(["mkdir", "-p", str(remote_dir)])
        await self.dut_transport.copy_to(Path(self._fw_local_path), remote_bin)
        self._sink("bench", f"staged firmware → {remote_bin} on DUT host")
        return remote_bin

    async def _launch_protomq(self) -> None:
        from hil_controller.config import get_settings

        cfg = get_settings()
        repo = self.protomq_repo or cfg.protomq_repo
        # firmware-bench defaults to the displays-v2-testing broker branch, not
        # the arduino-ws default (main).
        ref = self.protomq_ref or cfg.firmware_bench_protomq_ref
        work_dir = str(Path(self.jobs_dir) / self.job_id / "protomq")
        self._protomq = ProtomqLauncher(
            controller_transport=self.controller_transport,
            repo=repo,
            ref=ref,
            work_dir=work_dir,
            active_script=self.params.get("protomq_script") or None,
            on_line=self._protomq_line,
            pat=self.params.get("protomq_pat") or self.params.get("pat") or None,
            credential_helper=cfg.git_credential_helper or None,
            proto_repo=self.params.get("protobuf_repo") or cfg.protobuf_repo,
            proto_ref=self.params.get("protobuf_ref") or cfg.protobuf_ref,
        )
        self._sink("bench", f"cloning + building protomq ({ref})")
        await self._protomq.clone_and_build()
        await self._protomq.start()
        self._sink(
            "bench",
            f"protomq up: mqtt={self._protomq.mqtt_port} api={self._protomq.api_port} "
            f"(DUT will use {self.controller_ip}:{self._protomq.mqtt_port})",
        )
        await self._start_observer()

    def _protomq_line(self, line: str) -> None:
        """protomq stdout sink: tee to the protomq.log file + job events."""
        if self._protomq_log_path is not None:
            try:
                # UTC ms timestamp so protomq.log lines up with serial/flash logs.
                ts = datetime.now(UTC).isoformat(timespec="milliseconds")
                with self._protomq_log_path.open("a", encoding="utf-8") as f:
                    f.write(f"{ts}  {line}\n")
            except OSError:
                pass
        self._sink("protomq", line)

    async def _start_observer(self) -> None:
        """MQTT log forwarding + optional script activation, on localhost."""
        if self._protomq is None or self._emit is None:
            return
        try:
            from hil_controller.adapters.protomq_observer import ProtoMQObserver
        except ImportError:
            return
        obs = ProtoMQObserver(
            broker_host="127.0.0.1",
            mqtt_port=self._protomq.mqtt_port or 1884,
            api_url=f"http://127.0.0.1:{self._protomq.api_port or 5173}",
        )
        script = self.params.get("protomq_script")
        if script:
            try:
                await obs.activate_script(script)
                self._sink("protomq", f"activated script {script!r}")
            except Exception as exc:  # noqa: BLE001
                self._sink("protomq", f"activate failed: {exc}")
        self._observer = obs
        self._observe_task = asyncio.create_task(
            obs.observe(self._emit), name=f"fwbench-mqtt-{self.job_id}"
        )

    async def _resolve_serial(
        self, *, explicit: str | None, filt: str | None, fallback: str
    ) -> str:
        """Resolve a serial path: explicit > by-id filter match > fallback."""
        if explicit:
            return explicit
        if filt:
            res = await self.dut_transport.exec(["ls", "-1", "/dev/serial/by-id/"])
            names = [
                ln.strip() for ln in (getattr(res, "stdout", "") or "").splitlines() if ln.strip()
            ]
            needle = filt.lower()
            match = next((n for n in names if needle in n.lower()), None)
            if match:
                return f"/dev/serial/by-id/{match}"
            self._sink("bench", f"WARNING: no /dev/serial/by-id matched {filt!r}; falling back")
        return fallback

    async def _start_serial(self, port: str) -> None:
        if self._serial is not None:
            return  # already capturing (start_serial_log stage ran)
        if not port:
            self._sink("bench", "no log serial port resolved; serial capture disabled")
            return
        baud = int(self.params.get("serial_baud", 115200))
        self._serial = SerialCaptureAdapter(
            transport=self.dut_transport,
            serial_path=port,
            baud=baud,
            on_line=lambda line: self._sink("serial", line),
            artifact_path=self._serial_log_path,
        )
        await self._serial.start()
        self._sink("bench", f"serial capture started on {port} @ {baud}")

    async def _pause_serial(self) -> None:
        """Release the serial port so an esptool op can use it (no-solenoid reset).
        No-op if no capture is running."""
        if self._serial is not None:
            await self._serial.pause()
            self._sink("bench", "serial capture paused (port released for esptool reset)")

    async def _resume_serial(self) -> None:
        """Re-attach serial capture after an esptool op (catches the boot/checkin log)."""
        if self._serial is not None:
            await self._serial.resume()
            self._sink("bench", "serial capture resumed")

    async def _window_status(self, deadline_monotonic: float) -> str | None:
        """Return a reason to end the hold, or None to keep holding."""
        # Cancellation / terminal state via the DB.
        if self._db_path:
            try:
                from hil_controller.db.connection import get_db, get_job
                from hil_controller.queue.leases import get_active_for_job

                async with get_db(self._db_path) as db:
                    row = await get_job(db, self.job_id)
                if row and row["state"] in ("cancelled", "error", "timeout", "finished"):
                    return f"job state {row['state']}"

                lease = await get_active_for_job(self._db_path, self.job_id)
                if lease is None:
                    return "lease released"
                if lease.get("expires_at"):
                    from datetime import datetime, timezone

                    now = datetime.now(UTC)
                    try:
                        if datetime.fromisoformat(lease["expires_at"]) <= now:
                            return "window expired"
                    except ValueError:
                        pass
                return None  # lease active, not expired
            except Exception as exc:  # noqa: BLE001
                log.warning("firmware-bench window poll failed: %s", exc)
        # Fallback when not DB-bound: monotonic deadline.
        if time.monotonic() >= deadline_monotonic:
            return "window expired (local timer)"
        return None

    async def _link_firmware_asset(self, local_path: str) -> None:
        """Tie the staged firmware to this job (Assets page + eventual purge).

        An uploaded image already has a ``kind='firmware'`` asset (job_id NULL) —
        link it to this job. A url-fetched / pre-staged path has none — register
        one. Best-effort; never breaks the flash.
        """
        if self._db_path is None or not local_path:
            return
        try:
            import os as _os

            from hil_controller.db.connection import get_db

            async with get_db(self._db_path) as db:
                cur = await db.execute(
                    "UPDATE assets SET job_id=? WHERE path=? AND kind='firmware' AND job_id IS NULL",  # noqa: E501
                    (self.job_id, local_path),
                )
                await db.commit()
                if (cur.rowcount or 0) > 0:
                    self._sink(
                        "bench",
                        f"linked uploaded firmware asset to job ({_os.path.basename(local_path)})",
                    )
                    return
                size = _os.path.getsize(local_path) if _os.path.isfile(local_path) else 0
                await db.execute(
                    "INSERT INTO assets (id, filename, path, size_bytes, kind, job_id, created_at) "
                    "VALUES (?, ?, ?, ?, 'firmware', ?, ?)",
                    (
                        str(uuid.uuid4()),
                        _os.path.basename(local_path),
                        local_path,
                        size,
                        self.job_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                await db.commit()
                self._sink(
                    "bench", f"registered firmware asset for job ({_os.path.basename(local_path)})"
                )
        except Exception:  # noqa: BLE001 — asset bookkeeping must never break the flash
            log.warning("failed to link firmware asset for job %s", self.job_id, exc_info=True)

    async def _register_log_asset(self, path: Path | None, label: str) -> None:
        """Persist a captured log file as a downloadable 'log' asset.

        Mirrors the worker's deploy.log handling so serial / protomq output from
        a hold shows up on the Assets page and job detail, not only as events.
        """
        if self._db_path is None or path is None:
            return
        try:
            if not path.exists() or path.stat().st_size == 0:
                return
            from hil_controller.db.connection import get_db

            aid = str(uuid.uuid4())
            size = path.stat().st_size
            async with get_db(self._db_path) as db:
                await db.execute(
                    "INSERT INTO assets (id, filename, path, size_bytes, kind, job_id, created_at) "
                    "VALUES (?, ?, ?, ?, 'log', ?, ?)",
                    (aid, path.name, str(path), size, self.job_id, datetime.now(UTC).isoformat()),
                )
                await db.commit()
            self._sink("bench", f"saved {label} log ({size} bytes) as asset {path.name}")
        except Exception:  # noqa: BLE001 — asset capture must never fail teardown
            log.warning("failed to register %s log asset", label, exc_info=True)

    async def _teardown(self) -> None:
        if self._serial is not None:
            try:
                # Hard bound: never let serial teardown stall lease release / asset
                # registration, even if the SSH stream close hangs.
                await asyncio.wait_for(self._serial.stop(), timeout=8.0)
            except (TimeoutError, Exception):  # noqa: BLE001
                log.warning("serial stop timed out during teardown")
            self._serial = None
        if self._observe_task is not None:
            self._observe_task.cancel()
            try:
                await asyncio.wait_for(self._observe_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._observe_task = None
        if self._observer is not None:
            try:
                await asyncio.wait_for(self._observer.deactivate(), timeout=5.0)
            except (TimeoutError, Exception):  # noqa: BLE001
                pass
            self._observer = None
        if self._protomq is not None:
            try:
                await asyncio.wait_for(self._protomq.stop(), timeout=8.0)
            except (TimeoutError, Exception):  # noqa: BLE001
                log.warning("protomq stop timed out during teardown")
            self._protomq = None
        # Persist captured logs as downloadable assets (after the writers stop).
        await self._register_log_asset(self._flash_log_path, "flash")
        await self._register_log_asset(self._serial_log_path, "serial")
        await self._register_log_asset(self._protomq_log_path, "protomq")
        await self._flush_logs()
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._drain_task = None
        # On-demand power: de-energise this DUT's channel so it's idle-off (only the
        # DUT under test is ever powered — a bad board can't storm the hub).
        await self._power_off_dut()
        # Sign out any per-job git/gh auth on the DUT host so the next job's supplied
        # PAT is used cleanly (host-side per-session clones authenticate fresh — no
        # stale login leaks between jobs). Best-effort + bounded; a no-op when gh
        # isn't logged in (or isn't installed). Does NOT touch the controller's own
        # persistent gh credential helper (separate host).
        await self._signout_host_git()
