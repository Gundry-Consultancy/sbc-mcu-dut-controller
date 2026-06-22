"""Camera and ROI management API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from hil_controller.adapters.camera import orchestrator, roi_snapshot
from hil_controller.adapters.camera.focus_drivers import get_driver, resolve_camera_kind
from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db, now_iso

router = APIRouter(tags=["cameras"])
Auth = Annotated[Principal, Depends(require_auth)]


async def _fetch_frame(url: str) -> bytes:
    """Fetch a single frame over HTTP; raises HTTPException(503) on failure."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Failed to fetch frame: {exc}") from exc


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CameraSummary(BaseModel):
    id: str
    host_id: str | None
    source: str
    kind: str | None = None
    model: str
    pool: str
    status: str
    notes: str | None
    streams: list[dict[str, Any]]
    resolution_w: int | None = None
    resolution_h: int | None = None


class ROIResponse(BaseModel):
    device_id: str
    camera_id: str
    x: int
    y: int
    w: int
    h: int
    roi_frame_width: int | None = None
    roi_frame_height: int | None = None
    source: str
    confidence: float | None
    updated_at: str


class ROISetRequest(BaseModel):
    x: int
    y: int
    w: int
    h: int
    # Frame the ROI was drawn on. Omit to have the server detect it from a live
    # snapshot. Stored so the ROI can be scaled to any capture resolution.
    frame_width: int | None = None
    frame_height: int | None = None


class FocusRequest(BaseModel):
    # region  -> window AF on the whole ROI rectangle
    # point   -> window AF on a small box at the ROI centre
    # auto    -> full-frame continuous AF
    # manual  -> fixed focus at ``position`` (camera's native units)
    mode: str = "region"
    position: float | None = None


# ---------------------------------------------------------------------------
# Camera list / detail
# ---------------------------------------------------------------------------


def _parse_streams(row: dict) -> list[dict[str, Any]]:
    raw = row.get("streams_json")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    if row.get("source"):
        return [{"url": row["source"], "type": "snapshot"}]
    return []


def _camera_summary(r: dict) -> CameraSummary:
    return CameraSummary(
        id=r["id"],
        host_id=r["host_id"],
        source=r["source"],
        kind=r.get("kind"),
        model=r["model"],
        pool=r["pool"],
        status=r["status"],
        notes=r["notes"],
        streams=_parse_streams(r),
        resolution_w=r.get("resolution_w"),
        resolution_h=r.get("resolution_h"),
    )


@router.get("/v1/cameras", response_model=list[CameraSummary])
async def list_cameras(request: Request, _auth: Auth) -> list[CameraSummary]:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM cameras ORDER BY id") as cur:
            rows = await cur.fetchall()
    return [_camera_summary(dict(r)) for r in rows]


@router.get("/v1/cameras/{cam_id}", response_model=CameraSummary)
async def get_camera(request: Request, cam_id: str, _auth: Auth) -> CameraSummary:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM cameras WHERE id = ?", (cam_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return _camera_summary(dict(row))


# ---------------------------------------------------------------------------
# Camera snapshot
# ---------------------------------------------------------------------------


@router.get("/v1/cameras/{cam_id}/snapshot")
async def camera_snapshot(request: Request, cam_id: str, _auth: Auth) -> Response:
    """Return a single JPEG frame from the camera's primary source URL."""
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT source, streams_json FROM cameras WHERE id = ?", (cam_id,)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    streams = _parse_streams(dict(row))
    # Use first snapshot-type stream, then any stream
    url = None
    for s in streams:
        if s.get("type") in ("snapshot", "mjpeg", "rtsp"):
            url = s.get("url")
            break
    if url is None and row["source"]:
        url = row["source"]
    if not url:
        raise HTTPException(status_code=503, detail="Camera has no stream URL configured")

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return Response(content=r.content, media_type="image/jpeg")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to fetch frame: {exc}") from exc


# ---------------------------------------------------------------------------
# Device camera assignment + ROI
# ---------------------------------------------------------------------------


