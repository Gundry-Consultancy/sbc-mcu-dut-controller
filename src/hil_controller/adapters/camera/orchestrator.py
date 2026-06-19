"""Per-camera control orchestrator.

When devices that share a camera have manual focus or illuminator
brightness overrides, libcamera/the NeoPixel ring need a single
effective value. This module queries those overrides for the devices
currently "active" on each camera (assigned to a running/preparing/
flashing job) and pushes the combined settings to the camera server.

Compromise rules:
  * focus       — midpoint of (min, max) of all manual_focus_dioptres
                  values across active devices. When no device has an
                  override, lens returns to auto.
  * brightness  — max of all illuminator_brightness values across
                  active devices. When no device has a value, brightness
                  goes to 0.

Network failures are swallowed — the camera server is a best-effort
peripheral; a job must never fail because the ring is unreachable.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urljoin

import aiosqlite
import httpx

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("preparing", "flashing", "running", "assigned")


def compute_focus_compromise(values: list[float]) -> float | None:
    """Midpoint of min/max across all non-null manual focus values."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return (min(clean) + max(clean)) / 2.0


def compute_brightness_compromise(values: list[int]) -> int | None:
    """Max brightness across active devices."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return max(clean)


def camera_base_url(source: str) -> str | None:
    """Strip path off a camera source URL to get the server base.

    ``http://192.168.1.234:8080/`` -> ``http://192.168.1.234:8080``
    ``http://10.0.0.5:8080/shot.jpg`` -> ``http://10.0.0.5:8080``
    Non-HTTP sources (rtsp://, /dev/video0) return None.
    """
    if not source:
        return None
    if not source.startswith(("http://", "https://")):
        return None
    # Keep scheme://host:port; drop everything else.
    after_scheme = source.split("://", 1)[1]
    host_part = after_scheme.split("/", 1)[0]
    scheme = source.split("://", 1)[0]
    return f"{scheme}://{host_part}"


async def _active_devices_on_camera(
    db: aiosqlite.Connection, camera_id: str
) -> list[dict[str, Any]]:
    """Devices with an active job assignment that share this camera."""
    placeholders = ",".join("?" for _ in ACTIVE_STATES)
    sql = f"""
        SELECT d.id, d.manual_focus_dioptres, d.illuminator_brightness
        FROM devices d
        JOIN jobs j ON j.assigned_device = d.id
        WHERE d.camera_id = ?
          AND j.state IN ({placeholders})
    """
    async with db.execute(sql, (camera_id, *ACTIVE_STATES)) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def _camera_source(db: aiosqlite.Connection, camera_id: str) -> str | None:
    async with db.execute("SELECT source FROM cameras WHERE id = ?", (camera_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["source"] if isinstance(row, aiosqlite.Row) else row[0]


async def _push_lens(base: str, focus: float | None) -> None:
    body: dict[str, Any]
    if focus is None:
        body = {"mode": "auto"}
    else:
        body = {"mode": "manual", "position": focus}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(f"{base}/lens", json=body)
            r.raise_for_status()
    except Exception as exc:
        logger.warning("camera lens push failed (%s): %s", base, exc)


async def _push_illuminator(base: str, brightness: int | None) -> None:
    body = {"brightness": int(brightness) if brightness is not None else 0}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(f"{base}/illuminator", json=body)
            r.raise_for_status()
    except Exception as exc:
        logger.warning("camera illuminator push failed (%s): %s", base, exc)


async def recompute_for_camera(db: aiosqlite.Connection, camera_id: str) -> dict[str, Any]:
    """Recompute effective lens + illuminator settings for one camera.

    Reads device overrides for everything active on this camera, combines
    them via the compromise rules, and POSTs the result to the camera
    server. Returns the computed values for tests / introspection.
    """
    source = await _camera_source(db, camera_id)
    devices = await _active_devices_on_camera(db, camera_id)
    focus = compute_focus_compromise([d["manual_focus_dioptres"] for d in devices])
    brightness = compute_brightness_compromise([d["illuminator_brightness"] for d in devices])
    base = camera_base_url(source) if source else None
    if base:
        await _push_lens(base, focus)
        await _push_illuminator(base, brightness)
    return {
        "camera_id": camera_id,
        "base": base,
        "focus": focus,
        "brightness": brightness,
        "device_count": len(devices),
    }


async def recompute_for_device(db: aiosqlite.Connection, device_id: str) -> dict[str, Any] | None:
    """Look up the device's camera and recompute for it."""
    async with db.execute("SELECT camera_id FROM devices WHERE id = ?", (device_id,)) as cur:
        row = await cur.fetchone()
    if row is None or not row["camera_id"]:
        return None
    return await recompute_for_camera(db, row["camera_id"])
