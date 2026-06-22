"""Frame-relative ROI cropping helpers shared by the /v1 and /ui snapshot routes.

An ROI is stored as pixel coords ``(x, y, w, h)`` in a reference frame of size
``(roi_frame_width, roi_frame_height)`` — the exact image it was drawn on. To crop
it out of a *different* capture (e.g. the sensor-native ``?full=1`` frame, which is
larger than the warm frame the ROI was calibrated against), scale every coord by
``actual_dims / reference_dims``. These functions are pure (no IO) so they're trivial
to unit-test; the HTTP fetch stays in the route handlers.
"""

from __future__ import annotations

from typing import Any, Optional

try:  # cv2/numpy are optional at import time; routes fall back to the full frame.
    import cv2
    import numpy as np

    _CV2 = True
except ImportError:  # pragma: no cover - exercised only where cv2 is absent
    _CV2 = False


def full_res_url(source: str | None, streams: list[dict[str, Any]] | None) -> str | None:
    """Resolve the highest-resolution still URL for a camera.

    Preference order:
      1. an explicit stream of ``type == "snapshot_full"`` (topology/UI override);
      2. auto-derived from a known camera type:
         - Pi camera-server (``…:8080/`` warm endpoint) -> append ``?full=1``
           (sensor-native still, see tools/camera-server);
         - Android IP Webcam (``…/shot.jpg`` live snapshot) -> swap to ``/photo.jpg``
           (full-res still);
      3. otherwise the plain ``source`` (best effort).
    """
    streams = streams or []
    for s in streams:
        if s.get("type") == "snapshot_full" and s.get("url"):
            return s["url"]
    if not source:
        return None
    base = source.split("?", 1)[0]
    if base.endswith("/shot.jpg"):
        return base[: -len("shot.jpg")] + "photo.jpg"
    if source.endswith("/"):
        return source + "?full=1"
    if "?" in source:
        return source + "&full=1"
    return source + "?full=1"


def scale_box(
    x: int,
    y: int,
    w: int,
    h: int,
    ref_w: int | None,
    ref_h: int | None,
    target_w: int,
    target_h: int,
    pad: float = 0.0,
) -> tuple[int, int, int, int]:
    """Scale an ROI from its reference frame to a target frame and clamp to bounds.

    ``ref_w``/``ref_h`` falsy (legacy ROI with no recorded frame size) means the
    coords are already in the target frame's space, so scale is 1.0. ``pad`` grows
    the box by that fraction of its size on each side (e.g. 0.1 = +10%).
    """
    sx = (target_w / ref_w) if ref_w else 1.0
    sy = (target_h / ref_h) if ref_h else 1.0
    fx, fy, fw, fh = x * sx, y * sy, w * sx, h * sy
    if pad:
        fx -= fw * pad
        fy -= fh * pad
        fw += fw * 2 * pad
        fh += fh * 2 * pad
    ix = max(0, min(int(round(fx)), target_w - 1))
    iy = max(0, min(int(round(fy)), target_h - 1))
    iw = max(1, min(int(round(fw)), target_w - ix))
    ih = max(1, min(int(round(fh)), target_h - iy))
    return ix, iy, iw, ih


def normalize_roi(
    x: int, y: int, w: int, h: int, frame_w: int | None, frame_h: int | None
) -> tuple[float, float, float, float] | None:
    """Map a pixel ROI to a normalized ``(nx, ny, nw, nh)`` rect in ``[0, 1]``.

    The result is frame-resolution independent so each camera backend can scale
    it into its own coordinate space (e.g. libcamera ``AfWindows`` in sensor
    pixels). Returns ``None`` when the reference frame size is unknown (legacy
    ROIs predate ``roi_frame_*``) or non-positive — the caller falls back to
    full-frame focus rather than guess a window.
    """
    if not frame_w or not frame_h or frame_w <= 0 or frame_h <= 0:
        return None
    nx = max(0.0, min(x / frame_w, 1.0))
    ny = max(0.0, min(y / frame_h, 1.0))
    nw = max(0.0, min(w / frame_w, 1.0 - nx))
    nh = max(0.0, min(h / frame_h, 1.0 - ny))
    if nw <= 0.0 or nh <= 0.0:
        return None
    return nx, ny, nw, nh


def center_box(
    rect: tuple[float, float, float, float], size: float = 0.2
) -> tuple[float, float, float, float]:
    """Shrink a normalized rect to a small box centred on it (point focus).

    ``size`` is the fraction of the *original* box kept on each axis, clamped so
    the result stays within ``[0, 1]``. Used when the caller wants point-focus
    (a single AF point at the ROI centre) rather than region metering.
    """
    nx, ny, nw, nh = rect
    cx, cy = nx + nw / 2.0, ny + nh / 2.0
    bw, bh = nw * size, nh * size
    bx = max(0.0, min(cx - bw / 2.0, 1.0 - bw))
    by = max(0.0, min(cy - bh / 2.0, 1.0 - bh))
    return bx, by, bw, bh


def decode_dims(frame_bytes: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` of a JPEG, or None if cv2 missing / decode fails."""
    if not _CV2:
        return None
    img = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    return w, h


def crop_to_jpeg(
    frame_bytes: bytes,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    ref_w: int | None = None,
    ref_h: int | None = None,
    pad: float = 0.0,
) -> bytes | None:
    """Decode, scale the ROI from its reference frame, crop, and re-encode as JPEG.

    Returns None when cv2 is unavailable or the frame can't be decoded — callers
    fall back to returning the full frame.
    """
    if not _CV2:
        return None
    img = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    th, tw = img.shape[:2]
    bx, by, bw, bh = scale_box(x, y, w, h, ref_w, ref_h, tw, th, pad)
    crop = img[by : by + bh, bx : bx + bw]
    ok, buf = cv2.imencode(".jpg", crop)
    return buf.tobytes() if ok else None
