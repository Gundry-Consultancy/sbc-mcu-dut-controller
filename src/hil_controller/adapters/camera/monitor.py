"""Generic CameraMonitor: continuously reads frames, maintains per-device ROI crops."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

try:
    import numpy as np

    _NUMPY = True
except ImportError:
    _NUMPY = False

if TYPE_CHECKING:
    import numpy as np


@dataclass
class ROI:
    """Per-device region of interest in camera pixel space."""

    x: int
    y: int
    w: int
    h: int
    source: str = "manual"  # "qr_auto" | "yellow_box" | "manual"
    confidence: float | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class CameraMonitor:
    """Continuously read from a CameraSource and maintain per-device ROI crops.

    Thread-safe: get_crop() and get_roi() may be called from any thread.
    ROIs come from the DB (camera_rois table), not hardcoded.
    """

    def __init__(
        self,
        source: object,  # CameraSource
        rois: dict[str, ROI],  # {device_id: ROI} loaded from DB
        qr_map: dict[str, str],  # {qr_data: device_id}
        capture_interval: float = 1.0,
        archive: bool = False,
        output_dir: Path | None = None,
    ) -> None:
        self._source = source
        self._rois: dict[str, ROI] = dict(rois)
        self._qr_map = qr_map
        self._capture_interval = capture_interval
        self._archive = archive
        self._output_dir = output_dir
        self._crops: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    async def start(self) -> None:
        """Start the background capture loop."""
        self._task = asyncio.get_event_loop().create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            frame = await self._source.read_frame()
            if frame is not None and _NUMPY:
                self._update_crops(frame)
            await asyncio.sleep(self._capture_interval)

    def _update_crops(self, frame: np.ndarray) -> None:
        with self._lock:
            for device_id, roi in self._rois.items():
                crop = frame[roi.y : roi.y + roi.h, roi.x : roi.x + roi.w]
                self._crops[device_id] = crop.copy()

    def get_crop(self, device_id: str) -> np.ndarray | None:
        """Return the most recent cropped frame for a device, or None."""
        with self._lock:
            return self._crops.get(device_id)

    def get_roi(self, device_id: str) -> ROI | None:
        """Return the ROI for a device, or None."""
        with self._lock:
            return self._rois.get(device_id)

    def update_roi(self, device_id: str, roi: ROI) -> None:
        """Replace the ROI for a device (live, without restarting)."""
        with self._lock:
            self._rois[device_id] = roi

    def available_devices(self) -> list[str]:
        """Return the list of device IDs currently being monitored."""
        with self._lock:
            return list(self._rois.keys())
