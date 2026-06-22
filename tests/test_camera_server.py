"""HTTP-server tests for tools/camera-server.

Uses a FakeBackend so the tests don't need real camera hardware.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

CAMERA_SERVER_DIR = Path(__file__).resolve().parents[1] / "tools" / "camera-server"
sys.path.insert(0, str(CAMERA_SERVER_DIR))

from http.server import ThreadingHTTPServer  # noqa: E402

from backends.base import Backend, FrameConfig  # noqa: E402
from illuminators import NullIlluminator  # noqa: E402
from server import SnapshotHandler  # noqa: E402


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, cfg: FrameConfig, *, jpeg_payload: bytes = b"\xff\xd8\xff\xd9"):
        super().__init__(cfg)
        self._payload = jpeg_payload
        self._counter = 0
        self._lens_mode = "auto"
        self._manual_position: float | None = None
        self._window: tuple | None = None

    def supports_autofocus(self) -> bool:
        return True

    def _open(self) -> None:
        pass

    def _grab_jpeg(self) -> bytes:
        self._counter += 1
        return self._payload + self._counter.to_bytes(2, "big")

    def _close(self) -> None:
        pass

    def capture_full_jpeg(self) -> bytes:
        return self._payload + b"FULL"

    def set_lens(
        self, *, mode: str, position: float | None = None, window: tuple | None = None
    ) -> None:
        if mode == "auto":
            self._lens_mode = "auto"
            self._manual_position = None
            self._window = None
        elif mode == "manual":
            if position is None:
                raise ValueError("manual lens mode requires position")
            self._lens_mode = "manual"
            self._manual_position = float(position)
            self._window = None
        elif mode == "window":
            if window is None:
                raise ValueError("window lens mode requires window")
            self._lens_mode = "window"
            self._manual_position = None
            self._window = tuple(window)
        else:
            raise ValueError(f"unknown lens mode: {mode!r}")

    def get_lens(self) -> dict:
        return {
            "mode": self._lens_mode,
            "position": self._manual_position,
            "window": self._window,
        }


@pytest.fixture
def running_server():
    backend = FakeBackend(FrameConfig(width=320, height=240, fps=30))
    illuminator = NullIlluminator()
    backend.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), SnapshotHandler)
    server.backend = backend
    server.illuminator = illuminator
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, backend, illuminator
    finally:
        server.shutdown()
        server.server_close()
        backend.stop()


def test_health_reports_backend_metadata(running_server):
    port, _backend, _illum = running_server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert body["ok"] is True
    assert body["backend"] == "fake"
    assert body["autofocus"] is True
    assert body["width"] == 320
    assert body["height"] == 240
    assert body["lens"] == {"mode": "auto", "position": None, "window": None}
    assert body["illuminator"]["kind"] == "null"
    assert body["illuminator"]["available"] is False
    assert body["illuminator"]["brightness"] == 0


def test_snapshot_returns_jpeg(running_server):
    port, _backend, _illum = running_server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
        assert r.status == 200
        assert r.headers["Content-Type"] == "image/jpeg"
        body = r.read()
    assert body.startswith(b"\xff\xd8\xff\xd9")  # FakeBackend payload prefix


def test_full_res_snapshot_uses_capture_full_jpeg(running_server):
    port, _backend, _illum = running_server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/?full=1", timeout=2) as r:
        assert r.status == 200
        body = r.read()
    assert body.endswith(b"FULL")


def test_stream_emits_multiple_distinct_frames(running_server):
    port, _backend, _illum = running_server
    parts: list[bytes] = []
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=3) as r:
        assert "multipart/x-mixed-replace" in r.headers["Content-Type"]
        deadline = time.monotonic() + 2.0
        buf = b""
        while time.monotonic() < deadline and len(parts) < 3:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += chunk
            # crude split: each frame ends with the JPEG EOI \xff\xd9 + \r\n
            while b"\xff\xd9" in buf:
                idx = buf.index(b"\xff\xd9") + 2
                parts.append(buf[:idx])
                buf = buf[idx:]
    assert len(parts) >= 2, "stream should emit at least two frames in 2s"
    # Successive frames should differ (FakeBackend counter advances).
    assert parts[0] != parts[1]


def test_post_lens_manual_then_auto(running_server):
    port, backend, _illum = running_server
    # Manual with position
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/lens",
        data=json.dumps({"mode": "manual", "position": 12.5}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        body = json.loads(r.read())
    assert body["lens"]["mode"] == "manual"
    assert body["lens"]["position"] == 12.5

    # Back to auto
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/lens",
        data=json.dumps({"mode": "auto"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        body = json.loads(r.read())
    assert body["lens"]["mode"] == "auto"


def test_post_lens_manual_without_position_returns_400(running_server):
    port, _backend, _illum = running_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/lens",
        data=json.dumps({"mode": "manual"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    else:
        pytest.fail("expected 400")


def test_post_illuminator_sets_brightness(running_server):
    port, _backend, illum = running_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/illuminator",
        data=json.dumps({"brightness": 192}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        body = json.loads(r.read())
    assert body["illuminator"]["brightness"] == 192
    assert illum.get_brightness() == 192


def test_post_illuminator_clamps_out_of_range(running_server):
    port, _backend, illum = running_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/illuminator",
        data=json.dumps({"brightness": 999}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        body = json.loads(r.read())
    assert body["illuminator"]["brightness"] == 255
    assert illum.get_brightness() == 255


def test_post_lens_window_sets_af_window(running_server):
    port, backend, _illum = running_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/lens",
        data=json.dumps(
            {"mode": "window", "window": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        body = json.loads(r.read())
    assert body["lens"]["mode"] == "window"
    assert body["lens"]["window"] == [0.25, 0.25, 0.5, 0.5]
    assert backend._window == (0.25, 0.25, 0.5, 0.5)


def test_post_lens_window_without_window_returns_400(running_server):
    port, _backend, _illum = running_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/lens",
        data=json.dumps({"mode": "window"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    else:
        pytest.fail("expected 400")


class _FakeCam:
    """Just enough of a Picamera2 handle to test AF-window coordinate mapping."""

    def __init__(self, *, props=None, controls=None, sensor=(4656, 3496)):
        self.camera_properties = props if props is not None else {}
        self.camera_controls = controls if controls is not None else {}
        self.sensor_resolution = sensor


def test_picamera2_af_window_rect_uses_scaler_crop_maximum():
    from backends.picamera2_backend import Picamera2Backend

    backend = Picamera2Backend(FrameConfig(width=0, height=0, fps=5))
    backend._cam = _FakeCam(props={"ScalerCropMaximum": (0, 0, 4000, 3000)})
    # Centre half of the frame -> centred sensor rect.
    assert backend._af_window_rect((0.25, 0.25, 0.5, 0.5)) == (1000, 750, 2000, 1500)


def test_picamera2_af_window_rect_falls_back_to_scaler_crop_then_sensor():
    from backends.picamera2_backend import Picamera2Backend

    backend = Picamera2Backend(FrameConfig(width=0, height=0, fps=5))
    # No ScalerCropMaximum -> use the max of the ScalerCrop control range.
    backend._cam = _FakeCam(
        props={}, controls={"ScalerCrop": ((0, 0, 0, 0), (0, 0, 4656, 3496), (0, 0, 0, 0))}
    )
    assert backend._af_window_rect((0.0, 0.0, 1.0, 1.0)) == (0, 0, 4656, 3496)

    # Nothing populated -> sensor resolution with a zero offset.
    backend._cam = _FakeCam(props={}, controls={}, sensor=(1920, 1080))
    assert backend._af_window_rect((0.5, 0.0, 0.5, 1.0)) == (960, 0, 960, 1080)


def test_unknown_path_returns_404(running_server):
    port, _backend, _illum = running_server
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=2)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        pytest.fail("expected 404")
