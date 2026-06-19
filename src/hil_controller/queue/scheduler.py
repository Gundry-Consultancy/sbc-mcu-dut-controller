"""In-process asyncio scheduler: assigns queued jobs to available hosts."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC
from typing import Any

log = logging.getLogger(__name__)


class Scheduler:
    """Picks queued jobs from the DB and dispatches workers."""

    def __init__(
        self,
        db_path: str,
        event_bus: Any,
        host_registry: Any | None = None,
    ) -> None:
        self.db_path = db_path
        self.event_bus = event_bus
        self.host_registry = host_registry
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._active: dict[str, asyncio.Task] = {}
        self._running = False
        self._dispatch_task: asyncio.Task | None = None

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel the running worker task for *job_id*, if one is active.

        Marking a job ``cancelled`` in the DB is not enough to stop a worker
        mid-flight — especially an interactive firmware-bench hold, which has no
        total timeout and can sit for minutes in its bootloader-entry loop. This
        cancels the actual asyncio task; the worker handles ``CancelledError`` by
        running its ``release()`` teardown (stop serial/protomq, free the port,
        release the lease). Returns True if a task was signalled.
        """
        task = self._active.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def start(self) -> None:
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="scheduler-dispatch")

    async def stop(self) -> None:
        self._running = False
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        for task in list(self._active.values()):
            task.cancel()
        if self._active:
            await asyncio.gather(*self._active.values(), return_exceptions=True)

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                job_id = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except TimeoutError:
                continue
            asyncio.create_task(self._run_job(job_id), name=f"worker-{job_id}")

    async def _run_job(self, job_id: str) -> None:
        from hil_controller.db.connection import get_db, get_job, update_job_state
        from hil_controller.queue.leases import (
            LeaseConflict,
        )
        from hil_controller.queue.leases import (
            acquire as acquire_lease,
        )
        from hil_controller.queue.leases import (
            release as release_lease,
        )
        from hil_controller.queue.worker import JobWorker

        adapter = await self._resolve_adapter(job_id)

        async with get_db(self.db_path) as db:
            row = await get_job(db, job_id)
        if row is None:
            return

        import json

        request = json.loads(row["request_json"])

        # Acquire an exclusive_device lease if the adapter resolved a device.
        # The scheduler is single-process so the in-memory semaphore is the
        # primary concurrency guard; the lease is durable + auditable + the
        # signal used by passive USB-ID learn.
        lease_id: int | None = None
        assigned_device = row.get("assigned_device")
        learn_task: asyncio.Task | None = None
        # Interactive holds (firmware-bench) get a time-boxed lease from the
        # start; the holding adapter polls expires_at and the extend endpoint
        # bumps it. Other jobs hold an open-ended lease for their lifetime.
        initial_expires_at = self._interactive_window_expiry(request)
        if assigned_device:
            try:
                lease = await acquire_lease(
                    self.db_path,
                    kind="exclusive_device",
                    device_id=assigned_device,
                    job_id=job_id,
                    expires_at=initial_expires_at,
                )
                lease_id = lease["id"]
                learn_task = await self._maybe_start_passive_learn(job_id, assigned_device, adapter)
            except LeaseConflict as exc:
                log.warning("could not acquire lease for job %s: %s", job_id, exc)

        worker = JobWorker(
            job_id=job_id,
            adapter=adapter,
            event_bus=self.event_bus,
            script=request.get("script", "git-clone-and-run"),
            params=request.get("params") or {},
            payload=request.get("payload") or {},
            timeouts=request.get("timeouts") or {"total_s": 1800},
            db_path=self.db_path,
        )
        self._active[job_id] = asyncio.current_task()  # type: ignore[assignment]
        try:
            await worker.run()
        finally:
            if learn_task is not None:
                learn_task.cancel()
                try:
                    await learn_task
                except (asyncio.CancelledError, Exception):
                    pass
            if lease_id is not None:
                try:
                    await release_lease(self.db_path, lease_id)
                except Exception as exc:
                    log.warning("release_lease failed for job %s: %s", job_id, exc)
            self._active.pop(job_id, None)
            self.event_bus.cleanup(job_id)

    async def _maybe_start_passive_learn(
        self, job_id: str, device_id: str, adapter: Any
    ) -> asyncio.Task | None:
        """Start a background passive-learn loop if we can build a scan_fn.

        Best-effort: returns None when the device has no hub_port_path or
        we can't get a transport for the hub host. Never raises.
        """
        try:
            from hil_controller.adapters.usb_scan import (
                make_ssh_scan_fn,
                passive_learn_loop,
            )
            from hil_controller.db.connection import get_db

            async with get_db(self.db_path) as db:
                async with db.execute(
                    "SELECT hub_host_id, hub_port_path FROM devices WHERE id=?",
                    (device_id,),
                ) as cur:
                    drow = await cur.fetchone()
            if drow is None or not drow["hub_port_path"]:
                return None

            transport = getattr(adapter, "transport", None)
            if transport is None or not hasattr(transport, "run"):
                return None

            scan_fn = await make_ssh_scan_fn(transport, drow["hub_host_id"])
            return asyncio.create_task(
                passive_learn_loop(
                    self.db_path,
                    device_id=device_id,
                    hub_port_path=drow["hub_port_path"],
                    job_id=job_id,
                    scan_fn=scan_fn,
                ),
                name=f"learn-{job_id}",
            )
        except Exception as exc:
            log.debug("passive learn could not start for %s: %s", job_id, exc)
            return None

    @staticmethod
    def _interactive_window_expiry(request: dict[str, Any]) -> str | None:
        """ISO expiry for an interactive hold's lease, or None for normal jobs.

        Window length comes from ``params.window_minutes`` (default 30).
        """
        from hil_controller.queue.worker import JobWorker

        if request.get("script") not in JobWorker.INTERACTIVE_SCRIPTS:
            return None
        from datetime import datetime, timedelta, timezone

        minutes = (request.get("params") or {}).get("window_minutes", 30)
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 30
        return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()

    async def _resolve_adapter(self, job_id: str):  # noqa: ANN201
        """Return the appropriate adapter for this job.

        Falls back to a FakeAdapter when no real host registry is configured,
        which is useful for unit tests and local dev.
        """
        if self.host_registry is not None:
            return await self.host_registry.get_adapter(job_id)
        return _FakeAdapter()


class _FakeAdapter:
    async def acquire(self) -> None:
        await asyncio.sleep(0)

    async def reset(self) -> None:
        await asyncio.sleep(0)

    async def flash(self, artifact: dict) -> None:
        await asyncio.sleep(0)

    async def open_serial(self):
        return iter([])

    async def release(self) -> None:
        await asyncio.sleep(0)