@router.get("/v1/devices/{device_id}/camera")
async def get_device_camera(request: Request, device_id: str, _auth: Auth) -> dict[str, Any]:
    """Return the camera assignment and current ROI for a device."""
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT camera_id, qr_identifier FROM devices WHERE id = ?", (device_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found")

        roi = None
        async with db.execute("SELECT * FROM camera_rois WHERE device_id = ?", (device_id,)) as cur:
            roi_row = await cur.fetchone()
        if roi_row:
            roi = dict(roi_row)

    return {
        "device_id": device_id,
        "camera_id": row["camera_id"],
        "qr_identifier": row["qr_identifier"],
        "roi": roi,
    }


@router.put("/v1/devices/{device_id}/camera/roi", response_model=ROIResponse)
async def set_device_roi(
    request: Request, device_id: str, body: ROISetRequest, _auth: Auth
) -> ROIResponse:
    """Set a manual ROI for a device."""
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT d.camera_id, c.source, c.streams_json "
            "FROM devices d LEFT JOIN cameras c ON c.id = d.camera_id "
            "WHERE d.id = ?",
            (device_id,),
        ) as cur:
            dev = await cur.fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail="Device not found")
        if not dev["camera_id"]:
            raise HTTPException(status_code=422, detail="Device has no camera assigned")

        frame_w, frame_h = body.frame_width, body.frame_height
        if not (frame_w and frame_h):
            # Detect the reference frame size from a live snapshot so the ROI is
            # interpretable against any capture resolution later.
            streams = _parse_streams({"source": dev["source"], "streams_json": dev["streams_json"]})
            url = next(
                (s["url"] for s in streams if s.get("type") in ("snapshot", "mjpeg")), dev["source"]
            )
            if url:
                try:
                    dims = roi_snapshot.decode_dims(await _fetch_frame(url))
                except HTTPException:
                    dims = None
                if dims:
                    frame_w, frame_h = dims

        ts = now_iso()
        await db.execute(
            """INSERT INTO camera_rois
                   (device_id, camera_id, x, y, w, h, roi_frame_width, roi_frame_height,
                    source, confidence, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', NULL, ?)
               ON CONFLICT(device_id) DO UPDATE SET
                 x=excluded.x, y=excluded.y, w=excluded.w, h=excluded.h,
                 roi_frame_width=excluded.roi_frame_width,
                 roi_frame_height=excluded.roi_frame_height,
                 source='manual', confidence=NULL, updated_at=excluded.updated_at""",
            (device_id, dev["camera_id"], body.x, body.y, body.w, body.h, frame_w, frame_h, ts),
        )
        await db.commit()

    return ROIResponse(
        device_id=device_id,
        camera_id=dev["camera_id"],
        x=body.x,
        y=body.y,
        w=body.w,
        h=body.h,
        roi_frame_width=frame_w,
        roi_frame_height=frame_h,
        source="manual",
        confidence=None,
        updated_at=ts,
    )


@router.delete("/v1/devices/{device_id}/camera/roi")
async def delete_device_roi(request: Request, device_id: str, _auth: Auth) -> dict[str, str]:
    """Clear manual ROI override for a device."""
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await db.execute("DELETE FROM camera_rois WHERE device_id = ?", (device_id,))
        await db.commit()
    return {"status": "cleared", "device_id": device_id}


@router.post("/v1/devices/{device_id}/camera/focus")
async def focus_device_camera(
    request: Request, device_id: str, body: FocusRequest, _auth: Auth
) -> dict[str, Any]:
    """Focus this device's camera on its ROI now (region/point/auto/manual).

    Forces the focus decision onto this device, bypassing the shared-camera
    precedence chain. ``region``/``point`` need a calibrated ROI; without one the
    camera falls back to full-frame auto (reported in ``directive``).
    """
    if body.mode not in ("region", "point", "auto", "manual"):
        raise HTTPException(status_code=422, detail=f"invalid mode: {body.mode!r}")
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT camera_id FROM devices WHERE id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found")
        if not row["camera_id"]:
            raise HTTPException(status_code=422, detail="Device has no camera assigned")
        if (
            body.mode in ("region", "point")
            and (await orchestrator._device_roi(db, device_id)) is None
        ):
            raise HTTPException(
                status_code=422,
                detail="Device has no ROI; calibrate one or use mode=auto/manual",
            )
        result = await orchestrator.recompute_for_device(
            db, device_id, prefer=True, prefer_mode=body.mode, position=body.position
        )
    if result is None:
        raise HTTPException(status_code=422, detail="Device has no camera assigned")
    return result


