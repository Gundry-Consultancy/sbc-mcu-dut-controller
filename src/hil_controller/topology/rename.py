"""Cascade-rename a host or device id, rewriting every referencing column.

The schema declares its foreign keys without ``ON UPDATE CASCADE``, so renaming
a primary key has to repoint each referencing column by hand. We do it the way
the topology seeder does a bulk rewrite: flip ``PRAGMA foreign_keys=OFF`` for the
duration of one transaction, update the PK and every reference, then turn FKs
back on and commit. With FKs off there's no mid-transaction violation as rows
briefly point at both ids.

The reference map (kept in lockstep with ``db/schema.sql``):

* **hosts.id**  ← devices.host_id, devices.hub_host_id, cameras.host_id,
  jobs.assigned_host, device_leases.hub_host_id
* **devices.id** ← device_usb_ids.device_id, device_leases.device_id,
  connections.device_id, camera_rois.device_id, device_peripherals.device_id,
  jobs.assigned_device

Old job/audit history stays linked because ``assigned_host`` / ``assigned_device``
are rewritten too. Both functions raise ``ValueError`` for a bad/blank new id,
``LookupError`` if the row is missing, and ``KeyError`` if the target id is taken.
"""

from __future__ import annotations

import json
import logging
import re

import aiosqlite

from hil_controller.db.connection import now_iso

log = logging.getLogger(__name__)

#: A conservative id charset (matches the "unique slug, no spaces" UI hint).
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: (table, column) pairs that reference hosts.id / devices.id.
_HOST_REFS = [
    ("devices", "host_id"),
    ("devices", "hub_host_id"),
    ("cameras", "host_id"),
    ("jobs", "assigned_host"),
    ("device_leases", "hub_host_id"),
]
_DEVICE_REFS = [
    ("device_usb_ids", "device_id"),
    ("device_leases", "device_id"),
    ("connections", "device_id"),
    ("camera_rois", "device_id"),
    ("device_peripherals", "device_id"),
    ("jobs", "assigned_device"),
]


def _validate(new_id: str) -> str:
    new_id = (new_id or "").strip()
    if not new_id:
        raise ValueError("new id is required")
    if not _ID_RE.match(new_id):
        raise ValueError("id may only contain letters, digits, '.', '_' and '-' (no spaces)")
    return new_id


async def _exists(db: aiosqlite.Connection, table: str, id_: str) -> bool:
    async with db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (id_,)) as cur:
        return await cur.fetchone() is not None


async def _rename(
    db: aiosqlite.Connection,
    *,
    table: str,
    old_id: str,
    new_id: str,
    refs: list[tuple[str, str]],
) -> int:
    """Shared core: rename ``table``.id and repoint every (table,col) in ``refs``.

    Returns the total number of referencing rows repointed. Caller owns the
    transaction + the foreign_keys pragma.
    """
    total = 0
    await db.execute(f"UPDATE {table} SET id = ? WHERE id = ?", (new_id, old_id))
    for ref_table, ref_col in refs:
        try:
            cur = await db.execute(
                f"UPDATE {ref_table} SET {ref_col} = ? WHERE {ref_col} = ?", (new_id, old_id)
            )
            total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except aiosqlite.OperationalError:
            # A referencing table that doesn't exist yet (older DB) is a no-op.
            log.debug("rename: skipped missing table %s", ref_table)
    return total


async def _audit(
    db: aiosqlite.Connection, event: str, old_id: str, new_id: str, refs_updated: int
) -> None:
    await db.execute(
        "INSERT INTO audit_log (at, event, subject, repo, entity_id, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            event,
            "rename",
            "",
            new_id,
            json.dumps({"old_id": old_id, "new_id": new_id, "refs_updated": refs_updated}),
        ),
    )


async def rename_host(db: aiosqlite.Connection, old_id: str, new_id: str) -> int:
    """Rename a host id, cascading to all references. Returns refs repointed.

    No-op (returns 0) if ``old_id == new_id``.
    """
    new_id = _validate(new_id)
    if old_id == new_id:
        return 0
    if not await _exists(db, "hosts", old_id):
        raise LookupError(f"host not found: {old_id}")
    if await _exists(db, "hosts", new_id):
        raise KeyError(f"host id already exists: {new_id}")

    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        n = await _rename(db, table="hosts", old_id=old_id, new_id=new_id, refs=_HOST_REFS)
        await _audit(db, "host.rename", old_id, new_id, n)
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys=ON")
    log.info("renamed host %s → %s (%d references repointed)", old_id, new_id, n)
    return n


async def rename_device(db: aiosqlite.Connection, old_id: str, new_id: str) -> int:
    """Rename a device id, cascading to all references. Returns refs repointed.

    No-op (returns 0) if ``old_id == new_id``.
    """
    new_id = _validate(new_id)
    if old_id == new_id:
        return 0
    if not await _exists(db, "devices", old_id):
        raise LookupError(f"device not found: {old_id}")
    if await _exists(db, "devices", new_id):
        raise KeyError(f"device id already exists: {new_id}")

    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        n = await _rename(db, table="devices", old_id=old_id, new_id=new_id, refs=_DEVICE_REFS)
        await _audit(db, "device.rename", old_id, new_id, n)
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys=ON")
    log.info("renamed device %s → %s (%d references repointed)", old_id, new_id, n)
    return n
