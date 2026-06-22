"""Per-camera control orchestrator.

When devices that share a camera have manual focus or illuminator brightness
overrides — or a job is actively capturing visual assets — libcamera / the
NeoPixel ring need a single effective setting. This module decides what each
camera should focus on and how bright the ring should be, then dispatches that
to the camera through its per-kind :mod:`focus_drivers` driver.

Focus directive precedence (per camera):
  1. an explicit request for a specific device (``prefer_device_id`` — the
     "focus this DUT now" API path);
  2. else the most-recently-created active job on the camera whose job declares
     visual asset requirements — window AF on that device's ROI;
  3. else, if active devices set a manual focus value, the *mean* of those
     values (full-frame manual focus);
  4. else plain continuous auto-focus.

Brightness stays the max of all illuminator_brightness across active devices.

Network failures are swallowed by the drivers — the camera is a best-effort
peripheral; a job must never fail because the camera is unreachable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite
import httpx

from hil_controller.adapters.camera import roi_snapshot
from hil_controller.adapters.camera.focus_drivers import get_driver, resolve_camera_kind

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("preparing", "flashing", "running", "assigned")

# collect_artifacts patterns ending in one of these mark a job as capturing
# visual assets (camera snapshots) — used to pick the AF target on a shared camera.
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


def compute_focus_compromise(values: list[float]) -> float | None:
    """Midpoint of min/max across all non-null manual focus values."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return (min(clean) + max(clean)) / 2.0


def compute_focus_mean(values: list[float | None]) -> float | None:
    """Mean of all non-null manual focus values (shared-camera fallback)."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


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


def _job_wants_visual(request_json: str | None) -> bool:
    """True when a job declares it captures camera images.

    Signalled by ``params.collect_artifacts`` containing an image glob
    (``*.jpg`` / ``*.jpeg`` / ``*.png``) — the same patterns the worker harvests
    as assets after a run.
    """
    if not request_json:
        return False
    try:
        req = json.loads(request_json)
    except (json.JSONDecodeError, TypeError):
        return False
    params = req.get("params") if isinstance(req, dict) else None
    arts = params.get("collect_artifacts") if isinstance(params, dict) else None
    if not isinstance(arts, list):
        return False
    return any(isinstance(p, str) and p.lower().rstrip().endswith(_IMAGE_SUFFIXES) for p in arts)


# ---- DB lookups -----------------------------------------------------------


async def _camera_row(db: aiosqlite.Connection, camera_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT source, kind FROM cameras WHERE id = ?", (camera_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row is not None else None


async def _active_devices_on_camera(
    db: aiosqlite.Connection, camera_id: str
) -> list[dict[str, Any]]:
    """Devices with an active job assignment that share this camera."""
    placeholders = ",".join("?" for _ in ACTIVE_STATES)
    sql = f"""
        SELECT d.id, d.manual_focus, d.illuminator_brightness
        FROM devices d
        JOIN jobs j ON j.assigned_device = d.id
        WHERE d.camera_id = ?
          AND j.state IN ({placeholders})
    """
    async with db.execute(sql, (camera_id, *ACTIVE_STATES)) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def _visual_job_devices(db: aiosqlite.Connection, camera_id: str) -> list[str]:
    """Active devices on this camera whose job captures visual assets, newest first."""
    placeholders = ",".join("?" for _ in ACTIVE_STATES)
    sql = f"""
        SELECT j.assigned_device AS device_id, j.request_json
        FROM jobs j
        JOIN devices d ON d.id = j.assigned_device
        WHERE d.camera_id = ?
          AND j.state IN ({placeholders})
        ORDER BY j.created_at DESC
    """
    async with db.execute(sql, (camera_id, *ACTIVE_STATES)) as cur:
        rows = await cur.fetchall()
    return [r["device_id"] for r in rows if _job_wants_visual(r["request_json"])]


async def _device_roi(db: aiosqlite.Connection, device_id: str) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT x, y, w, h, roi_frame_width, roi_frame_height FROM camera_rois WHERE device_id = ?",
        (device_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row is not None else None


async def _device_manual_focus(db: aiosqlite.Connection, device_id: str) -> float | None:
    async with db.execute(
        "SELECT manual_focus FROM devices WHERE id = ?", (device_id,)
    ) as cur:
        row = await cur.fetchone()
    return row["manual_focus"] if row is not None else None


# ---- directive ------------------------------------------------------------


def _auto_directive() -> dict[str, Any]:
    return {"mode": "auto", "window": None, "position": None, "target_device": None}


async def _window_directive(
    db: aiosqlite.Connection, device_id: str, *, point: bool = False
) -> dict[str, Any] | None:
    """Build a windowed-AF directive from a device's ROI, or None if unusable."""
    roi = await _device_roi(db, device_id)
    if not roi:
        return None
    rect = roi_snapshot.normalize_roi(
        roi["x"],
        roi["y"],
        roi["w"],
        roi["h"],
        roi["roi_frame_width"],
        roi["roi_frame_height"],
    )
    if rect is None:
        return None  # legacy ROI without a reference frame — can't window safely
    if point:
        rect = roi_snapshot.center_box(rect)
    return {"mode": "window", "window": rect, "position": None, "target_device": device_id}


