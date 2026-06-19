"""VideoRecorder: OpenCV VideoCapture + VideoWriter in a background thread."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

try:
    import cv2

    _CV2 = True
except ImportError:
    _CV2 = False


class VideoRecorder:
    """Record a camera feed to a video file in a background thread."""

    def __init__(
        self,
        output_path: Path,
        device: int | str = 0,
        fps: float = 30.0,
        width: int = 1280,
        height: int = 720,
    ) -> None:
        self.output_path = Path(output_path)
        self.device = device
        self.fps = fps
        self.width = width
        self.height = height
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._error: Exception | None = None

    def start(self) -> None:
        """Begin recording in a background thread; blocks until device is open."""
        if not _CV2:
            raise RuntimeError("cv2 not installed; cannot record video")
        self._stop.clear()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError(f"Camera device {self.device!r} did not open within 10 s")
        if self._error:
            raise self._error

    def stop(self) -> Path:
        """Stop recording and return the output path."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        return self.output_path

    def _record_loop(self) -> None:
        cap = None
        writer = None
        try:
            cap = cv2.VideoCapture(self.device)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open camera device {self.device!r}")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            writer = cv2.VideoWriter(
                str(self.output_path), fourcc, self.fps, (self.width, self.height)
            )
            if not writer.isOpened():
                raise RuntimeError(f"Cannot open VideoWriter for {self.output_path}")
            self._started.set()
            while not self._stop.is_set():
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.01)
                    continue
                h, w = frame.shape[:2]
                if w != self.width or h != self.height:
                    frame = cv2.resize(frame, (self.width, self.height))
                writer.write(frame)
        except Exception as exc:
            self._error = exc
            self._started.set()
        finally:
            if cap is not None:
                cap.release()
            if writer is not None:
                writer.release()
