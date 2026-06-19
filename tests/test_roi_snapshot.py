"""Unit tests for the frame-relative ROI snapshot helpers (pure, no IO)."""

from __future__ import annotations

import pytest

from hil_controller.adapters.camera import roi_snapshot

# ---------------------------------------------------------------------------
# full_res_url
# ---------------------------------------------------------------------------


def test_full_res_url_pi_camera_server():
    # Warm endpoint ending in "/" -> sensor-native still via ?full=1
    assert (
        roi_snapshot.full_res_url("http://192.168.1.234:8080/", [])
        == "http://192.168.1.234:8080/?full=1"
    )


def test_full_res_url_ip_webcam():
    # Android IP Webcam live snapshot -> full-res still
    assert (
        roi_snapshot.full_res_url("http://192.168.1.249:8080/shot.jpg", [])
        == "http://192.168.1.249:8080/photo.jpg"
    )


def test_full_res_url_explicit_stream_override():
    streams = [
        {"url": "http://cam/", "type": "snapshot"},
        {"url": "http://cam/big.jpg", "type": "snapshot_full"},
    ]
    assert roi_snapshot.full_res_url("http://cam/", streams) == "http://cam/big.jpg"


def test_full_res_url_existing_query_appends():
    assert roi_snapshot.full_res_url("http://cam/snap?x=1", []) == "http://cam/snap?x=1&full=1"


def test_full_res_url_none_source():
    assert roi_snapshot.full_res_url(None, []) is None


# ---------------------------------------------------------------------------
# scale_box
# ---------------------------------------------------------------------------


def test_scale_box_doubles_for_full_res():
    # The live eInk ROI: warm 2328x1748 -> sensor 4656x3496 is exactly 2.0x
    box = roi_snapshot.scale_box(1571, 1055, 620, 653, 2328, 1748, 4656, 3496)
    assert box == (3142, 2110, 1240, 1306)


def test_scale_box_ref_none_is_identity():
    assert roi_snapshot.scale_box(10, 20, 30, 40, None, None, 1000, 1000) == (10, 20, 30, 40)


def test_scale_box_clamps_to_bounds():
    # A box scaled past the frame edge is clamped to stay inside.
    box = roi_snapshot.scale_box(900, 900, 200, 200, 1000, 1000, 1000, 1000)
    x, y, w, h = box
    assert x + w <= 1000 and y + h <= 1000


def test_scale_box_pad_grows_box():
    base = roi_snapshot.scale_box(100, 100, 100, 100, 1000, 1000, 1000, 1000)
    padded = roi_snapshot.scale_box(100, 100, 100, 100, 1000, 1000, 1000, 1000, pad=0.1)
    assert padded[2] > base[2] and padded[3] > base[3]


# ---------------------------------------------------------------------------
# crop_to_jpeg (needs cv2)
# ---------------------------------------------------------------------------


def test_crop_to_jpeg_scales_and_crops():
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    # 200x100 frame; ROI defined on a 100x50 reference frame -> 2x scale.
    frame = np.full((100, 200, 3), 255, dtype=np.uint8)
    _, enc = cv2.imencode(".jpg", frame)
    out = roi_snapshot.crop_to_jpeg(enc.tobytes(), x=10, y=10, w=20, h=15, ref_w=100, ref_h=50)
    assert out is not None
    dec = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    h, w = dec.shape[:2]
    assert (w, h) == (40, 30)  # 20x15 scaled by 2.0
