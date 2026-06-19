"""UsbFingerprintAdapter — active depower+repower VID/PID capture.

Workflow:
  1. Acquire exclusive_hub lease on the device's hub_host_id.
  2. (Optionally) snapshot baseline `usbip list -l`.
  3. Solenoid all_off → wait for settle → solenoid port_on(channel).
  4. Scan for new VID/PIDs on the device's hub_port_path.
  5. (Optional reset cycle) reset the device, capture role='bootloader',
     wait for re-enum, capture role='runtime'.
  6. Release lease; upsert rows with source='learn-job'.

The adapter takes a `hub` object exposing `all_off()`, `port_on(channel)`,
and `port_off(channel)`, plus a `scan_fn` returning parsed `usbip list -l`
output. In production these are wired to the SSH transport and the
SolenoidHubController in vendor/hil-detection/usb_hub.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timezone
from typing import Any

import aiosqlite

from hil_controller.queue.leases import (
    LeaseConflict,
)
from hil_controller.queue.leases import (
    acquire as acquire_lease,
)
from hil_controller.queue.leases import (
    release as release_lease,
)

log = logging.getLogger(__name__)


class FingerprintError(RuntimeError):
    """Raised when prerequisites for fingerprinting aren't met."""


ScanFn = Callable[[], list[dict[str, Any]] | Awaitable[list[dict[str, Any]]]]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _await_maybe(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


class UsbFingerprintAdapter:
    def __init__(
        self,
        *,
        db_path: str,
        hub: Any,
        scan_fn: ScanFn,
        settle_s: float = 2.0,
        reset_settle_s: float = 1.5,
    ) -> None:
        self.db_path = db_path
        self.hub = hub
        self.scan_fn = scan_fn
        self.settle_s = settle_s
        self.reset_settle_s = reset_settle_s

    async def _scan(self) -> list[dict[str, Any]]:
        return await _await_maybe(self.scan_fn()) or []

    async def _device_row(self, device_id: str) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT hub_host_id, host_id, hub_port_path, solenoid_channel "
                "FROM devices WHERE id=?",
                (device_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            raise FingerprintError(f"unknown device {device_id}")
        d = dict(row)
        d["hub_host_id"] = d["hub_host_id"] or d["host_id"]
        if not d["hub_port_path"]:
            raise FingerprintError(f"device {device_id} has no hub_port_path; cannot fingerprint")
        return d

    async def _capture_for_port(self, hub_port_path: str) -> list[dict[str, Any]]:
        entries = await self._scan()
        return [e for e in entries if e.get("busid") == hub_port_path]

    async def _upsert(
        self,
        device_id: str,
        captured: list[dict[str, Any]],
        *,
        role: str,
        job_id: str | None,
    ) -> list[dict[str, Any]]:
        now = _now_iso()
        out: list[dict[str, Any]] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            for e in captured:
                vid = (e.get("vid") or "").lower()
                pid = (e.get("pid") or "").lower()
                if not (vid and pid):
                    continue
                description = e.get("description") or None
                async with db.execute(
                    "SELECT id FROM device_usb_ids "
                    "WHERE device_id=? AND vid=? AND pid=? "
                    "AND COALESCE(iserial,'')=''",
                    (device_id, vid, pid),
                ) as cur:
                    existing = await cur.fetchone()
                if existing:
                    await db.execute(
                        "UPDATE device_usb_ids "
                        "SET role=?, description=COALESCE(description, ?), "
                        "    last_seen_at=?, learned_from_job=?, source='learn-job' "
                        "WHERE id=?",
                        (role, description, now, job_id, existing["id"]),
                    )
                    row_id = existing["id"]
                else:
                    cur = await db.execute(
                        "INSERT INTO device_usb_ids "
                        "(device_id, vid, pid, role, description, "
                        " first_seen_at, last_seen_at, learned_from_job, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'learn-job')",
                        (device_id, vid, pid, role, description, now, now, job_id),
                    )
                    row_id = cur.lastrowid
                async with db.execute("SELECT * FROM device_usb_ids WHERE id=?", (row_id,)) as cur:
                    row = await cur.fetchone()
                out.append(dict(row))
            await db.commit()
        return out

    async def learn(
        self,
        *,
        device_id: str,
        job_id: str | None = None,
        include_reset_cycle: bool = False,
    ) -> list[dict[str, Any]]:
        """Drive the depower/repower sequence and upsert observations."""
        device = await self._device_row(device_id)
        hub_host_id = device["hub_host_id"]
        port_path = device["hub_port_path"]
        channel = device["solenoid_channel"]

        # Try to claim the hub exclusively. Conflicts surface to the caller.
        try:
            lease = await acquire_lease(
                self.db_path,
                kind="exclusive_hub",
                hub_host_id=hub_host_id,
                job_id=job_id,
            )
        except LeaseConflict:
            raise

        lease_id = lease["id"]
        upserted: list[dict[str, Any]] = []
        try:
            await _await_maybe(self.hub.all_off())
            await asyncio.sleep(self.settle_s)
            # Baseline scan (informational only; we capture new appearances next).
            try:
                await self._capture_for_port(port_path)
            except Exception as exc:
                log.debug("baseline scan failed: %s", exc)

            if channel is not None:
                await _await_maybe(self.hub.port_on(channel))
            await asyncio.sleep(self.settle_s)

            if include_reset_cycle:
                # First wave is treated as bootloader (board often boots into
                # bootloader on cold power-up depending on firmware).
                first = await self._capture_for_port(port_path)
                upserted.extend(
                    await self._upsert(device_id, first, role="bootloader", job_id=job_id)
                )
                # Issue a quick power cycle as a stand-in for a reset signal —
                # in real deployments a bossac double-tap would happen here.
                if channel is not None:
                    await _await_maybe(self.hub.port_off(channel))
                    await asyncio.sleep(self.reset_settle_s)
                    await _await_maybe(self.hub.port_on(channel))
                    await asyncio.sleep(self.reset_settle_s)
                second = await self._capture_for_port(port_path)
                upserted.extend(
                    await self._upsert(device_id, second, role="runtime", job_id=job_id)
                )
            else:
                observed = await self._capture_for_port(port_path)
                upserted.extend(
                    await self._upsert(device_id, observed, role="unknown", job_id=job_id)
                )
        finally:
            try:
                await release_lease(self.db_path, lease_id)
            except Exception as exc:
                log.warning("failed to release fingerprint lease: %s", exc)

        return upserted
