"""HilCapture: marker parsing + per-stage controller-side webcam proof, and the
LocalTransport on_line streaming hook it rides on."""

import asyncio

import pytest

from hil_controller.adapters.camera import hil_capture
from hil_controller.adapters.camera.hil_capture import HilCapture
from hil_controller.hosts.local import LocalTransport


def _cfg(tmp_path, **kw):
    base = {"webcam_url": "http://cam.local/shot.jpg", "snapshot_dir": str(tmp_path)}
    base.update(kw)
    return base


def test_feed_parses_markers_and_ignores_noise(tmp_path):
    cap = HilCapture(_cfg(tmp_path))
    cap.feed("WS_HIL_CAPTURE seq=1 label=splash kind=splash window_s=14.0")
    cap.feed("some other test output line")
    cap.feed("WS_HIL_CAPTURE seq=2 label=after_write_1 kind=snap window_s=0.0")
    first = cap._q.get_nowait()
    second = cap._q.get_nowait()
    assert first == {"seq": 1, "label": "splash", "kind": "splash", "window_s": 14.0}
    assert second["label"] == "after_write_1" and second["kind"] == "snap"
    assert cap._q.empty()  # the noise line enqueued nothing


@pytest.mark.asyncio
async def test_consume_snaps_each_marker(tmp_path, monkeypatch):
    async def _fake_fetch(url, timeout=10.0):
        return b"\xff\xd8\xff-jpeg-bytes"

    monkeypatch.setattr(hil_capture, "_fetch", _fake_fetch)
    cap = HilCapture(_cfg(tmp_path))  # no ROI -> full frame only, no cv2 needed
    cap.feed("WS_HIL_CAPTURE seq=0 label=baseline_pre_add kind=snap window_s=0.0")
    cap.feed("WS_HIL_CAPTURE seq=2 label=after_write_1 kind=snap window_s=0.0")
    cap.close()
    await asyncio.wait_for(cap.consume(), timeout=10)

    names = sorted(p.name for p in tmp_path.glob("*.jpg"))
    assert names == ["00_baseline_pre_add.jpg", "02_after_write_1.jpg"]
    assert (tmp_path / "00_baseline_pre_add.jpg").read_bytes() == b"\xff\xd8\xff-jpeg-bytes"


@pytest.mark.asyncio
async def test_consume_tune_best_effort(tmp_path, monkeypatch):
    calls = []

    async def _fake_fetch(url, timeout=10.0):
        calls.append(url)
        return b"frame"

    monkeypatch.setattr(hil_capture, "_fetch", _fake_fetch)
    cap = HilCapture(_cfg(tmp_path, tune={"iso": "524", "exposure_ns": "7170511"}))
    cap.close()
    await asyncio.wait_for(cap.consume(), timeout=10)
    # tune fired manual_sensor + iso + exposure_ns settings URLs
    assert any("manual_sensor" in c for c in calls)
    assert any("iso" in c for c in calls)


def test_pick_settled_prefers_first_settled_after_baseline(tmp_path):
    np = pytest.importorskip("numpy")
    cap = HilCapture(_cfg(tmp_path))
    blank = np.zeros((40, 40), dtype="uint8")
    splash = np.full((40, 40), 200, dtype="uint8")
    cap._baseline_gray = blank
    # pre-state (≈baseline), then two identical settled splash frames
    samples = [
        (b"f0", blank.copy()),
        (b"f1", splash.copy()),
        (b"f2", splash.copy()),
    ]
    assert cap._pick_settled(samples) == b"f1"  # first changed + settled


def test_pick_settled_none_when_never_changes(tmp_path):
    np = pytest.importorskip("numpy")
    cap = HilCapture(_cfg(tmp_path))
    blank = np.zeros((10, 10), dtype="uint8")
    cap._baseline_gray = blank
    assert cap._pick_settled([(b"f0", blank.copy()), (b"f1", blank.copy())]) is None


@pytest.mark.asyncio
async def test_local_transport_on_line_streams_and_accumulates():
    seen = []
    result = await LocalTransport().exec(
        ["bash", "-c", "echo one; echo two; echo three"],
        on_line=seen.append,
    )
    assert result.exit_status == 0
    assert seen == ["one", "two", "three"]
    assert result.stdout.splitlines() == ["one", "two", "three"]