@router.get("/v1/devices/{device_id}/camera/focus")
async def get_device_focus(request: Request, device_id: str, _auth: Auth) -> dict[str, Any]:
    """Report the resolved focus driver + the directive the camera would apply.

    Shows what automatic orchestration would push right now (the shared-camera
    precedence chain) plus the camera's live lens state from ``/health``.
    """
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT d.camera_id, c.source, c.kind "
            "FROM devices d LEFT JOIN cameras c ON c.id = d.camera_id "
            "WHERE d.id = ?",
            (device_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found")
        if not row["camera_id"]:
            raise HTTPException(status_code=422, detail="Device has no camera assigned")
        directive = await orchestrator.compute_focus_directive(db, row["camera_id"])

    kind = resolve_camera_kind(dict(row))
    driver = get_driver(kind)
    base = orchestrator.camera_base_url(row["source"]) if row["source"] else None

    lens: dict[str, Any] | None = None
    if base:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{base}/health")
                r.raise_for_status()
                lens = r.json().get("lens")
        except Exception:  # noqa: BLE001 — health is best-effort
            lens = None

    return {
        "device_id": device_id,
        "camera_id": row["camera_id"],
        "kind": kind,
        "supports_window": driver.supports_window,
        # manual_focus is in the camera's native units — surfaced so callers/UI
        # can label inputs correctly (it does not translate across camera kinds).
        "focus_units": driver.focus_units,
        "focus_range": [driver.focus_min, driver.focus_max],
        "directive": directive,
        "lens": lens,
    }


@router.get("/v1/devices/{device_id}/camera/snapshot")
async def device_camera_snapshot(
    request: Request,
    device_id: str,
    _auth: Auth,
    res: str = Query("warm", pattern="^(warm|full)$"),
    pad: float = Query(0.0, ge=0.0, le=2.0),
) -> Response:
    """Return the current frame cropped to the device's ROI.

    ``res=warm`` (default) crops the fast warm-pipeline frame (back-compatible).
    ``res=full`` crops the sensor-native still and scales the ROI from the frame
    size it was calibrated against (``roi_frame_*``) — a much sharper crop.
    ``pad`` grows the ROI box by that fraction on each side.
    """
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT d.camera_id, r.x, r.y, r.w, r.h, "
            "r.roi_frame_width, r.roi_frame_height, c.source, c.streams_json "
            "FROM devices d "
            "LEFT JOIN camera_rois r ON r.device_id = d.id "
            "LEFT JOIN cameras c ON c.id = d.camera_id "
            "WHERE d.id = ?",
            (device_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found")
        if not row["camera_id"]:
            raise HTTPException(status_code=422, detail="Device has no camera assigned")

        streams = _parse_streams({"source": row["source"], "streams_json": row["streams_json"]})
        warm_url = next(
            (s["url"] for s in streams if s.get("type") in ("snapshot", "mjpeg")), row["source"]
        )
        has_roi = row["x"] is not None
        ref_w, ref_h = row["roi_frame_width"], row["roi_frame_height"]

        if res == "full":
            url = roi_snapshot.full_res_url(row["source"], streams) or warm_url
        else:
            url = warm_url
        if not url:
            raise HTTPException(status_code=503, detail="Camera has no stream URL")

        # Legacy ROIs predate roi_frame_*; for a full-res crop we must know the
        # frame they were drawn on. Learn it from the warm frame once and backfill.
        if res == "full" and has_roi and not (ref_w and ref_h):
            dims = roi_snapshot.decode_dims(await _fetch_frame(warm_url)) if warm_url else None
            if dims:
                ref_w, ref_h = dims
                await db.execute(
                    "UPDATE camera_rois SET roi_frame_width=?, roi_frame_height=? WHERE device_id=?",  # noqa: E501
                    (ref_w, ref_h, device_id),
                )
                await db.commit()

    frame_bytes = await _fetch_frame(url)

    if has_roi:
        # For a warm crop the fetched frame IS the reference frame (scale 1.0).
        crop = roi_snapshot.crop_to_jpeg(
            frame_bytes,
            x=int(row["x"]),
            y=int(row["y"]),
            w=int(row["w"]),
            h=int(row["h"]),
            ref_w=ref_w if res == "full" else None,
            ref_h=ref_h if res == "full" else None,
            pad=pad,
        )
        if crop is not None:
            return Response(content=crop, media_type="image/jpeg")

    return Response(content=frame_bytes, media_type="image/jpeg")


@router.post("/v1/devices/{device_id}/camera/calibrate")
async def calibrate_device_roi(request: Request, device_id: str, _auth: Auth) -> dict[str, Any]:
    """Trigger QR auto-detection on a live frame; return proposed ROI (does not save)."""
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT d.camera_id, d.qr_identifier, c.source, c.streams_json "
            "FROM devices d LEFT JOIN cameras c ON c.id = d.camera_id "
            "WHERE d.id = ?",
            (device_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")
    if not row["camera_id"]:
        raise HTTPException(status_code=422, detail="Device has no camera assigned")
    if not row["qr_identifier"]:
        raise HTTPException(status_code=422, detail="Device has no qr_identifier set")

    streams = _parse_streams({"source": row["source"], "streams_json": row["streams_json"]})
    url = next((s["url"] for s in streams if s.get("type") in ("snapshot", "mjpeg")), row["source"])
    if not url:
        raise HTTPException(status_code=503, detail="Camera has no stream URL")

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            frame_bytes = r.content
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to fetch frame: {exc}") from exc

    try:
        import cv2
        import numpy as np

        from hil_controller.adapters.camera.qr_locator import (
            scan_qr_codes,
            segment_board_roi,
        )

        arr = np.frombuffer(frame_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return {"found": False, "reason": "frame_decode_failed"}

        qrs = scan_qr_codes(img)
        qr_id = row["qr_identifier"]
        if qr_id not in qrs:
            return {"found": False, "reason": "no_qr_detected", "qr_identifier": qr_id}

        bbox = qrs[qr_id]
        board = segment_board_roi(img, bbox)
        ih, iw = img.shape[:2]
        return {
            "found": True,
            "qr_data": qr_id,
            "roi": {"x": board.x, "y": board.y, "w": board.w, "h": board.h},
            "frame_width": int(iw),
            "frame_height": int(ih),
            "confidence": 0.9,
        }
    except ImportError:
        return {"found": False, "reason": "cv2_not_available"}


@router.post("/v1/devices/{device_id}/camera/calibrate/save")
async def save_calibration(request: Request, device_id: str, _auth: Auth) -> ROIResponse:
    """Run QR calibration and save the result to camera_rois."""
    result = await calibrate_device_roi(request, device_id, _auth)
    if not result.get("found"):
        raise HTTPException(status_code=422, detail=result.get("reason", "calibration_failed"))

    roi = result["roi"]
    frame_w = result.get("frame_width")
    frame_h = result.get("frame_height")
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT camera_id FROM devices WHERE id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")

    ts = now_iso()
    async with get_db(db_path) as db:
        await db.execute(
            """INSERT INTO camera_rois
                   (device_id, camera_id, x, y, w, h, roi_frame_width, roi_frame_height,
                    source, confidence, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'qr_auto', ?, ?)
               ON CONFLICT(device_id) DO UPDATE SET
                 x=excluded.x, y=excluded.y, w=excluded.w, h=excluded.h,
                 roi_frame_width=excluded.roi_frame_width,
                 roi_frame_height=excluded.roi_frame_height,
                 source='qr_auto', confidence=excluded.confidence, updated_at=excluded.updated_at""",  # noqa: E501
            (
                device_id,
                row["camera_id"],
                roi["x"],
                roi["y"],
                roi["w"],
                roi["h"],
                frame_w,
                frame_h,
                result.get("confidence", 0.9),
                ts,
            ),
        )
        await db.commit()

    return ROIResponse(
        device_id=device_id,
        camera_id=row["camera_id"],
        x=roi["x"],
        y=roi["y"],
        w=roi["w"],
        h=roi["h"],
        roi_frame_width=frame_w,
        roi_frame_height=frame_h,
        source="qr_auto",
        confidence=result.get("confidence"),
        updated_at=ts,
    )
