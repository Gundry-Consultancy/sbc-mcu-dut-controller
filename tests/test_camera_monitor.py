"""Tests for CameraMonitor and ROI dataclass — uses fake sources, no real camera."""

from __future__ import annotations

import pytest

try:
    import numpy as np

    _NUMPY = True
except ImportError:
    _NUMPY = False

_skip = pytest.mark.skipif(not _NUMPY, reason="numpy not installed")


class _FakeSource:
    """Fake CameraSource returning a sequence of frames."""

    def __init__(self, frames):
        self._it = iter(frames)

    async def read_frame(self):
        return next(self._it, None)

    async def read_frames(self):
        while True:
            f = await self.read_frame()
            if f is None:
                return
            yield f

    async def close(self):
        pass


def test_roi_dataclass_defaults():
    from hil_controller.adapters.camera.monitor import ROI

    roi = ROI(x=1, y=2, w=3, h=4)
    assert roi.source == "manual"
    assert roi.confidence is None
    assert roi.updated_at is not None


def test_roi_all_source_values():
    from hil_controller.adapters.camera.monitor import ROI

    for src in ("manual", "qr_auto", "yellow_box"):
        r = ROI(0, 0, 10, 10, source=src)
        assert r.source == src


def test_monitor_available_devices():
    from hil_controller.adapters.camera.monitor import ROI, CameraMonitor

    rois = {"dev-a": ROI(0, 0, 100, 100), "dev-b": ROI(100, 100, 100, 100)}
    mon = CameraMonitor(source=_FakeSource([]), rois=rois, qr_map={})
    assert set(mon.available_devices()) == {"dev-a", "dev-b"}


def test_monitor_get_roi_returns_roi():
    from hil_controller.adapters.camera.monitor import ROI, CameraMonitor

    roi = ROI(10, 20, 80, 60)
    mon = CameraMonitor(source=_FakeSource([]), rois={"dev": roi}, qr_map={})
    assert mon.get_roi("dev") is roi


def test_monitor_get_roi_missing_returns_none():
    from hil_controller.adapters.camera.monitor import CameraMonitor

    mon = CameraMonitor(source=_FakeSource([]), rois={}, qr_map={})
    assert mon.get_roi("nonexistent") is None


def test_monitor_get_crop_before_update_returns_none():
    from hil_controller.adapters.camera.monitor import ROI, CameraMonitor

    mon = CameraMonitor(source=_FakeSource([]), rois={"dev": ROI(0, 0, 100, 100)}, qr_map={})
    assert mon.get_crop("dev") is None


@_skip
def test_monitor_update_crops_extracts_correct_region():
    from hil_controller.adapters.camera.monitor import ROI, CameraMonitor

    frame = np.zeros((200, 300, 3), dtype=np.uint8)
    frame[50:150, 100:200] = 128  # mark the ROI region

    roi = ROI(x=100, y=50, w=100, h=100)
    mon = CameraMonitor(source=_FakeSource([frame]), rois={"dev": roi}, qr_map={})
    mon._update_crops(frame)

    crop = mon.get_crop("dev")
    assert crop is not None
    assert crop.shape == (100, 100, 3)
    assert crop[0, 0, 0] == 128


@_skip
def test_monitor_update_roi_replaces():
    from hil_controller.adapters.camera.monitor import ROI, CameraMonitor

    old = ROI(0, 0, 50, 50)
    new = ROI(10, 10, 80, 80)
    mon = CameraMonitor(source=_FakeSource([]), rois={"dev": old}, qr_map={})
    mon.update_roi("dev", new)
    assert mon.get_roi("dev") is new


@_skip
def test_monitor_empty_rois():
    from hil_controller.adapters.camera.monitor import CameraMonitor

    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    mon = CameraMonitor(source=_FakeSource([frame]), rois={}, qr_map={})
    mon._update_crops(frame)
    assert mon.available_devices() == []
