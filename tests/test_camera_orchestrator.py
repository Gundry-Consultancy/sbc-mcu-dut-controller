"""Unit + integration tests for hil_controller.adapters.camera.orchestrator."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import aiosqlite
import pytest

from hil_controller.adapters.camera.orchestrator import (
    camera_base_url,
    compute_brightness_compromise,
    compute_focus_compromise,
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
                    camera_id, manual_focus_dioptres, illuminator_brightness)
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

    # 10 and 20 → midpoint 15.0
    assert result["focus"] == 15.0
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

    assert result["focus"] is None
    assert result["brightness"] is None
    lens_body = next(p["body"] for p in posts if p["path"] == "/lens")
    assert lens_body == {"mode": "auto"}
    illum_body = next(p["body"] for p in posts if p["path"] == "/illuminator")
    assert illum_body == {"brightness": 0}
