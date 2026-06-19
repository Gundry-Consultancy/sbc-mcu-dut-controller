"""Device/hub exclusivity leases.

A lease is a durable claim recorded in `device_leases`:

- `exclusive_device` — a job exclusively owns one DUT. Conflicts with another
  active lease on the same device, or with an `exclusive_hub` lease on its hub.
- `exclusive_hub`    — a job (typically USB-fingerprint learn-mode) owns the
  whole hub, blocking *any* device lease on that hub.

Conflict checks are done inside an IMMEDIATE transaction so two callers cannot
both acquire conflicting leases.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timezone
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

VALID_KINDS = {"exclusive_device", "exclusive_hub"}
ACTIVE_JOB_STATES = ("queued", "assigned", "preparing", "flashing", "running")


class LeaseConflict(RuntimeError):
    """Raised when an acquire collides with an existing active lease."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _resolve_hub_host(db: aiosqlite.Connection, device_id: str) -> str | None:
    async with db.execute(
        "SELECT COALESCE(hub_host_id, host_id) AS hh FROM devices WHERE id=?",
        (device_id,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def _row_to_dict(db: aiosqlite.Connection, lease_id: int) -> dict[str, Any]:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM device_leases WHERE id=?", (lease_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else {}


async def acquire(
    db_path: str,
    *,
    kind: str,
    device_id: str | None = None,
    hub_host_id: str | None = None,
    job_id: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Atomic acquire. Raises LeaseConflict if blocked."""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}")
    if kind == "exclusive_device" and not device_id:
        raise ValueError("exclusive_device requires device_id")
    if kind == "exclusive_hub" and not hub_host_id:
        raise ValueError("exclusive_hub requires hub_host_id")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            if kind == "exclusive_device":
                # Resolve hub for conflict checks + audit.
                hub = hub_host_id or await _resolve_hub_host(db, device_id)
                # Conflict: any active lease on this device, or active hub lease on its hub.
                async with db.execute(
                    "SELECT id, kind FROM device_leases "
                    "WHERE released_at IS NULL AND "
                    "      (device_id = ? OR (kind='exclusive_hub' AND hub_host_id = ?))",
                    (device_id, hub),
                ) as cur:
                    conflict = await cur.fetchone()
                if conflict:
                    raise LeaseConflict(
                        f"device {device_id} blocked by lease #{conflict['id']} "
                        f"({conflict['kind']})"
                    )
                cur = await db.execute(
                    "INSERT INTO device_leases "
                    "(device_id, hub_host_id, job_id, kind, acquired_at, expires_at) "
                    "VALUES (?, ?, ?, 'exclusive_device', ?, ?)",
                    (device_id, hub, job_id, _now_iso(), expires_at),
                )
            else:  # exclusive_hub
                # Conflict: any active lease on this hub (device or hub kind).
                async with db.execute(
                    "SELECT id, kind FROM device_leases "
                    "WHERE released_at IS NULL AND hub_host_id = ?",
                    (hub_host_id,),
                ) as cur:
                    conflict = await cur.fetchone()
                if conflict:
                    raise LeaseConflict(
                        f"hub {hub_host_id} blocked by lease #{conflict['id']} ({conflict['kind']})"
                    )
                cur = await db.execute(
                    "INSERT INTO device_leases "
                    "(device_id, hub_host_id, job_id, kind, acquired_at, expires_at) "
                    "VALUES (NULL, ?, ?, 'exclusive_hub', ?, ?)",
                    (hub_host_id, job_id, _now_iso(), expires_at),
                )

            new_id = cur.lastrowid
            await db.commit()
            return await _row_to_dict(db, new_id)
        except Exception:
            await db.rollback()
            raise


async def release(db_path: str, lease_id: int) -> bool:
    """Mark a lease released. Returns True if a row was actually updated."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "UPDATE device_leases SET released_at=? WHERE id=? AND released_at IS NULL",
            (_now_iso(), lease_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def renew(db_path: str, lease_id: int, *, expires_at: str | None) -> bool:
    """Set a (still-active) lease's ``expires_at``. Returns True if updated.

    Used by interactive holds (firmware-bench) to extend the window: the lease
    is the durable source of truth for the deadline, and the holding adapter
    polls ``expires_at`` to decide when to wind down. Passing ``None`` clears
    the expiry (an unbounded hold).
    """
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "UPDATE device_leases SET expires_at=? WHERE id=? AND released_at IS NULL",
            (expires_at, lease_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_active_for_job(db_path: str, job_id: str) -> dict[str, Any] | None:
    """Return the most recent active lease held by *job_id*, or ``None``.

    The extend endpoint and the firmware-bench hold-loop use this to find the
    lease whose ``expires_at`` governs the window.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM device_leases "
            "WHERE job_id=? AND released_at IS NULL ORDER BY id DESC LIMIT 1",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_active(db_path: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM device_leases WHERE released_at IS NULL ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def list_all(db_path: str, limit: int = 200) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM device_leases ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def startup_sweep(db_path: str) -> int:
    """Release any active lease whose job is not in an active state.

    Called at controller boot — leases held by crashed jobs would otherwise
    block new work indefinitely. Returns the number of leases released.
    """
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "UPDATE device_leases "
            "SET released_at=? "
            "WHERE released_at IS NULL AND ("
            "  job_id IS NULL OR "
            "  job_id NOT IN (SELECT id FROM jobs WHERE state IN "
            f"   ({','.join('?' * len(ACTIVE_JOB_STATES))}))"
            ")",
            (_now_iso(), *ACTIVE_JOB_STATES),
        )
        await db.commit()
        n = cur.rowcount
    if n:
        log.info("startup_sweep released %d orphan device_leases", n)
    return n
