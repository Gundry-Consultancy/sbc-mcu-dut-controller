"""Async SQLite connection pool and schema initialiser."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = Path(__file__).parent / "schema.sql"


async def init_db(db_path: str) -> None:
    """Create tables and apply additive migrations."""
    async with aiosqlite.connect(db_path) as db:
        sql = _SCHEMA.read_text()
        await db.executescript(sql)
        await db.commit()
        await _migrate(db)


async def _migrate(db: aiosqlite.Connection) -> None:
    """Add columns introduced after the initial schema, safe to re-run."""
    token_cols = [
        ("allowed_pools", "TEXT NOT NULL DEFAULT '[]'"),
        ("allowed_profiles", "TEXT NOT NULL DEFAULT '[]'"),
        ("default_profile", "TEXT NOT NULL DEFAULT 'bench-protomq'"),
        ("capabilities", "TEXT NOT NULL DEFAULT '[]'"),
    ]
    for col, defn in token_cols:
        try:
            await db.execute(f"ALTER TABLE tokens ADD COLUMN {col} {defn}")
            await db.commit()
        except Exception:
            pass  # column already exists

    # streams_json: list of {url, type} dicts — used by camera aux items.
    try:
        await db.execute("ALTER TABLE auxes ADD COLUMN streams_json TEXT")
        await db.commit()
    except Exception:
        pass

    # cameras.kind: focus-driver selector ('pi-camera-server' | 'ip-webcam' |
    # NULL=auto-detect from source URL). See adapters/camera/focus_drivers.py.
    try:
        await db.execute("ALTER TABLE cameras ADD COLUMN kind TEXT")
        await db.commit()
    except Exception:
        pass  # column already exists

    # camera_id: FK to cameras table; qr_identifier: QR URL for auto-ROI.
    # manual_focus / illuminator_brightness: per-device overrides the camera
    # orchestrator combines across devices sharing one camera and pushes to the
    # camera via its focus driver (mean focus / max brightness). manual_focus is
    # in the *assigned camera's native units* — dioptres for libcamera (Pi
    # camera-server), 0..10 focus_distance for the Android IP Webcam — so it does
    # NOT auto-translate if a device moves to a different camera kind.
    #
    # Rename the legacy manual_focus_dioptres column (the "dioptre" unit was only
    # ever true for the Pi). Idempotent: errors when the old column is absent
    # (fresh DB, or already renamed) and falls through to the additive ADD below.
    try:
        await db.execute("ALTER TABLE devices RENAME COLUMN manual_focus_dioptres TO manual_focus")
        await db.commit()
    except Exception:
        pass

    for col, defn in [
        ("camera_id", "TEXT"),
        ("qr_identifier", "TEXT"),
        ("manual_focus", "REAL"),
        ("illuminator_brightness", "INTEGER"),
    ]:
        try:
            await db.execute(f"ALTER TABLE devices ADD COLUMN {col} {defn}")
            await db.commit()
        except Exception:
            pass

    # camera_rois: frame-relative ROI — record the frame size the ROI was drawn
    # on so consumers can scale it to any capture resolution. Additive; no-op
    # when already present.
    for col, defn in [
        ("roi_frame_width", "INTEGER"),
        ("roi_frame_height", "INTEGER"),
    ]:
        try:
            await db.execute(f"ALTER TABLE camera_rois ADD COLUMN {col} {defn}")
            await db.commit()
        except Exception:
            pass  # column already exists

    # peripherals + device_peripherals — added alongside topology peripherals section.
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS peripherals (
                id          TEXT PRIMARY KEY,
                kind        TEXT NOT NULL DEFAULT 'display',
                model       TEXT NOT NULL DEFAULT '',
                product_url TEXT,
                specs_json  TEXT,
                notes       TEXT
            )
            """
        )
        await db.commit()
    except Exception:
        pass

    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS device_peripherals (
                device_id     TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                peripheral_id TEXT NOT NULL REFERENCES peripherals(id) ON DELETE CASCADE,
                PRIMARY KEY (device_id, peripheral_id)
            )
            """
        )
        await db.commit()
    except Exception:
        pass

    # I2C component *strands*: a shared I2C chain the analog strand-mux (an aux)
    # routes to one DUT at a time. A strand may itself carry an on-strand TCA9548
    # (8-ch I2C address mux) for its components — modelled here, driven later.
    # See adapters/analog_mux.py and the select_i2c_strand bench stage.
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS strands (
                id          TEXT PRIMARY KEY,
                mux_aux_id  TEXT,                       -- analog strand-mux aux (auxes.id)
                mux_group   TEXT,                       -- switch group on that mux (e.g. "muxA")
                tca_address INTEGER,                    -- on-strand TCA9548 addr (NULL = none)
                pool        TEXT NOT NULL DEFAULT 'public',
                status      TEXT NOT NULL DEFAULT 'available',
                notes       TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS strand_components (
                id                TEXT PRIMARY KEY,
                strand_id         TEXT NOT NULL REFERENCES strands(id) ON DELETE CASCADE,
                model             TEXT NOT NULL DEFAULT '',   -- e.g. "pmsa003i", "sgp41"
                address           INTEGER,                    -- I2C address (e.g. 0x12, 0x59)
                tca_channel       INTEGER,                    -- NULL=direct; else TCA ch
                ws_types_json     TEXT NOT NULL DEFAULT '[]', -- WS sensor Type ints
                capabilities_json TEXT NOT NULL DEFAULT '[]', -- e.g. ["sensor:pm25","sensor:voc"]
                notes             TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS device_strands (
                device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                strand_id   TEXT NOT NULL REFERENCES strands(id) ON DELETE CASCADE,
                mux_channel INTEGER NOT NULL,           -- analog-mux channel for this DUT
                PRIMARY KEY (device_id, strand_id)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_strand_components_strand "
            "ON strand_components(strand_id)"
        )
        await db.commit()
    except Exception:
        pass

    # Migrate existing auxes (kind='camera') to the cameras table.
    try:
        await db.execute(
            """
            INSERT OR IGNORE INTO cameras (id, host_id, source, model, pool, status, streams_json)
            SELECT id, NULL, COALESCE(interface, ''), model, pool, status, streams_json
            FROM auxes WHERE kind = 'camera'
            """
        )
        await db.commit()
    except Exception:
        pass

    # USB hub-port identity + multi-VID/PID support.
    for col, defn in [
        ("hub_host_id", "TEXT"),
        ("hub_port_path", "TEXT"),
        ("solenoid_channel", "INTEGER"),
        ("usb_serial", "TEXT"),
    ]:
        try:
            await db.execute(f"ALTER TABLE devices ADD COLUMN {col} {defn}")
            await db.commit()
        except Exception:
            pass

    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS device_usb_ids (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id        TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                vid              TEXT NOT NULL,
                pid              TEXT NOT NULL,
                role             TEXT NOT NULL DEFAULT 'unknown',
                bcd_device       TEXT,
                description      TEXT,
                iserial          TEXT,
                first_seen_at    TEXT NOT NULL,
                last_seen_at     TEXT NOT NULL,
                learned_from_job TEXT,
                source           TEXT NOT NULL DEFAULT 'manual'
            )
            """
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_device_usb_ids_combo "
            "ON device_usb_ids(device_id, vid, pid, COALESCE(iserial, ''))"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_device_usb_ids_lookup ON device_usb_ids(vid, pid)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_device_usb_ids_device ON device_usb_ids(device_id)"
        )
        await db.commit()
    except Exception:
        pass

    # Backfill device_usb_ids from any pre-existing usb_json values.
    try:
        await _backfill_usb_ids(db)
    except Exception:
        pass

    # Device availability & self-rectification columns (see
    # docs/device-availability.md). Additive; no-op when already present.
    for col, defn in [
        ("unavailable_kind", "TEXT"),
        ("unavailable_reason", "TEXT"),
        ("unavailable_since", "TEXT"),
        ("retry_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("retry_after", "TEXT"),
        ("last_checked_at", "TEXT"),
        # arduino-cli build-target name (e.g. "qtpy_esp32s3_n4r2") so /v1/targets
        # keys match the build job's artifacts; see docs/device-availability.md.
        ("build_target", "TEXT"),
    ]:
        try:
            await db.execute(f"ALTER TABLE devices ADD COLUMN {col} {defn}")
            await db.commit()
        except Exception:
            pass  # column already exists

    # Host hardware: auto-detected specs + live load + work-speed score.
    # See host_hardware.py. Additive; no-op when already present.
    for col, defn in [
        ("hw_detected_json", "TEXT"),
        ("hw_override_json", "TEXT"),
        ("load_json", "TEXT"),
        ("speed_score", "REAL"),
        ("speed_score_at", "TEXT"),
        ("specs_detected_at", "TEXT"),
    ]:
        try:
            await db.execute(f"ALTER TABLE hosts ADD COLUMN {col} {defn}")
            await db.commit()
        except Exception:
            pass  # column already exists

    # Device/hub exclusivity leases.
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS device_leases (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id     TEXT REFERENCES devices(id) ON DELETE CASCADE,
                hub_host_id   TEXT,
                job_id        TEXT,
                kind          TEXT NOT NULL,
                acquired_at   TEXT NOT NULL,
                expires_at    TEXT,
                released_at   TEXT
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_device_leases_active_dev "
            "ON device_leases(device_id) WHERE released_at IS NULL"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_device_leases_active_hub "
            "ON device_leases(hub_host_id) WHERE released_at IS NULL"
        )
        await db.commit()
    except Exception:
        pass


async def _backfill_usb_ids(db: aiosqlite.Connection) -> None:
    """Copy single-{vid,pid} usb_json rows into device_usb_ids if not already present."""
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT id, usb_json FROM devices WHERE usb_json IS NOT NULL AND usb_json != ''"
    ) as cur:
        rows = await cur.fetchall()
    now = now_iso()
    for r in rows:
        try:
            data = json.loads(r["usb_json"]) or {}
        except Exception:
            continue
        vid = (data.get("vid") or "").strip().lower()
        pid = (data.get("pid") or "").strip().lower()
        if not (vid and pid):
            continue
        async with db.execute(
            "SELECT 1 FROM device_usb_ids WHERE device_id=? AND vid=? AND pid=? "
            "AND COALESCE(iserial,'')=''",
            (r["id"], vid, pid),
        ) as cur:
            exists = await cur.fetchone()
        if exists:
            continue
        await db.execute(
            "INSERT INTO device_usb_ids "
            "(device_id, vid, pid, role, first_seen_at, last_seen_at, source) "
            "VALUES (?, ?, ?, 'unknown', ?, ?, 'migration')",
            (r["id"], vid, pid, now, now),
        )
    await db.commit()


