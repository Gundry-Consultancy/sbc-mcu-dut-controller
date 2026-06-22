"""Unit + integration tests for hil_controller.adapters.camera.orchestrator."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import aiosqlite
import pytest

from hil_controller.adapters.camera.focus_drivers import (
    IpWebcamDriver,
    PiCameraServerDriver,
    UnknownDriver,
    get_driver,
    resolve_camera_kind,
)
from hil_controller.adapters.camera.orchestrator import (
    _job_wants_visual,
    camera_base_url,
    compute_brightness_compromise,
    compute_focus_compromise,
    compute_focus_directive,
    compute_focus_mean,
    recompute_for_camera,
)
from hil_controller.db.connection import init_db


def test_focus_compromise_midpoint():
    assert compute_focus_compromise([10.0, 18.0, 26.0]) == 18.0
    assert compute_focus_compromise([5.0, 25.0]) == 15.0


def test_focus_compromise_ignores_nulls():
    assert compute_focus_compromise([None, 10.0, None, 20.0]) == 15.0


def test_focus_compromise_all_null_returns_none():
    assert compute_focus_compromise([None, None]) is None
    assert compute_focus_compromise([]) is None


def test_brightness_compromise_takes_max():
    assert compute_brightness_compromise([50, 200, 128]) == 200
    assert compute_brightness_compromise([None, 100, None]) == 100
    assert compute_brightness_compromise([None]) is None


def test_camera_base_url_strips_path():
    assert camera_base_url("http://192.168.1.234:8080/") == "http://192.168.1.234:8080"
    assert camera_base_url("http://10.0.0.5:8080/shot.jpg") == "http://10.0.0.5:8080"
    assert camera_base_url("https://cam.local/snapshot?token=x") == "https://cam.local"


def test_camera_base_url_rejects_non_http():
    assert camera_base_url("rtsp://10.0.0.5/stream") is None
    assert camera_base_url("/dev/video0") is None
    assert camera_base_url("") is None


# ---- HTTP integration ----------------------------------------------------


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records every POST body so tests can assert what was pushed."""

    posts: list = []  # class-level so tests can read

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw or b"{}")
        _CaptureHandler.posts.append({"path": self.path, "body": body})
        resp = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *_a, **_kw) -> None:
        pass


@pytest.fixture
def fake_camera_server():
    _CaptureHandler.posts = []
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, _CaptureHandler.posts
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_recompute_pushes_compromise_to_camera_server(tmp_path: Path, fake_camera_server):
    port, posts = fake_camera_server
    db_path = str(tmp_path / "orch.db")
    await init_db(db_path)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT INTO cameras (id, host_id, source, model, pool, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "cam1",
                "host1",
                f"http://127.0.0.1:{port}/",
                "fake",
                "public",
                "available",
            ),
        )
        # Two active devices on cam1 with different manual focus + brightness.
        for dev_id, focus, bright in [("d1", 10.0, 100), ("d2", 20.0, 200)]:
            await db.execute(
                """INSERT INTO devices
                   (id, host_id, kind, model, capabilities_json, pool, status,
                    camera_id, manual_focus, illuminator_brightness)
                   VALUES (?, ?, 'microcontroller', '', '[]', 'public', 'available',
                           ?, ?, ?)""",
                (dev_id, "host1", "cam1", focus, bright),
            )
            await db.execute(
                "INSERT INTO jobs (id, request_json, secrets_profile, exclusive_host, "
                "state, created_at, assigned_device) "
                "VALUES (?, '{}', '', 0, 'running', '2026-01-01', ?)",
                (f"job-{dev_id}", dev_id),
            )
        await db.commit()

        result = await recompute_for_camera(db, "cam1")

    # No visual job → manual fallback: mean(10, 20) = 15.0
    assert result["kind"] == "pi-camera-server"
    assert result["directive"]["mode"] == "manual"
    assert result["directive"]["position"] == 15.0
    # max(100, 200) → 200
    assert result["brightness"] == 200
    assert result["device_count"] == 2

    paths = {p["path"] for p in posts}
    assert "/lens" in paths
    assert "/illuminator" in paths
    lens_body = next(p["body"] for p in posts if p["path"] == "/lens")
    assert lens_body == {"mode": "manual", "position": 15.0}
    illum_body = next(p["body"] for p in posts if p["path"] == "/illuminator")
    assert illum_body == {"brightness": 200}


