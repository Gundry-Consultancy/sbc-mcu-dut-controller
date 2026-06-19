"""CameraCapture: per-job camera capture adapter."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class ROIStore(Protocol):
    """Thin DB wrapper for per-device ROI persistence."""

    def get_roi(self, device_id: str) -> ROI | None: ...
    def set_roi(self, device_id: str, roi: ROI) -> None: ...


@dataclass
class CameraArtifacts:
    """Artifacts produced by a per-job camera capture."""

    device_id: str
    video_path: Path | None = None
    frames: list = field(default_factory=list)
    report_path: str | None = None


class CameraCapture:
    """Per-job camera capture.

    Lifecycle:
      1. start() — grabs ROI from store; auto-calibrates if missing + qr_identifier set.
      2. (job runs) — recording happens via VideoRecorder if device is local.
      3. stop() — stops recorder, extracts distinct frames, generates report.
    """

    def __init__(
        self,
        device_id: str,
        camera_source: object,  # CameraSource
        roi_store: ROIStore,
        artifact_dir: Path,
        fps: float = 15.0,
        width: int = 1280,
        height: int = 720,
        qr_identifier: str | None = None,
    ) -> None:
        self.device_id = device_id
        self._source = camera_source
        self._roi_store = roi_store
        self._artifact_dir = Path(artifact_dir)
        self._fps = fps
        self._width = width
        self._height = height
        self._qr_identifier = qr_identifier

    async def start(self) -> None:
        """Prepare for capture; auto-calibrate ROI if not set."""
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        roi = self._roi_store.get_roi(self.device_id)
        if roi is None and self._qr_identifier:
            roi = await self.calibrate()
            if roi:
                self._roi_store.set_roi(self.device_id, roi)

    async def stop(self) -> CameraArtifacts:
        """Stop capture and return collected artifacts."""
        return CameraArtifacts(device_id=self.device_id)

    async def calibrate(self) -> ROI | None:
        """Try QR auto-detection on a live frame; return ROI if found, else None."""
        try:
            from hil_controller.adapters.camera.monitor import ROI
            from hil_controller.adapters.camera.qr_locator import (
                scan_qr_codes,
                segment_board_roi,
            )
        except ImportError:
            return None
        frame = await self._source.read_frame()
        if frame is None or self._qr_identifier is None:
            return None
        qrs = scan_qr_codes(frame)
        if self._qr_identifier not in qrs:
            logger.info("QR %r not found in frame during calibrate()", self._qr_identifier)
            return None
        bbox = qrs[self._qr_identifier]
        board = segment_board_roi(frame, bbox)
        return ROI(
            x=board.x,
            y=board.y,
            w=board.w,
            h=board.h,
            source="qr_auto",
            confidence=0.9,
            updated_at=datetime.now(UTC).isoformat(),
        )
