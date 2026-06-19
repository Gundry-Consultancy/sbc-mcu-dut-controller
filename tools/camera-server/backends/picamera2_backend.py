"""Picamera2 backend (libcamera-based; Pi CSI and many libcamera boards).

Continuous AF runs on the camera's own pipeline so snapshots stay sharp
without per-request AF cycles.
"""
from __future__ import annotations

import io

from .base import Backend, BackendUnavailable, FrameConfig

try:
    from picamera2 import Picamera2  # type: ignore[import-not-found]
    from libcamera import controls  # type: ignore[import-not-found]
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001
    Picamera2 = None  # type: ignore[assignment]
    controls = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def _smallest_full_fov_mode(cam, sensor_size: tuple[int, int]) -> tuple[int, int]:
    """Pick the smallest sensor mode whose crop covers the full active area.

    Sensors typically expose multiple modes: smaller ones use a centre
    crop of the pixel array, larger ones use the full area (often via
    binning). We want the smallest *full-area* mode so FoV stays full
    while memory pressure stays manageable on small boards.
    """
    full_w, full_h = sensor_size
    candidates: list[tuple[int, int]] = []
    for mode in getattr(cam, "sensor_modes", []) or []:
        size = mode.get("size")
        crop = mode.get("crop_limits")
        if not size or not crop:
            continue
        _, _, cw, ch = crop
        # Mode is full-FoV when its crop window matches the active area.
        if cw >= full_w and ch >= full_h:
            candidates.append(size)
    if not candidates:
        return sensor_size
    return min(candidates, key=lambda s: s[0] * s[1])


class Picamera2Backend(Backend):
    name = "picamera2"

    def __init__(self, cfg: FrameConfig, *, camera_num: int = 0):
        super().__init__(cfg)
        self._camera_num = camera_num
        self._cam = None
        self._lens_mode = "auto"
        self._manual_position: float | None = None

    def supports_autofocus(self) -> bool:
        return True

    def set_lens(self, *, mode: str, position: float | None = None) -> None:
        if self._cam is None:
            raise RuntimeError("camera not open")
        if mode == "auto":
            self._cam.set_controls(
                {
                    "AfMode": controls.AfModeEnum.Continuous,
                    "AfRange": controls.AfRangeEnum.Full,
                }
            )
            self._lens_mode = "auto"
            self._manual_position = None
        elif mode == "manual":
            if position is None:
                raise ValueError("manual lens mode requires position")
            self._cam.set_controls(
                {"AfMode": controls.AfModeEnum.Manual, "LensPosition": float(position)}
            )
            self._lens_mode = "manual"
            self._manual_position = float(position)
        else:
            raise ValueError(f"unknown lens mode: {mode!r}")

    def get_lens(self) -> dict:
        reported: float | None = None
        if self._cam is not None:
            try:
                md = self._cam.capture_metadata()
                reported = md.get("LensPosition")
            except Exception:
                reported = None
        return {
            "mode": getattr(self, "_lens_mode", "auto"),
            "position": reported,
            "manual_position": getattr(self, "_manual_position", None),
        }

    def _open(self) -> None:
        if Picamera2 is None:
            raise BackendUnavailable(f"picamera2 not importable: {_IMPORT_ERROR}")
        try:
            cam = Picamera2(camera_num=self._camera_num)
            # Pin the raw stream to the sensor's native resolution so
            # libcamera selects a full-FoV sensor mode regardless of the
            # main stream size. Smaller raw modes on the IMX519 are
            # cropped centre reads — at 1280x720 you only see the middle
            # ~55% x 41% of the active area.
            sensor_size = cam.sensor_resolution
            full_fov_raw = _smallest_full_fov_mode(cam, sensor_size)
            # Resolve "native" (0) to the full-FoV raw mode picked above —
            # not the sensor's max resolution, because at 4656x3496 a
            # continuous video pipeline blows the Pi Zero 2W's CMA budget
            # (RGB main + raw stream + ISP buffers ~ 150-200MB).
            main_w = self.cfg.width or full_fov_raw[0]
            main_h = self.cfg.height or full_fov_raw[1]
            # video_configuration keeps the ISP+AF loop running at a stable
            # framerate, which is what continuous AF needs to converge. The
            # still_configuration runs the pipeline only during capture and
            # leaves AF starved.
            config = cam.create_video_configuration(
                main={"size": (main_w, main_h), "format": "RGB888"},
                raw={"size": full_fov_raw},
            )
            cam.configure(config)
            # Remember the resolved size so /health reports the actual stream.
            self.cfg.width = main_w
            self.cfg.height = main_h

            # Set AF controls before start() so they're active from frame 0.
            # Best-effort: sensors without an AF motor (CM2, CM HQ) lack the
            # AfMode control and would raise here.
            if "AfMode" in cam.camera_controls:
                cam.set_controls(
                    {
                        "AfMode": controls.AfModeEnum.Continuous,
                        "AfRange": controls.AfRangeEnum.Full,
                        "AfSpeed": controls.AfSpeedEnum.Fast,
                    }
                )

            cam.start()
            self._cam = cam
        except BackendUnavailable:
            raise
        except Exception as exc:
            raise BackendUnavailable(f"picamera2 open failed: {exc}") from exc

    def capture_full_jpeg(self) -> bytes:
        """Reconfigure to sensor-native still mode, capture, restore video.

        Costs ~1-2s per call (two reconfigures); guarded by ``self._lock``
        so it won't race the grabber thread.
        """
        if self._cam is None:
            raise RuntimeError("camera not open")
        sensor_size = self._cam.sensor_resolution
        # Save the current video config so we can return to it.
        with self._lock:
            self._cam.stop()
            try:
                still_cfg = self._cam.create_still_configuration(
                    main={"size": sensor_size, "format": "RGB888"},
                    raw={"size": sensor_size},
                )
                self._cam.configure(still_cfg)
                self._cam.start()
                buf = io.BytesIO()
                self._cam.options["quality"] = self.cfg.jpeg_quality
                self._cam.capture_file(buf, format="jpeg")
                jpeg = buf.getvalue()
            finally:
                # Always restore the video pipeline, even on failure, so
                # subsequent /  requests don't hang on a stopped camera.
                self._cam.stop()
                video_cfg = self._cam.create_video_configuration(
                    main={
                        "size": (self.cfg.width, self.cfg.height),
                        "format": "RGB888",
                    },
                    raw={"size": _smallest_full_fov_mode(self._cam, sensor_size)},
                )
                self._cam.configure(video_cfg)
                # Re-apply AF / lens controls before start so they're live
                # from frame 0.
                if "AfMode" in self._cam.camera_controls:
                    if self._lens_mode == "manual" and self._manual_position is not None:
                        self._cam.set_controls(
                            {
                                "AfMode": controls.AfModeEnum.Manual,
                                "LensPosition": self._manual_position,
                            }
                        )
                    else:
                        self._cam.set_controls(
                            {
                                "AfMode": controls.AfModeEnum.Continuous,
                                "AfRange": controls.AfRangeEnum.Full,
                                "AfSpeed": controls.AfSpeedEnum.Fast,
                            }
                        )
                self._cam.start()
        return jpeg

    def _grab_jpeg(self) -> bytes:
        assert self._cam is not None
        buf = io.BytesIO()
        # capture_file with format="jpeg" uses the hardware ISP path when
        # available and respects JPEG quality via the global config.
        self._cam.options["quality"] = self.cfg.jpeg_quality
        self._cam.capture_file(buf, format="jpeg")
        return buf.getvalue()

    def _close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            finally:
                self._cam = None


BACKEND_CLASS = Picamera2Backend
