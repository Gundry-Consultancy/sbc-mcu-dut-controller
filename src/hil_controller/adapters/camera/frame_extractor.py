"""Extract visually distinct frames from video."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np

    _CV2 = True
except ImportError:
    _CV2 = False

if TYPE_CHECKING:
    import numpy as np


@dataclass
class Frame:
    """A single distinct frame extracted from a video."""

    timestamp_s: float
    frame_number: int
    image: np.ndarray = field(repr=False)
    change_type: str = "initial"  # "initial" | "display" | "led"
    path: str | None = None


def _classify_change(
    diff_mask: np.ndarray,
    frame_bgr: np.ndarray,
    roi_area: int,
    led_area_max_ratio: float = 0.02,
    brightness_thresh: int = 200,
) -> str:
    """Heuristic: small + bright area → LED toggle; large area → display update."""
    contours, _ = cv2.findContours(diff_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return "display"
    total_changed = sum(cv2.contourArea(c) for c in contours)
    changed_ratio = total_changed / max(roi_area, 1)
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if len(frame_bgr.shape) == 3 else frame_bgr
    bright_in_mask = cv2.bitwise_and(gray, gray, mask=diff_mask)
    bright_px = np.count_nonzero(bright_in_mask > brightness_thresh)
    bright_ratio = bright_px / max(np.count_nonzero(diff_mask), 1)
    if changed_ratio < led_area_max_ratio and bright_ratio > 0.3:
        return "led"
    return "display"


def extract_distinct_frames(
    video_path: str,
    roi: BoundingBox | None = None,
    threshold: float = 0.03,
    output_dir: str | None = None,
) -> list[Frame]:
    """Walk a video and return frames where visible content changed significantly."""
    if not _CV2:
        logger.warning("cv2 not available; skipping frame extraction")
        return []
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open video: %s", video_path)
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    stem = Path(video_path).stem
    save_dir = Path(output_dir) if output_dir else Path("artifacts") / "frames" / stem
    save_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Frame] = []
    prev_gray: np.ndarray | None = None
    frame_idx = 0
    try:
        while True:
            ret, raw = cap.read()
            if not ret:
                break
            region = raw[roi.y : roi.y + roi.h, roi.x : roi.x + roi.w] if roi else raw
            gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
            ts = round(frame_idx / fps, 3)
            if prev_gray is None:
                frames.append(Frame(ts, frame_idx, region.copy(), "initial"))
                prev_gray = gray
                frame_idx += 1
                continue
            diff = cv2.absdiff(gray, prev_gray)
            _, mask = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
            change_ratio = np.count_nonzero(mask) / mask.size
            if change_ratio > threshold:
                ctype = _classify_change(mask, region, mask.size)
                frames.append(Frame(ts, frame_idx, region.copy(), ctype))
                prev_gray = gray
            frame_idx += 1
    finally:
        cap.release()
    for i, f in enumerate(frames):
        p = save_dir / f"frame_{i:04d}_{f.change_type}.jpg"
        cv2.imwrite(str(p), f.image)
        f.path = str(p)
    logger.info("Extracted %d frames from %s", len(frames), video_path)
    return frames