async def compute_focus_directive(
    db: aiosqlite.Connection,
    camera_id: str,
    *,
    prefer_device_id: str | None = None,
    prefer_mode: str | None = None,
    position: float | None = None,
) -> dict[str, Any]:
    """Resolve the effective focus directive for a camera.

    ``prefer_device_id`` forces the decision onto one device (the "focus now"
    path); ``prefer_mode`` is one of ``region`` | ``point`` | ``auto`` |
    ``manual``. Without a preference, the shared-camera precedence chain runs.
    """
    if prefer_device_id:
        mode = prefer_mode or "region"
        if mode == "auto":
            return _auto_directive()
        if mode == "manual":
            pos = (
                position
                if position is not None
                else await _device_manual_focus(db, prefer_device_id)
            )
            if pos is None:
                return _auto_directive()
            return {
                "mode": "manual",
                "window": None,
                "position": float(pos),
                "target_device": prefer_device_id,
            }
        directive = await _window_directive(db, prefer_device_id, point=(mode == "point"))
        return directive or _auto_directive()

    # 1. most-recently-created active visual job with a usable ROI
    for device_id in await _visual_job_devices(db, camera_id):
        directive = await _window_directive(db, device_id)
        if directive is not None:
            return directive

    # 2. mean of manual focus values across active devices
    devices = await _active_devices_on_camera(db, camera_id)
    focus = compute_focus_mean([d["manual_focus"] for d in devices])
    if focus is not None:
        return {"mode": "manual", "window": None, "position": focus, "target_device": None}

    # 3. plain continuous auto
    return _auto_directive()


# ---- dispatch -------------------------------------------------------------


async def recompute_for_camera(
    db: aiosqlite.Connection,
    camera_id: str,
    *,
    prefer_device_id: str | None = None,
    prefer_mode: str | None = None,
    position: float | None = None,
) -> dict[str, Any]:
    """Recompute and push effective focus + illuminator settings for one camera.

    Resolves the camera's focus driver from its kind, builds the focus directive
    via the precedence chain (or an explicit device preference), and dispatches
    both lens and illuminator to the camera. Returns the decision for tests /
    introspection.
    """
    cam = await _camera_row(db, camera_id)
    directive = await compute_focus_directive(
        db, camera_id, prefer_device_id=prefer_device_id, prefer_mode=prefer_mode, position=position
    )
    devices = await _active_devices_on_camera(db, camera_id)
    brightness = compute_brightness_compromise([d["illuminator_brightness"] for d in devices])

    source = cam["source"] if cam else None
    kind = resolve_camera_kind(cam) if cam else "unknown"
    base = camera_base_url(source) if source else None
    driver = get_driver(kind)

    lens_result: dict[str, Any] | None = None
    illum_result: dict[str, Any] | None = None
    if base:
        async with httpx.AsyncClient(timeout=3.0) as client:
            lens_result = await driver.apply(client, base, directive)
            illum_result = await driver.apply_illuminator(client, base, brightness)
    else:
        logger.debug("camera %s has no HTTP base (source=%r); skipping push", camera_id, source)

    return {
        "camera_id": camera_id,
        "base": base,
        "kind": kind,
        "directive": directive,
        "brightness": brightness,
        "device_count": len(devices),
        "lens": lens_result,
        "illuminator": illum_result,
    }


async def recompute_for_device(
    db: aiosqlite.Connection,
    device_id: str,
    *,
    prefer: bool = False,
    prefer_mode: str | None = None,
    position: float | None = None,
) -> dict[str, Any] | None:
    """Look up the device's camera and recompute for it.

    ``prefer=True`` forces this device to drive the focus decision (used by the
    explicit "focus this DUT now" API); otherwise the shared-camera precedence
    chain decides.
    """
    async with db.execute("SELECT camera_id FROM devices WHERE id = ?", (device_id,)) as cur:
        row = await cur.fetchone()
    if row is None or not row["camera_id"]:
        return None
    return await recompute_for_camera(
        db,
        row["camera_id"],
        prefer_device_id=device_id if prefer else None,
        prefer_mode=prefer_mode,
        position=position,
    )
