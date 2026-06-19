"""V4L2/UVC backend via OpenCV.

Drives any /dev/video* device that exposes an RGB-capable capture format.
AF is attempted via the standard UVC controls (focus_auto / focus_absolute)
if the camera reports them; otherwise the lens stays at whatever fixed
position the device defaults to.
"""
from __future__ import annotations

import subprocess

from .base import Backend, BackendUnavailable, FrameConfig

try:
    import cv2  # type: ignore[import-not-found]
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001
    cv2 = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def _v4l2_has_control(device: str, name: str) -> bool:
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-ctrls"],
            capture_output=True,
            timeout=2,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return name in out.stdout


def _v4l2_set(device: str, name: str, value: int) -> None:
    try:
        subprocess.run(
            ["v4l2-ctl", "-d", device, f"--set-ctrl={name}={value}"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


class V4L2Backend(Backend):
    name = "v4l2"

    def __init__(self, cfg: FrameConfig, *, device: str = "/dev/video0"):
        super().__init__(cfg)
        self._device = device
        self._cap = None
        self._af_enabled = False

    def supports_autofocus(self) -> bool:
        return self._af_enabled

    def _open(self) -> None:
        if cv2 is None:
            raise BackendUnavailable(f"cv2 not importable: {_IMPORT_ERROR}")

        # cv2.VideoCapture accepts either an int index or a string path; the
        # latter is more predictable across hosts with multiple video nodes.
        cap = cv2.VideoCapture(self._device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise BackendUnavailable(f"cannot open {self._device} via V4L2")

        # width/height = 0 means "use device default" — leave the cap alone
        # so the driver picks its native mode.
        if self.cfg.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
        if self.cfg.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
        # Reflect the actual negotiated size back into cfg for /health.
        self.cfg.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or self.cfg.width
        self.cfg.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or self.cfg.height
        # Single-frame queue so we always read the newest frame; not all
        # drivers honour this but it's cheap to ask.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        ok, _ = cap.read()
        if not ok:
            cap.release()
            raise BackendUnavailable(
                f"{self._device}: opened but first read() failed (no usable pipeline)"
            )

        if _v4l2_has_control(self._device, "focus_automatic_continuous"):
            _v4l2_set(self._device, "focus_automatic_continuous", 1)
            self._af_enabled = True
        elif _v4l2_has_control(self._device, "focus_auto"):
            _v4l2_set(self._device, "focus_auto", 1)
            self._af_enabled = True

        self._cap = cap

    def _grab_jpeg(self) -> bytes:
        assert self._cap is not None
        # Drain any stale buffered frames so we encode the newest one. cv2
        # gives us no peek API, so the cheapest "skip to latest" is to read
        # twice when the driver lied about BUFFERSIZE=1.
        self._cap.grab()
        ok, frame = self._cap.read()
        if not ok:
            raise RuntimeError("V4L2 read() returned no frame")
        ok, jpeg = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality]
        )
        if not ok:
            raise RuntimeError("cv2.imencode failed")
        return jpeg.tobytes()

    def _close(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            finally:
                self._cap = None


BACKEND_CLASS = V4L2Backend
