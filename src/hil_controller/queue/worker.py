"""Per-job async worker: drives the state machine, emits events."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from hil_controller.adapters.base import DeviceAdapter
from hil_controller.queue.events import EventBus

log = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"finished", "error", "timeout", "cancelled"})

# Redact credentials that adapters can echo into deploy logs: tokens embedded in
# clone URLs (https://<token>@host) and bare GitHub PATs. Keeps captured logs +
# the deploy:info announce safe to surface in the UI — partial (last-4) so the
# command stays identifiable, consistent with the bench transcript masking.
_URL_CRED_RE = re.compile(r"(https?://)([^@/\s]+)@")
_TOKEN_RE = re.compile(r"\b(?:ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})\b")


def _redact_secrets(text: str) -> str:
    from hil_controller.redact import mask_secret

    text = _URL_CRED_RE.sub(lambda m: m.group(1) + mask_secret(m.group(2)) + "@", text)
    return _TOKEN_RE.sub(lambda m: mask_secret(m.group(0)), text)


@dataclass
class WorkerResult:
    state: str
    result: str  # pass | fail | error | timeout | cancelled


class JobWorker:
    def __init__(
        self,
        *,
        job_id: str,
        adapter: DeviceAdapter,
        event_bus: EventBus,
        script: str,
        params: dict[str, Any],
        payload: dict[str, Any],
        timeouts: dict[str, Any],
        db_path: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.adapter = adapter
        self.event_bus = event_bus
        self.script = script
        self.params = params
        self.payload = payload
        self.timeouts = timeouts
        self.db_path = db_path
        self._cancelled = False
        self._protomq_observer: Any | None = None
        self._ctrl_protomq: Any | None = None
        self._hil_capture: Any | None = None

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        await self.event_bus.publish(self.job_id, {"kind": kind, "payload": payload})
        if self.db_path:
            from hil_controller.db.connection import append_event, get_db, update_job_state

            async with get_db(self.db_path) as db:
                await append_event(db, self.job_id, kind, payload)
                if kind == "state":
                    kw: dict[str, Any] = {}
                    if "result" in payload:
                        kw["result"] = payload["result"]
                    await update_job_state(db, self.job_id, payload["state"], **kw)
        if kind == "state":
            await self._sync_camera_settings(payload["state"])

    async def _sync_camera_settings(self, state: str) -> None:
        """Push compromise lens/illuminator settings on state transitions.

        On both entry to running-ish states and on terminal states we
        recompute, so the camera also relaxes back to auto/off when the
        last active device on it finishes. Best-effort — never propagates
        an exception out of the worker.
        """
        if not self.db_path:
            return
        if state not in TERMINAL_STATES and state not in (
            "preparing",
            "flashing",
            "running",
            "assigned",
        ):
            return
        try:
            from hil_controller.adapters.camera.orchestrator import recompute_for_device
            from hil_controller.db.connection import get_db

            # The worker doesn't carry the assigned_device explicitly; pull
            # it from the job row.
            async with get_db(self.db_path) as db:
                async with db.execute(
                    "SELECT assigned_device FROM jobs WHERE id = ?", (self.job_id,)
                ) as cur:
                    row = await cur.fetchone()
                if not row or not row["assigned_device"]:
                    return
                await recompute_for_device(db, row["assigned_device"])
        except Exception as exc:
            log.warning("camera settings sync failed for job %s: %s", self.job_id, exc)

    async def cancel(self) -> None:
        self._cancelled = True

    #: Scripts whose run phase is a long-lived, extendable interactive hold.
    #: Their deadline is owned by the adapter (via the lease ``expires_at``),
    #: so the fixed ``total_s`` ceiling must not pre-empt the window.
    INTERACTIVE_SCRIPTS = frozenset({"firmware-bench"})

    async def run(self) -> WorkerResult:
        if self.script in self.INTERACTIVE_SCRIPTS:
            return await self._run()
        total = self.timeouts.get("total_s", 1800)
        try:
            return await asyncio.wait_for(self._run(), timeout=total)
        except TimeoutError:
            await self._emit("state", {"state": "timeout"})
            return WorkerResult(state="timeout", result="timeout")

    async def _run(self) -> WorkerResult:
        _observe_task: asyncio.Task[None] | None = None
        _capture_task: asyncio.Task[None] | None = None
        # Give adapters that need live job-event streaming + DB access (e.g. the
        # firmware-bench hold-loop reading its lease expiry) the runtime context
        # the worker holds. Opt-in and additive — other adapters ignore it.
        if hasattr(self.adapter, "bind_runtime"):
            self.adapter.bind_runtime(  # type: ignore[attr-defined]
                emit=self._emit, db_path=self.db_path, job_id=self.job_id
            )
        try:
            await self._emit("state", {"state": "preparing"})
            await self.adapter.acquire()

            if self._cancelled:
                await self._emit("state", {"state": "cancelled"})
                return WorkerResult(state="cancelled", result="cancelled")

            # For git-source payloads, flash = deploy (clone + setup)
            if self.payload.get("kind") == "git-source":
                await self._emit("state", {"state": "flashing"})
                await self._deploy_git_source()
            elif self.payload.get("kind") not in (None, "fake", "none"):
                await self._emit("state", {"state": "flashing"})
                await self.adapter.flash(self.payload)
            elif self.script in self.INTERACTIVE_SCRIPTS and (
                self.params.get("firmware") or self.params.get("stages")
            ):
                # firmware-bench drives its pipeline (flash → secrets → inject …)
                # from params.firmware/params.stages — there is no top-level flash
                # payload. Run the setup pipeline before the hold; a bare
                # interactive hold (no firmware/stages) still skips straight to run().
                await self._emit("state", {"state": "flashing"})
                await self.adapter.flash(self.payload)

            if self._cancelled:
                await self._emit("state", {"state": "cancelled"})
                return WorkerResult(state="cancelled", result="cancelled")

            await self._emit("state", {"state": "running"})
            await self._maybe_launch_controller_protomq()
            _observe_task = await self._start_protomq_observer()
            _capture_task = self._maybe_start_hil_capture()
            result = await self._run_script()
            if _capture_task is not None and self._hil_capture is not None:
                # No more stdout markers once the run returns — drain the queue
                # (the last splash sample may still be finishing) before harvest.
                self._hil_capture.close()
                try:
                    await asyncio.wait_for(_capture_task, timeout=90)
                except (asyncio.CancelledError, Exception):
                    pass

        except asyncio.CancelledError:
            await self._emit("state", {"state": "cancelled"})
            return WorkerResult(state="cancelled", result="cancelled")
        except Exception as exc:
            log.exception("Worker error for job %s", self.job_id)
            await self._emit("state", {"state": "error"})
            await self._emit("log", {"msg": str(exc), "stream": "stderr"})
            return WorkerResult(state="error", result="error")
        finally:
            if _observe_task and not _observe_task.done():
                _observe_task.cancel()
                try:
                    await _observe_task
                except (asyncio.CancelledError, Exception):
                    pass
            if _capture_task is not None and not _capture_task.done():
                _capture_task.cancel()
                try:
                    await _capture_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._ctrl_protomq is not None:
                try:
                    await asyncio.wait_for(self._ctrl_protomq.stop(), timeout=10)
                except (asyncio.CancelledError, Exception):
                    pass
            await self._harvest_artifacts()
            await self._purge_job_secrets()
            try:
                await self.adapter.release()
            except Exception:
                pass

        await self._emit_protomq_status()
        final_result = "pass" if result == 0 else "fail"
        await self._emit("state", {"state": "finished", "result": final_result})
        return WorkerResult(state="finished", result=final_result)

    async def _deploy_git_source(self) -> None:
        if hasattr(self.adapter, "deploy"):
            source = getattr(self.adapter, "source", {})
            repo = source.get("repo", "")
            ref = source.get("ref", "")
            setup: list[str] = source.get("setup") or []
            msg = f"cloning {_redact_secrets(repo)} @ {ref}"
            if setup:
                cmd_str = (
                    setup[2]
                    if len(setup) == 3 and setup[:2] == ["bash", "-c"]
                    else shlex.join(setup)
                )
                msg += f"\nsetup: {_redact_secrets(cmd_str)}"
            await self._emit("log", {"stream": "deploy:info", "msg": msg})
            try:
                await self.adapter.deploy()  # type: ignore[attr-defined]
            finally:
                # Capture the build/deploy output even when deploy() raises, so a
                # failed compile (e.g. PlatformIO toolchain errors) is findable from
                # the UI as a downloadable log asset — not just the streamed events.
                await self._capture_deploy_log()

    async def _capture_deploy_log(self) -> None:
        sections: list[str] = []
        for attr, stream in [
            ("_deploy_stdout", "deploy:stdout"),
            ("_deploy_stderr", "deploy:stderr"),
        ]:
            text = getattr(self.adapter, attr, "")
            if text:
                text = _redact_secrets(text)
                await self._emit("log", {"stream": stream, "msg": text})
                sections.append(f"===== {stream} =====\n{text}")
        if sections:
            await self._store_log_asset("deploy.log", "\n\n".join(sections))

    async def _store_log_asset(self, filename: str, content: str) -> None:
        """Persist a deploy/build log to disk and register it as a job asset."""
        if not self.db_path:
            return
        try:
            from hil_controller.config import resolve_jobs_dir
            from hil_controller.db.connection import get_db

            dest_dir = Path(resolve_jobs_dir()) / self.job_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / filename
            dest.write_text(content, encoding="utf-8")
            aid = str(uuid.uuid4())
            async with get_db(self.db_path) as db:
                await db.execute(
                    "INSERT INTO assets (id, filename, path, size_bytes, kind, job_id, created_at) "
                    "VALUES (?, ?, ?, ?, 'log', ?, ?)",
                    (
                        aid,
                        filename,
                        str(dest),
                        len(content.encode("utf-8")),
                        self.job_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                await db.commit()
        except Exception as exc:  # never let log capture fail the job
            log.warning("failed to store deploy log asset for %s: %s", self.job_id, exc)

    async def _harvest_artifacts(self) -> None:
        """Copy files matched by ``params.collect_artifacts`` (a list of glob
        patterns) into the job dir and register each as a downloadable asset.

        Lets a non-interactive job (e.g. a pytest-suite display test) surface
        proof it produced on the runner host — camera ROI snapshots, a broker
        log — as first-class job assets that CI can pull via
        ``GET /v1/jobs/{id}/assets``. Local-filesystem only (the localhost
        runner); best-effort and never fails the job. ``.log``/``.txt`` register
        as ``log`` (text-previewable), everything else as ``file``.
        """
        patterns = self.params.get("collect_artifacts") or []
        if not patterns or not self.db_path:
            return
        try:
            import glob as _glob
            import shutil

            from hil_controller.config import resolve_jobs_dir
            from hil_controller.db.connection import get_db

            dest_dir = Path(resolve_jobs_dir()) / self.job_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            seen: set[str] = set()
            harvested: list[tuple[str, str, int, str]] = []
            for pattern in patterns:
                for src in sorted(_glob.glob(str(pattern))):
                    p = Path(src)
                    if not p.is_file() or src in seen:
                        continue
                    seen.add(src)
                    dest = dest_dir / p.name
                    if p.resolve() != dest.resolve():
                        shutil.copyfile(src, dest)
                    kind = "log" if p.suffix.lower() in (".log", ".txt") else "file"
                    harvested.append((p.name, str(dest), dest.stat().st_size, kind))
            if not harvested:
                return
            async with get_db(self.db_path) as db:
                for filename, path, size, kind in harvested:
                    await db.execute(
                        "INSERT INTO assets "
                        "(id, filename, path, size_bytes, kind, job_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            filename,
                            path,
                            size,
                            kind,
                            self.job_id,
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                await db.commit()
            await self._emit(
                "log",
                {
                    "stream": "artifacts",
                    "msg": f"harvested {len(harvested)} artifact(s): "
                    + ", ".join(h[0] for h in harvested),
                },
            )
        except Exception as exc:  # never let artifact harvest fail the job
            log.warning("artifact harvest failed for %s: %s", self.job_id, exc)

    async def _run_script(self) -> int:
        if hasattr(self.adapter, "run"):
            outcome = await self.adapter.run()  # type: ignore[attr-defined]
            sections: list[str] = []
            for attr, stream in [("_run_stdout", "stdout"), ("_run_stderr", "stderr")]:
                text = getattr(self.adapter, attr, "")
                if text:
                    await self._emit("log", {"stream": stream, "msg": text})
                    sections.append(f"===== {stream} =====\n{_redact_secrets(text)}")
            if sections:
                # Persist the run output as a downloadable asset too (not just a
                # streamed event), so CI can pull the test/client log alongside
                # deploy.log + any collected artifacts.
                await self._store_log_asset("run.log", "\n\n".join(sections))
            return 0 if outcome == "pass" else 1
        # Fake adapter — simulate pass
        await asyncio.sleep(0)
        return 0

    # ---------------------------------------------------------------------- #
    # ProtoMQ observer                                                         #
    # ---------------------------------------------------------------------- #

    async def _maybe_launch_controller_protomq(self) -> None:
        """Launch a protomq broker ON THE CONTROLLER for a non-interactive job
        that asks for it (``params.protomq.launch_on == "controller"``), and
        point the (possibly remote) test at it.

        For a python display HIL test on a remote SBC, the broker can't run on
        the DUT host (a Pi Zero W is ARMv6 — no node). So launch protomq locally
        via the same ProtomqLauncher firmware-bench uses, then inject
        PROTOMQ_RUN_EXTERNALLY + PROTOMQ_HOST/PORT (and MQTT_HOST/PORT) =
        controller_ip into the run env so the SBC test connects back to it.
        Torn down in the worker's finally."""
        cfg = self.params.get("protomq") or {}
        if cfg.get("launch_on") != "controller":
            return
        from hil_controller.adapters.protomq_launcher import ProtomqLauncher
        from hil_controller.config import get_settings, resolve_jobs_dir
        from hil_controller.hosts.local import LocalTransport

        s = get_settings()
        launcher = ProtomqLauncher(
            controller_transport=LocalTransport(),
            repo=cfg.get("repo") or s.protomq_repo,
            ref=cfg.get("ref") or s.firmware_bench_protomq_ref,
            work_dir=str(Path(resolve_jobs_dir()) / self.job_id / "protomq"),
            active_script=cfg.get("script") or None,
            on_line=None,
            pat=self.params.get("protomq_pat") or self.params.get("pat") or None,
            credential_helper=s.git_credential_helper or None,
            proto_repo=s.protobuf_repo,
            proto_ref=s.protobuf_ref,
        )
        await launcher.clone_and_build()
        await launcher.start()
        self._ctrl_protomq = launcher
        host, port = s.controller_ip, str(launcher.mqtt_port or 1884)
        injected = {
            "PROTOMQ_RUN_EXTERNALLY": "1",
            "PROTOMQ_HOST": host,
            "PROTOMQ_PORT": port,
            "MQTT_HOST": host,
            "MQTT_PORT": port,
            # WS-Python's defaults.py requires PROTOMQ_PATH to be set whenever
            # PROTOMQ_RUN_EXTERNALLY is — value is unused when external.
            "PROTOMQ_PATH": "/tmp/protomq-external",
        }
        # The scheduler parses request_json TWICE — once to build the adapter and
        # once for the worker — so self.params and the adapter's params are
        # *different* dict objects. The run reads the adapter's, so inject there
        # too (not only into self.params, which only our tests inspect).
        for params in (self.params, getattr(self.adapter, "params", None)):
            if isinstance(params, dict):
                params.setdefault("extra_env", {}).update(injected)
        await self._emit("log", {"stream": "protomq",
                                 "msg": f"launched on controller; test connects to "
                                        f"{host}:{port} (api {launcher.api_port})"})

    def _maybe_start_hil_capture(self) -> asyncio.Task[None] | None:
        """Start the controller-side webcam capture for a remote-display HIL test
        (``params.capture`` present). The SBC pytest drives the panel and prints
        ``WS_HIL_CAPTURE`` stage markers; we stream those into the capture via the
        adapter's ``on_line`` hook and a concurrent consume task turns each into a
        proof frame. The adapter must support per-line streaming (GitDeploy does)."""
        cfg = self.params.get("capture") or {}
        if not cfg.get("webcam_url") or not hasattr(self.adapter, "on_line"):
            return None
        from hil_controller.adapters.camera.hil_capture import HilCapture

        capture = HilCapture(cfg)
        self._hil_capture = capture
        self.adapter.on_line = capture.feed
        task = asyncio.ensure_future(capture.consume())
        log.info("hil_capture started for job %s (webcam %s, roi %s)",
                 self.job_id, cfg.get("webcam_url"), cfg.get("roi"))
        return task

    async def _start_protomq_observer(self) -> asyncio.Task[None] | None:
        cfg = self.params.get("protomq", {})
        if not cfg.get("script") or not cfg.get("broker_host"):
            return None
        try:
            from hil_controller.adapters.protomq_observer import ProtoMQObserver
        except ImportError:
            return None

        broker_host = cfg["broker_host"]
        api_url = f"http://{broker_host}:{cfg.get('api_port', 5173)}"
        obs = ProtoMQObserver(
            broker_host=broker_host,
            mqtt_port=cfg.get("mqtt_port", 1884),
            api_url=api_url,
        )
        try:
            await obs.activate_script(cfg["script"])
            await self._emit(
                "log",
                {
                    "stream": "protomq",
                    "msg": f"script '{cfg['script']}' activated on {broker_host}",
                },
            )
        except Exception as exc:
            log.warning("ProtoMQ activate failed: %s", exc)
            await self._emit("log", {"stream": "protomq", "msg": f"activate failed: {exc}"})
            return None

        self._protomq_observer = obs
        return asyncio.create_task(obs.observe(self._emit), name=f"protomq-{self.job_id}")

    async def _purge_job_secrets(self) -> None:
        """Redact request_json secrets in the DB to a partial (last-4) form.

        Credential values (``*KEY``/``*PASSWORD``/``*TOKEN``/…) are reduced to
        ``****`` + last 4 chars so the stored/API-served request stays auditable
        (you can confirm *which* credential ran) without persisting the full
        secret; non-credential fields (username / SSID) are kept readable.
        """
        if not self.db_path:
            return
        try:
            import json

            from hil_controller.db.connection import get_db, get_job
            from hil_controller.redact import mask_secret

            sensitive = ("KEY", "TOKEN", "PASSWORD", "SECRET", "PAT")

            async with get_db(self.db_path) as db:
                row = await get_job(db, self.job_id)
                if row:
                    req = json.loads(row["request_json"])
                    if req.get("secrets"):
                        req["secrets"] = {
                            k: (
                                mask_secret(str(v))
                                if any(h in k.upper() for h in sensitive)
                                else str(v)
                            )
                            for k, v in req["secrets"].items()
                        }
                        await db.execute(
                            "UPDATE jobs SET request_json = ? WHERE id = ?",
                            (json.dumps(req), self.job_id),
                        )
                        await db.commit()
        except Exception:
            pass

    async def _emit_protomq_status(self) -> None:
        obs = self._protomq_observer
        if obs is None:
            return
        try:
            status = await obs.get_script_status()
            completed = status.get("completed_steps", [])
            await self._emit(
                "log",
                {
                    "stream": "protomq",
                    "msg": f"completed steps: {completed}",
                    "completed_steps": completed,
                },
            )
            await obs.deactivate()
        except Exception as exc:
            log.warning("ProtoMQ teardown failed: %s", exc)
