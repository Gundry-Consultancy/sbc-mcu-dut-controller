"""QR code detection and board ROI segmentation."""

from __future__ import annotations

import logging
from collections import namedtuple
from typing import Optional

logger = logging.getLogger(__name__)

BoundingBox = namedtuple("BoundingBox", ["x", "y", "w", "h"])

try:
    import cv2
    import numpy as np
    from pyzbar import pyzbar as _pyzbar

    _CV2 = True
except ImportError:
    _CV2 = False


def scan_qr_codes(image: np.ndarray) -> dict[str, BoundingBox]:
    """Find all QR codes in a BGR frame. Returns {qr_data: BoundingBox}."""
    if not _CV2 or image is None:
        return {}
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    decoded = _pyzbar.decode(gray, symbols=[_pyzbar.ZBarSymbol.QRCODE])
    return {
        obj.data.decode("utf-8", errors="replace"): BoundingBox(
            x=obj.rect.left, y=obj.rect.top, w=obj.rect.width, h=obj.rect.height
        )
        for obj in decoded
    }


def _grabcut_board_roi(
    image: np.ndarray,
    qcx: int,
    qcy: int,
    qw: int,
    qh: int,
    seed_factor: float = 8.0,
    iterations: int = 3,
) -> BoundingBox | None:
    """GrabCut segmentation seeded from the QR centre."""
    h, w = image.shape[:2]
    seed = max(qw, qh)
    sw = max(150, int(seed * seed_factor))
    sh = max(150, int(seed * seed_factor))
    rx = max(1, qcx - sw // 2)
    ry = max(1, qcy - sh // 2)
    rw = min(w - rx - 2, sw)
    rh = min(h - ry - 2, sh)
    mask = np.zeros(image.shape[:2], np.uint8)
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(image, mask, (rx, ry, rw, rh), bgd, fgd, iterations, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None
    fg = np.where((mask == 2) | (mask == 0), 0, 1).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k, iterations=4)
    fg = cv2.dilate(fg, k, iterations=2)
    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0
    for cnt in contours:
        if cv2.pointPolygonTest(cnt, (float(qcx), float(qcy)), False) >= 0:
            a = cv2.contourArea(cnt)
            if a > best_area:
                best_area, best = a, cnt
    if best is None:
        return None
    bx, by, bw, bh = cv2.boundingRect(best)
    if bw < qw * 2 or bh < qh * 2:
        return None
    return BoundingBox(bx, by, bw, bh)


def _otsu_board_roi(
    image: np.ndarray,
    qcx: int,
    qcy: int,
    qw: int,
    qh: int,
    pad_factor: float = 5.0,
) -> BoundingBox | None:
    """Otsu threshold on a local crop; fallback when GrabCut fails."""
    h, w = image.shape[:2]
    pad = max(qw, qh) * int(pad_factor)
    cx1, cy1 = max(0, qcx - pad), max(0, qcy - pad)
    cx2, cy2 = min(w, qcx + pad), min(h, qcy + pad)
    crop = image[cy1:cy2, cx1:cx2]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k, iterations=4)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    lqx, lqy = qcx - cx1, qcy - cy1
    best = None
    for cnt in contours:
        if cv2.pointPolygonTest(cnt, (float(lqx), float(lqy)), False) >= 0:
            if best is None or cv2.contourArea(cnt) > cv2.contourArea(best):
                best = cnt
    if best is None:
        return None
    bx, by, bw, bh = cv2.boundingRect(best)
    return BoundingBox(bx + cx1, by + cy1, bw, bh)


def segment_board_roi(image: np.ndarray, qr_bbox: BoundingBox) -> BoundingBox:
    """Segment board ROI from QR position; tries GrabCut → Otsu → padding fallback."""
    h, w = image.shape[:2]
    qcx = qr_bbox.x + qr_bbox.w // 2
    qcy = qr_bbox.y + qr_bbox.h // 2
    qw, qh = qr_bbox.w, qr_bbox.h
    roi = _grabcut_board_roi(image, qcx, qcy, qw, qh)
    if roi:
        logger.debug("GrabCut ROI: %s", roi)
        return roi
    roi = _otsu_board_roi(image, qcx, qcy, qw, qh)
    if roi:
        logger.debug("Otsu ROI: %s", roi)
        return roi
    pad = max(qw, qh) * 4
    bx = max(0, qcx - pad)
    by = max(0, qcy - pad)
    logger.debug("Padding fallback ROI")
    return BoundingBox(bx, by, min(w - bx, pad * 2), min(h - by, pad * 2))


def locate_all_boards(image: np.ndarray) -> dict[str, BoundingBox]:
    """Find all QR codes in image and return segmented board ROI for each."""
    qrs = scan_qr_codes(image)
    if not qrs:
        return {}
    return {data: segment_board_roi(image, bbox) for data, bbox in qrs.items()}


def locate_board_roi(
    video_path: str,
    qr_identifier: str,
    max_frames: int = 30,
) -> BoundingBox | None:
    """Search the first max_frames of a video for a specific QR identifier."""
    if not _CV2:
        return None
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error("Cannot open video: %s", video_path)
        return None
    try:
        for _ in range(max_frames):
            ret, frame = cap.read()
            if not ret:
                break
            qrs = scan_qr_codes(frame)
            if qr_identifier in qrs:
                return segment_board_roi(frame, qrs[qr_identifier])
    finally:
        cap.release()
    logger.warning("QR %s not found in first %d frames", qr_identifier, max_frames)
    return None