@pytest.mark.asyncio
async def test_recompute_with_no_active_devices_sends_auto_and_off(
    tmp_path: Path, fake_camera_server
):
    port, posts = fake_camera_server
    db_path = str(tmp_path / "orch_empty.db")
    await init_db(db_path)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT INTO cameras (id, host_id, source, model, pool, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "cam2",
                "host1",
                f"http://127.0.0.1:{port}/",
                "fake",
                "public",
                "available",
            ),
        )
        await db.commit()

        result = await recompute_for_camera(db, "cam2")

    assert result["directive"]["mode"] == "auto"
    assert result["brightness"] is None
    lens_body = next(p["body"] for p in posts if p["path"] == "/lens")
    assert lens_body == {"mode": "auto"}
    illum_body = next(p["body"] for p in posts if p["path"] == "/illuminator")
    assert illum_body == {"brightness": 0}


# ---- mean / visual-job helpers -------------------------------------------


def test_focus_mean():
    assert compute_focus_mean([10.0, 20.0, 60.0]) == 30.0
    assert compute_focus_mean([None, 5.0]) == 5.0
    assert compute_focus_mean([None, None]) is None


def test_job_wants_visual():
    assert _job_wants_visual('{"params": {"collect_artifacts": ["/tmp/out/*.jpg"]}}')
    assert _job_wants_visual('{"params": {"collect_artifacts": ["a.PNG", "b.log"]}}')
    assert not _job_wants_visual('{"params": {"collect_artifacts": ["x.log"]}}')
    assert not _job_wants_visual('{"params": {}}')
    assert not _job_wants_visual("{}")
    assert not _job_wants_visual(None)
    assert not _job_wants_visual("not json")


# ---- kind resolution ------------------------------------------------------


def test_resolve_camera_kind_explicit_column_wins():
    assert resolve_camera_kind({"kind": "ip-webcam", "source": "http://x:8080/"}) == "ip-webcam"


def test_resolve_camera_kind_url_heuristic():
    assert (
        resolve_camera_kind({"kind": None, "source": "http://1.2.3.4:8080/shot.jpg"}) == "ip-webcam"
    )
    assert resolve_camera_kind({"kind": None, "source": "http://1.2.3.4/photo.jpg"}) == "ip-webcam"
    assert (
        resolve_camera_kind({"kind": None, "source": "http://1.2.3.4:8080/"}) == "pi-camera-server"
    )
    assert resolve_camera_kind({"kind": None, "source": "/dev/video0"}) == "unknown"
    assert resolve_camera_kind({"kind": "", "source": ""}) == "unknown"


def test_get_driver_fallback_is_noop():
    assert isinstance(get_driver("pi-camera-server"), PiCameraServerDriver)
    assert isinstance(get_driver("ip-webcam"), IpWebcamDriver)
    assert isinstance(get_driver("nonsense"), UnknownDriver)


def test_driver_focus_units_and_clamp():
    # manual_focus is in each driver's native units, with a declared range.
    pi = PiCameraServerDriver()
    assert pi.focus_units == "dioptre"
    assert pi.clamp_focus(-5.0) == 0.0  # min clamp
    assert pi.clamp_focus(50.0) == 50.0  # no max → passthrough (sensor clamps)

    ip = IpWebcamDriver()
    assert ip.focus_units == "distance"
    assert (ip.focus_min, ip.focus_max) == (0.0, 10.0)
    assert ip.clamp_focus(99.0) == 10.0
    assert ip.clamp_focus(-1.0) == 0.0


# ---- driver dispatch (fake httpx client) ----------------------------------


class _FakeResponse:
    def raise_for_status(self) -> None:
        pass