@asynccontextmanager
async def get_db(db_path: str) -> AsyncGenerator[aiosqlite.Connection, None]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        yield db


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def insert_job(
    db: aiosqlite.Connection,
    *,
    job_id: str,
    request_json: dict[str, Any],
    secrets_profile: str,
    exclusive_host: bool,
    submitted_by: str = "",
    repo: str = "",
) -> None:
    await db.execute(
        """
        INSERT INTO jobs (id, submitted_by, repo, request_json, secrets_profile,
                          exclusive_host, state, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
        """,
        (
            job_id,
            submitted_by,
            repo,
            json.dumps(request_json),
            secrets_profile,
            int(exclusive_host),
            now_iso(),
        ),
    )
    await db.commit()


async def get_job(db: aiosqlite.Connection, job_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return dict(row)


async def update_job_state(
    db: aiosqlite.Connection,
    job_id: str,
    state: str,
    *,
    result: str | None = None,
    assigned_host: str | None = None,
    assigned_device: str | None = None,
    summary: str | None = None,
) -> None:
    fields = ["state = ?"]
    values: list[Any] = [state]

    if state in ("running", "assigned", "preparing", "flashing") and not assigned_host:
        pass
    if state in ("assigned", "preparing", "flashing", "running"):
        fields.append("started_at = COALESCE(started_at, ?)")
        values.append(now_iso())
    if state in ("finished", "error", "timeout", "cancelled"):
        fields.append("finished_at = ?")
        values.append(now_iso())
    if result is not None:
        fields.append("result = ?")
        values.append(result)
    if assigned_host is not None:
        fields.append("assigned_host = ?")
        values.append(assigned_host)
    if assigned_device is not None:
        fields.append("assigned_device = ?")
        values.append(assigned_device)
    if summary is not None:
        fields.append("summary = ?")
        values.append(summary)

    values.append(job_id)
    await db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
    await db.commit()


async def append_event(
    db: aiosqlite.Connection,
    job_id: str,
    kind: str,
    payload: dict[str, Any],
) -> int:
    import sqlite3

    for _ in range(10):
        async with db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM events WHERE job_id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
            seq = row[0] if row else 0

        try:
            await db.execute(
                "INSERT INTO events (job_id, seq, at, kind, payload_json) VALUES (?, ?, ?, ?, ?)",
                (job_id, seq, now_iso(), kind, json.dumps(payload)),
            )
            await db.commit()
            return seq
        except (aiosqlite.IntegrityError, sqlite3.IntegrityError):
            # Concurrent writer took this seq — re-read MAX and retry.
            continue

    raise RuntimeError(f"Failed to append event for job {job_id} after 10 retries")


async def get_events_since(
    db: aiosqlite.Connection, job_id: str, since: int
) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT seq, at, kind, payload_json FROM events WHERE job_id = ? AND seq > ? ORDER BY seq",
        (job_id, since),
    ) as cur:
        rows = await cur.fetchall()
        return [
            {
                "seq": r["seq"],
                "at": r["at"],
                "kind": r["kind"],
                "payload": json.loads(r["payload_json"]),
            }
            for r in rows
        ]


async def audit_event(
    db: aiosqlite.Connection,
    event: str,
    *,
    subject: str = "",
    repo: str = "",
    entity_id: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        "INSERT INTO audit_log (at, event, subject, repo, entity_id, detail_json) VALUES (?, ?, ?, ?, ?, ?)",  # noqa: E501
        (now_iso(), event, subject, repo, entity_id, json.dumps(detail or {})),
    )
    await db.commit()