class _FakeClient:
    """Records the calls each driver makes without real HTTP."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post(self, url, json=None):  # noqa: A002 — mirror httpx signature
        self.calls.append({"method": "POST", "url": url, "json": json})
        return _FakeResponse()

    async def get(self, url):
        self.calls.append({"method": "GET", "url": url})
        return _FakeResponse()


@pytest.mark.asyncio
async def test_pi_driver_window_posts_normalized_rect():
    client = _FakeClient()
    directive = {"mode": "window", "window": (0.1, 0.2, 0.3, 0.4), "position": None}
    await PiCameraServerDriver().apply(client, "http://cam:8080", directive)
    assert client.calls == [
        {
            "method": "POST",
            "url": "http://cam:8080/lens",
            "json": {"mode": "window", "window": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}},
        }
    ]


@pytest.mark.asyncio
async def test_pi_driver_manual_and_auto():
    client = _FakeClient()
    await PiCameraServerDriver().apply(
        client, "http://cam:8080", {"mode": "manual", "position": 7.5, "window": None}
    )
    await PiCameraServerDriver().apply(
        client, "http://cam:8080", {"mode": "auto", "position": None, "window": None}
    )
    assert client.calls[0]["json"] == {"mode": "manual", "position": 7.5}
    assert client.calls[1]["json"] == {"mode": "auto"}


@pytest.mark.asyncio
async def test_ipwebcam_window_degrades_to_full_frame_af():
    client = _FakeClient()
    result = await IpWebcamDriver().apply(
        client, "http://phone:8080", {"mode": "window", "window": (0.1, 0.1, 0.5, 0.5)}
    )
    urls = [c["url"] for c in client.calls]
    assert urls == [
        "http://phone:8080/settings/focusmode?set=continuous-picture",
        "http://phone:8080/focus",
    ]
    assert result["windowed"] is False


@pytest.mark.asyncio
async def test_ipwebcam_manual_sets_focus_distance_clamped():
    client = _FakeClient()
    await IpWebcamDriver().apply(
        client, "http://phone:8080", {"mode": "manual", "position": 99.0, "window": None}
    )
    urls = [c["url"] for c in client.calls]
    assert urls == [
        "http://phone:8080/settings/focusmode?set=off",
        "http://phone:8080/settings/focus_distance?set=10.0",  # clamped to max
    ]


@pytest.mark.asyncio
async def test_ipwebcam_illuminator_maps_to_torch():
    client = _FakeClient()
    await IpWebcamDriver().apply_illuminator(client, "http://phone:8080", 128)
    await IpWebcamDriver().apply_illuminator(client, "http://phone:8080", 0)
    assert [c["url"] for c in client.calls] == [
        "http://phone:8080/enabletorch",
        "http://phone:8080/disabletorch",
    ]


# ---- directive precedence (DB) -------------------------------------------


async def _seed_device_with_roi_and_job(
    db, *, dev_id, camera_id, created_at, request_json, focus=None, roi=True
):
    await db.execute(
        """INSERT INTO devices
           (id, host_id, kind, model, capabilities_json, pool, status,
            camera_id, manual_focus)
           VALUES (?, 'h', 'microcontroller', '', '[]', 'public', 'available', ?, ?)""",
        (dev_id, camera_id, focus),
    )
    await db.execute(
        "INSERT INTO jobs (id, request_json, secrets_profile, exclusive_host, "
        "state, created_at, assigned_device) VALUES (?, ?, '', 0, 'running', ?, ?)",
        (f"job-{dev_id}", request_json, created_at, dev_id),
    )
    if roi:
        await db.execute(
            "INSERT INTO camera_rois (device_id, camera_id, x, y, w, h, "
            "roi_frame_width, roi_frame_height, source, updated_at) "
            "VALUES (?, ?, 100, 200, 400, 300, 1000, 1000, 'manual', '2026-01-01')",
            (dev_id, camera_id),
        )


async def _new_camera_db(tmp_path, name, source="http://cam:8080/"):
    db_path = str(tmp_path / f"{name}.db")
    await init_db(db_path)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute(
        "INSERT INTO cameras (id, host_id, source, model, pool, status) "
        "VALUES ('cam', 'h', ?, 'fake', 'public', 'available')",
        (source,),
    )
    return db


@pytest.mark.asyncio
async def test_directive_picks_most_recent_visual_job(tmp_path):
    db = await _new_camera_db(tmp_path, "visual")
    try:
        # d1: older, visual; d2: newer, visual -> d2 wins. d3: newest but non-visual.
        await _seed_device_with_roi_and_job(
            db,
            dev_id="d1",
            camera_id="cam",
            created_at="2026-01-01T00:00:00Z",
            request_json='{"params": {"collect_artifacts": ["a.jpg"]}}',
        )
        await _seed_device_with_roi_and_job(
            db,
            dev_id="d2",
            camera_id="cam",
            created_at="2026-01-02T00:00:00Z",
            request_json='{"params": {"collect_artifacts": ["b.jpg"]}}',
        )
        await _seed_device_with_roi_and_job(
            db,
            dev_id="d3",
            camera_id="cam",
            created_at="2026-01-03T00:00:00Z",
            request_json='{"params": {"collect_artifacts": ["c.log"]}}',
        )
        await db.commit()
        directive = await compute_focus_directive(db, "cam")
    finally:
        await db.close()
    assert directive["mode"] == "window"
    assert directive["target_device"] == "d2"
    # ROI (100,200,400,300) on a 1000x1000 frame -> normalized.
    assert directive["window"] == (0.1, 0.2, 0.4, 0.3)


@pytest.mark.asyncio
async def test_directive_manual_mean_when_no_visual_job(tmp_path):
    db = await _new_camera_db(tmp_path, "mean")
    try:
        await _seed_device_with_roi_and_job(
            db,
            dev_id="d1",
            camera_id="cam",
            created_at="2026-01-01T00:00:00Z",
            request_json="{}",
            focus=10.0,
            roi=False,
        )
        await _seed_device_with_roi_and_job(
            db,
            dev_id="d2",
            camera_id="cam",
            created_at="2026-01-02T00:00:00Z",
            request_json="{}",
            focus=20.0,
            roi=False,
        )
        await db.commit()
        directive = await compute_focus_directive(db, "cam")
    finally:
        await db.close()
    assert directive["mode"] == "manual"
    assert directive["position"] == 15.0


@pytest.mark.asyncio
async def test_directive_auto_when_nothing_active(tmp_path):
    db = await _new_camera_db(tmp_path, "empty")
    try:
        directive = await compute_focus_directive(db, "cam")
    finally:
        await db.close()
    assert directive == {"mode": "auto", "window": None, "position": None, "target_device": None}


@pytest.mark.asyncio
async def test_directive_prefer_device_point_shrinks_window(tmp_path):
    db = await _new_camera_db(tmp_path, "prefer")
    try:
        await _seed_device_with_roi_and_job(
            db,
            dev_id="d1",
            camera_id="cam",
            created_at="2026-01-01T00:00:00Z",
            request_json="{}",
        )
        await db.commit()
        region = await compute_focus_directive(
            db, "cam", prefer_device_id="d1", prefer_mode="region"
        )
        point = await compute_focus_directive(db, "cam", prefer_device_id="d1", prefer_mode="point")
    finally:
        await db.close()
    assert region["mode"] == "window"
    assert region["window"] == (0.1, 0.2, 0.4, 0.3)
    # point box is centred on the ROI and strictly smaller than the region.
    assert point["mode"] == "window"
    assert point["window"][2] < region["window"][2]
    assert point["window"][3] < region["window"][3]


@pytest.mark.asyncio
async def test_directive_prefer_device_without_roi_falls_back_to_auto(tmp_path):
    db = await _new_camera_db(tmp_path, "noroi")
    try:
        await _seed_device_with_roi_and_job(
            db,
            dev_id="d1",
            camera_id="cam",
            created_at="2026-01-01T00:00:00Z",
            request_json="{}",
            roi=False,
        )
        await db.commit()
        directive = await compute_focus_directive(
            db, "cam", prefer_device_id="d1", prefer_mode="region"
        )
    finally:
        await db.close()
    assert directive["mode"] == "auto"


# ---- column migration -----------------------------------------------------


@pytest.mark.asyncio
async def test_manual_focus_column_migrated_from_dioptres(tmp_path):
    """A legacy manual_focus_dioptres column is renamed to manual_focus, value kept."""
    db_path = str(tmp_path / "mig.db")
    await init_db(db_path)
    # Simulate a pre-rename DB: put the legacy column name back, with a value.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "ALTER TABLE devices RENAME COLUMN manual_focus TO manual_focus_dioptres"
        )
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, model, capabilities_json, pool, "
            "status, manual_focus_dioptres) "
            "VALUES ('legacy', 'h', 'microcontroller', '', '[]', 'public', 'available', 7.0)"
        )
        await db.commit()
    # Re-running init_db must rename it forward and preserve the stored value.
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("PRAGMA table_info(devices)") as cur:
            cols = {r["name"] for r in await cur.fetchall()}
        assert "manual_focus" in cols
        assert "manual_focus_dioptres" not in cols
        async with db.execute("SELECT manual_focus FROM devices WHERE id='legacy'") as cur:
            row = await cur.fetchone()
        assert row["manual_focus"] == 7.0
