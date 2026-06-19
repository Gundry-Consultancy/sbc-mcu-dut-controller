"""Tests for the Jinja2/HTMX web UI."""

from datetime import UTC

import pytest

TOKEN = "test-token-for-ci"
COOKIE = {"hil_token": TOKEN}


def _created(r) -> bool:
    """Success: either HX-Redirect header (new) or 200 with content (old error path)."""
    return r.status_code == 200 and ("HX-Redirect" in r.headers or r.text == "")


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_page_renders(client):
    r = await client.get("/ui/login")
    assert r.status_code == 200
    assert "Bearer Token" in r.text


@pytest.mark.asyncio
async def test_login_invalid_token_returns_401(client):
    r = await client.post("/ui/login", data={"token": "bad-token"})
    assert r.status_code == 401
    assert "Invalid token" in r.text


@pytest.mark.asyncio
async def test_login_valid_token_sets_cookie_and_redirects(client):
    r = await client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/"
    assert "hil_token" in r.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_logout_clears_cookie(client):
    r = await client.get("/ui/logout", follow_redirects=False)
    assert r.status_code == 303
    assert "hil_token" in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_redirects_without_cookie(client):
    r = await client.get("/ui/", follow_redirects=False)
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]


@pytest.mark.asyncio
async def test_hosts_redirects_without_cookie(client):
    r = await client.get("/ui/hosts", follow_redirects=False)
    assert r.status_code == 303


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders(client):
    r = await client.get("/ui/", cookies=COOKIE)
    assert r.status_code == 200
    assert "Dashboard" in r.text
    assert "Hosts" in r.text


# ---------------------------------------------------------------------------
# Asset view route + job-detail assets link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_view_log_asset_serves_text(client, app, tmp_path):
    import uuid
    from datetime import datetime, timezone

    from hil_controller.db.connection import get_db, insert_job

    db_path = app.state.db_path
    job_id = "asset-job-1"
    log_file = tmp_path / "deploy.log"
    log_file.write_text("toolchain error: Dynconfig not exist\n")
    aid = str(uuid.uuid4())
    async with get_db(db_path) as db:
        await insert_job(
            db,
            job_id=job_id,
            request_json={"params": {}, "payload": {"kind": "git-source"}},
            secrets_profile="p",
            exclusive_host=False,
        )
        await db.execute(
            "INSERT INTO assets (id, filename, path, size_bytes, kind, job_id, created_at) "
            "VALUES (?, 'deploy.log', ?, 10, 'log', ?, ?)",
            (aid, str(log_file), job_id, datetime.now(UTC).isoformat()),
        )
        await db.commit()

    # view route serves the file as text
    r = await client.get(f"/ui/assets/{aid}/view", cookies=COOKIE)
    assert r.status_code == 200
    assert "Dynconfig" in r.text

    # job detail page links the asset
    r = await client.get(f"/ui/jobs/{job_id}", cookies=COOKIE)
    assert r.status_code == 200
    assert f"/ui/assets/{aid}/view" in r.text
    assert "deploy.log" in r.text


@pytest.mark.asyncio
async def test_view_asset_requires_auth(client):
    r = await client.get("/ui/assets/nope/view", follow_redirects=False)
    assert r.status_code in (303, 307)


# ---------------------------------------------------------------------------
# Hosts CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hosts_page_renders_empty(client):
    r = await client.get("/ui/hosts", cookies=COOKIE)
    assert r.status_code == 200
    assert "Hosts" in r.text


@pytest.mark.asyncio
async def test_new_host_form_renders(client):
    r = await client.get("/ui/hosts/form", cookies=COOKIE)
    assert r.status_code == 200
    assert "New Host" in r.text


@pytest.mark.asyncio
async def test_create_host_returns_hx_redirect(client):
    r = await client.post(
        "/ui/hosts",
        data={
            "id": "test-host-01",
            "role": "sbc-fleet",
            "addr": "10.0.0.1",
            "transport": "ssh",
            "ssh_user": "pi",
            "ssh_key_path": "/etc/hil/keys/test",
            "max_concurrent_jobs": "1",
            "capabilities": "linux, cameras",
            "status": "available",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/ui/hosts"


@pytest.mark.asyncio
async def test_create_host_missing_id_shows_error(client):
    r = await client.post(
        "/ui/hosts",
        data={
            "id": "",
            "role": "sbc-fleet",
            "addr": "",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "required" in r.text.lower()
    assert "HX-Redirect" not in r.headers


@pytest.mark.asyncio
async def test_edit_host_form_renders(client):
    await client.post(
        "/ui/hosts",
        data={
            "id": "edit-host-01",
            "role": "sbc-fleet",
            "addr": "10.0.0.2",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.get("/ui/hosts/edit-host-01/form", cookies=COOKIE)
    assert r.status_code == 200
    assert "edit-host-01" in r.text
    assert "Edit Host" in r.text


@pytest.mark.asyncio
async def test_update_host_returns_hx_redirect(client):
    await client.post(
        "/ui/hosts",
        data={
            "id": "upd-host-01",
            "role": "sbc-fleet",
            "addr": "10.0.0.3",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.post(
        "/ui/hosts/upd-host-01",
        data={
            "role": "microcontroller-fleet",
            "addr": "10.0.0.33",
            "transport": "ssh",
            "ssh_user": "admin",
            "status": "available",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/ui/hosts"


@pytest.mark.asyncio
async def test_delete_host_returns_empty(client):
    await client.post(
        "/ui/hosts",
        data={
            "id": "del-host-01",
            "role": "sbc-fleet",
            "addr": "10.0.0.4",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.delete("/ui/hosts/del-host-01", cookies=COOKIE)
    assert r.status_code == 200
    assert r.text == ""


# ---------------------------------------------------------------------------
# Devices CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_devices_page_renders(client):
    await client.post(
        "/ui/hosts",
        data={
            "id": "devices-host-01",
            "role": "microcontroller-fleet",
            "addr": "10.0.1.40",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    await client.post(
        "/ui/devices",
        data={
            "id": "devices-page-mcu-01",
            "host_id": "devices-host-01",
            "kind": "microcontroller",
            "model": "esp32-s3",
            "hub_port_path": "1-1.1.3",
            "pool": "public",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.get("/ui/devices", cookies=COOKIE)
    assert r.status_code == 200
    assert "Devices" in r.text
    assert "Hub port" in r.text
    assert "1-1.1.3" in r.text


@pytest.mark.asyncio
async def test_new_device_form_renders(client):
    r = await client.get("/ui/devices/form", cookies=COOKIE)
    assert r.status_code == 200
    assert "New Device" in r.text
    # the "Discover busids" button passes the token to its fetch — the new form must
    # render it (regression: it was omitted → button 401'd "Missing token")
    assert f"discoverBusids('new', '{TOKEN}')" in r.text


@pytest.mark.asyncio
async def test_device_form_has_camera_dropdown(client):
    """Camera ID must be a <select> element, not a free-text input."""
    r = await client.get("/ui/devices/form", cookies=COOKIE)
    assert r.status_code == 200
    # Select element with name="camera_id" must be present
    assert 'name="camera_id"' in r.text
    assert "<select" in r.text
    # "— none —" sentinel option must always be rendered
    assert "none" in r.text.lower()


@pytest.mark.asyncio
async def test_device_form_camera_dropdown_lists_cameras(client):
    """Camera options appear in the dropdown once a camera is seeded."""
    cam_r = await client.post(
        "/ui/cameras",
        data={
            "id": "test-cam-01",
            "model": "Test Webcam",
            "pool": "public",
            "status": "available",
            "stream_url": "http://10.0.0.1:8080/shot.jpg",
            "stream_type": "snapshot",
        },
        cookies=COOKIE,
    )
    assert cam_r.headers.get("HX-Redirect") == "/ui/cameras", cam_r.text
    r = await client.get("/ui/devices/form", cookies=COOKIE)
    assert r.status_code == 200
    assert "test-cam-01" in r.text


@pytest.mark.asyncio
async def test_create_device(client):
    await client.post(
        "/ui/hosts",
        data={
            "id": "dev-host-01",
            "role": "microcontroller-fleet",
            "addr": "10.0.1.1",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.post(
        "/ui/devices",
        data={
            "id": "qtpy-test-01",
            "host_id": "dev-host-01",
            "kind": "microcontroller",
            "model": "esp32-s3",
            "pool": "public",
            "capabilities": "spi, i2c",
            "status": "available",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/ui/devices"


@pytest.mark.asyncio
async def test_create_device_with_focus_and_brightness_overrides(client):
    await client.post(
        "/ui/hosts",
        data={
            "id": "focus-host-01",
            "role": "microcontroller-fleet",
            "addr": "10.0.1.99",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.post(
        "/ui/devices",
        data={
            "id": "focus-dev-01",
            "host_id": "focus-host-01",
            "kind": "microcontroller",
            "model": "esp32-s3",
            "pool": "public",
            "status": "available",
            "manual_focus_dioptres": "12.5",
            "illuminator_brightness": "192",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/ui/devices"

    # Confirm the values landed in the DB by reading them back via the API.
    r = await client.get(
        "/v1/devices/focus-dev-01",
        headers={"Authorization": "Bearer test-token-for-ci"},
    )
    # The /v1/devices/{id} response doesn't expose these fields yet, so
    # round-trip via the form GET which renders saved values into HTML.
    r = await client.get("/ui/devices/focus-dev-01/form", cookies=COOKIE)
    assert r.status_code == 200
    assert 'value="12.5"' in r.text
    assert 'value="192"' in r.text


@pytest.mark.asyncio
async def test_blank_focus_brightness_stored_as_null(client):
    await client.post(
        "/ui/hosts",
        data={
            "id": "focus-host-02",
            "role": "microcontroller-fleet",
            "addr": "10.0.1.98",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.post(
        "/ui/devices",
        data={
            "id": "focus-dev-02",
            "host_id": "focus-host-02",
            "kind": "microcontroller",
            "pool": "public",
            "status": "available",
            "manual_focus_dioptres": "",
            "illuminator_brightness": "",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    r = await client.get("/ui/devices/focus-dev-02/form", cookies=COOKIE)
    # Both fields render with the saved-null state — value="" attached
    # specifically to these two inputs (not just any empty input on the page).
    import re

    focus_match = re.search(r'<input[^>]*name="manual_focus_dioptres"[^>]*value="([^"]*)"', r.text)
    brightness_match = re.search(
        r'<input[^>]*name="illuminator_brightness"[^>]*value="([^"]*)"', r.text
    )
    assert focus_match is not None and focus_match.group(1) == ""
    assert brightness_match is not None and brightness_match.group(1) == ""


@pytest.mark.asyncio
async def test_delete_device(client):
    await client.post(
        "/ui/hosts",
        data={
            "id": "dev-host-02",
            "role": "microcontroller-fleet",
            "addr": "10.0.1.2",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    await client.post(
        "/ui/devices",
        data={
            "id": "del-device-01",
            "host_id": "dev-host-02",
            "kind": "microcontroller",
            "model": "rp2040",
            "pool": "public",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.delete("/ui/devices/del-device-01", cookies=COOKIE)
    assert r.status_code == 200
    assert r.text == ""


# ---------------------------------------------------------------------------
# Hardware / Aux CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hardware_page_renders(client):
    r = await client.get("/ui/hardware", cookies=COOKIE)
    assert r.status_code == 200
    assert "Hardware" in r.text


@pytest.mark.asyncio
async def test_create_hardware(client):
    r = await client.post(
        "/ui/hardware",
        data={
            "id": "oled-test-01",
            "kind": "display",
            "model": "ssd1306",
            "interface": "i2c",
            "observability": "camera",
            "capabilities": "display:128x32",
            "pool": "public",
            "status": "available",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/ui/hardware"


@pytest.mark.asyncio
async def test_delete_hardware(client):
    await client.post(
        "/ui/hardware",
        data={
            "id": "del-hw-01",
            "kind": "sensor",
            "model": "ens160",
            "interface": "i2c",
            "observability": "none",
            "pool": "public",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.delete("/ui/hardware/del-hw-01", cookies=COOKIE)
    assert r.status_code == 200
    assert r.text == ""


# ---------------------------------------------------------------------------
# Cameras CRUD (multi-stream)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cameras_page_renders(client):
    r = await client.get("/ui/cameras", cookies=COOKIE)
    assert r.status_code == 200
    assert "Camera" in r.text


@pytest.mark.asyncio
async def test_create_camera_single_stream(client):
    r = await client.post(
        "/ui/cameras",
        data={
            "id": "cam-bench-01",
            "model": "Wyze Cam v3",
            "stream_url": "rtsp://192.168.1.50/stream",
            "stream_type": "rtsp",
            "pool": "public",
            "status": "available",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/ui/cameras"


@pytest.mark.asyncio
async def test_create_camera_multi_stream(client):
    from urllib.parse import urlencode

    body = urlencode(
        [
            ("id", "cam-multi-01"),
            ("model", "PoE Cam"),
            ("stream_url", "rtsp://192.168.1.51/stream"),
            ("stream_type", "rtsp"),
            ("stream_url", "http://192.168.1.51/snapshot.jpg"),
            ("stream_type", "snapshot"),
            ("pool", "public"),
            ("status", "available"),
        ]
    )
    r = await client.post(
        "/ui/cameras",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/ui/cameras"

    # verify both streams stored in the cameras table
    cams = await client.get(
        "/v1/cameras/cam-multi-01", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert cams.status_code == 200
    data = cams.json()
    assert len(data["streams"]) == 2


@pytest.mark.asyncio
async def test_create_camera_no_stream_shows_error(client):
    r = await client.post(
        "/ui/cameras",
        data={"id": "cam-nostream", "model": "test", "pool": "public", "status": "available"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers
    assert "required" in r.text.lower()


@pytest.mark.asyncio
async def test_delete_camera(client):
    await client.post(
        "/ui/cameras",
        data={
            "id": "del-cam-01",
            "model": "test",
            "stream_url": "rtsp://10.0.0.100",
            "stream_type": "rtsp",
            "pool": "public",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.delete("/ui/cameras/del-cam-01", cookies=COOKIE)
    assert r.status_code == 200
    assert r.text == ""


@pytest.mark.asyncio
async def test_camera_preview_requires_auth(client):
    r = await client.get("/ui/cameras/preview?url=http://cam/shot.jpg")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_camera_preview_no_url_returns_400(client):
    r = await client.get("/ui/cameras/preview", cookies=COOKIE)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_camera_preview_unreachable_returns_503(client):
    r = await client.get(
        "/ui/cameras/preview?url=http://192.0.2.1/shot.jpg",  # TEST-NET, unreachable
        cookies=COOKIE,
    )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_delete_connection(client):
    await client.post(
        "/ui/hosts",
        data={
            "id": "conn-host-01",
            "role": "microcontroller-fleet",
            "addr": "10.0.2.1",
            "transport": "ssh",
            "ssh_user": "pi",
            "status": "available",
        },
        cookies=COOKIE,
    )
    await client.post(
        "/ui/devices",
        data={
            "id": "conn-dev-01",
            "host_id": "conn-host-01",
            "kind": "microcontroller",
            "model": "esp32-s3",
            "pool": "public",
            "status": "available",
        },
        cookies=COOKIE,
    )
    await client.post(
        "/ui/hardware",
        data={
            "id": "conn-hw-01",
            "kind": "display",
            "model": "ssd1306",
            "interface": "i2c",
            "observability": "none",
            "pool": "public",
            "status": "available",
        },
        cookies=COOKIE,
    )
    r = await client.post(
        "/ui/connections",
        data={"aux_id": "conn-hw-01", "device_id": "conn-dev-01"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "conn-dev-01" in r.text

    import re

    m = re.search(r'id="conn-(\d+)"', r.text)
    assert m, "connection id not found in response"
    conn_id = m.group(1)

    r2 = await client.delete(f"/ui/connections/{conn_id}", cookies=COOKIE)
    assert r2.status_code == 200
    assert r2.text == ""


# ---------------------------------------------------------------------------
# Scripts page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scripts_page_no_dir_configured(client):
    r = await client.get("/ui/scripts", cookies=COOKIE)
    assert r.status_code == 200
    assert "HIL_SCRIPTS_DIR" in r.text


@pytest.mark.asyncio
async def test_scripts_page_with_dir(tmp_path, client):
    import json
    import os

    script = {"name": "Test Script", "description": "A test", "protoVersion": "v2", "steps": []}
    (tmp_path / "test-script.json").write_text(json.dumps(script))

    os.environ["HIL_SCRIPTS_DIR"] = str(tmp_path)
    from hil_controller import config as cfg

    cfg._settings = None

    try:
        r = await client.get("/ui/scripts", cookies=COOKIE)
        assert r.status_code == 200
        assert "Test Script" in r.text
    finally:
        os.environ.pop("HIL_SCRIPTS_DIR", None)
        cfg._settings = None


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_css_served(client):
    r = await client.get("/ui/static/app.css")
    assert r.status_code == 200
    assert "body" in r.text


# ---------------------------------------------------------------------------
# Jobs UI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jobs_page_renders(client):
    r = await client.get("/ui/jobs", cookies=COOKIE)
    assert r.status_code == 200
    assert "Test Jobs" in r.text


@pytest.mark.asyncio
async def test_new_job_page_renders(client):
    r = await client.get("/ui/jobs/new", cookies=COOKIE)
    assert r.status_code == 200
    assert "Repository URL" in r.text
    assert "No hardware" in r.text
    assert "BLINKA_OS_AGNOSTIC" in r.text


@pytest.mark.asyncio
async def test_submit_job_missing_repo_shows_error(client):
    r = await client.post(
        "/ui/jobs",
        data={
            "repo": "",
            "ref": "main",
            "hw_mode": "no_hardware",
            "test_args": '-m "not hardware" -v',
            "secrets_profile": "bench-protomq",
            "timeout_total": "600",
            "timeout_run": "300",
            "timeout_deploy": "180",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "required" in r.text.lower()


@pytest.mark.asyncio
async def test_jobs_list_partial(client):
    r = await client.get("/ui/jobs/list", cookies=COOKIE)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_job_detail_not_found(client):
    r = await client.get("/ui/jobs/nonexistent-id", cookies=COOKIE)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_job_log_partial_not_found(client):
    r = await client.get("/ui/jobs/nonexistent-id/log", cookies=COOKIE)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Arduino WipperSnapper test job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_arduino_ws_page_renders(client):
    r = await client.get("/ui/jobs/new-arduino-ws", cookies=COOKIE)
    assert r.status_code == 200
    assert "WipperSnapper" in r.text
    assert "protomq" in r.text.lower()


@pytest.mark.asyncio
async def test_new_arduino_ws_page_requires_auth(client):
    r = await client.get("/ui/jobs/new-arduino-ws", follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.asyncio
async def test_new_arduino_ws_page_shows_default_protomq_ref(client):
    r = await client.get("/ui/jobs/new-arduino-ws", cookies=COOKIE)
    assert r.status_code == 200
    assert "protomq_ref" in r.text
    assert "wippersnapper_ref" in r.text
    assert "pio_env" in r.text
    assert "serial_port" in r.text
    assert "PlatformIO" in r.text


def _build_ws(**over):
    from hil_controller.web.router import _build_arduino_ws_job_request

    base = dict(
        wippersnapper_repo="https://example/ws.git",
        wippersnapper_ref="displays-v2",
        protomq_repo="https://example/protomq.git",
        protomq_ref="displays-v2-testing",
        pat="",
        submodules=True,
        pio_env="adafruit_feather_esp32s3_reversetft",
        serial_port="/dev/ttyACM0",
        setup="",
        test_cmd="pytest",
        protomq_script="",
        device_id="",
        secrets_profile="bench-protomq",
        mqtt_host="",
        mqtt_port="1884",
        io_username="",
        io_key="",
        timeout_total=1200,
        timeout_run=300,
        timeout_deploy=900,
    )
    base.update(over)
    return _build_arduino_ws_job_request(**base)


def test_ws_builder_does_not_pin_arduino_pool():
    req = _build_ws()
    # No hardcoded pool: matching defaults to 'public' where the MCUs live.
    assert "pool" not in req["target"]


def test_ws_builder_auto_selector_requests_wippersnapper_capability():
    req = _build_ws(device_id="")
    assert req["target"]["device"]["capabilities"] == ["wippersnapper"]


def test_ws_builder_explicit_device_id_passthrough():
    req = _build_ws(device_id="mcu-feather-esp32s3-revtft")
    assert req["target"]["device"] == {"id": "mcu-feather-esp32s3-revtft"}


def test_ws_builder_protomq_clone_recurses_submodules_when_enabled():
    req = _build_ws(submodules=True)
    setup = req["payload"]["source"]["setup"][2]
    assert "git clone --depth 1 --recurse-submodules --branch displays-v2-testing" in setup
    assert req["payload"]["source"]["submodules"] is True


def test_ws_builder_protomq_clone_no_recurse_when_disabled():
    req = _build_ws(submodules=False)
    setup = req["payload"]["source"]["setup"][2]
    assert "--recurse-submodules" not in setup


def test_ws_builder_default_build_steps_create_venv_before_platformio():
    req = _build_ws(build_steps="")
    setup = req["payload"]["source"]["setup"][2]
    # venv is created with --system-site-packages and activated before pip/pio.
    assert "python3 -m venv --system-site-packages .venv" in setup
    assert setup.index(".venv") < setup.index("pip install platformio")
    assert "pio run -e adafruit_feather_esp32s3_reversetft" in setup


def test_ws_builder_default_build_steps_are_compile_only():
    # Compile and flash are now separate phases — the build chain must NOT
    # upload (the controller has no DUT serial port; flash runs over usbip).
    req = _build_ws(build_steps="")
    setup = req["payload"]["source"]["setup"][2]
    assert "--target upload" not in setup


def test_ws_builder_emits_exec_plan_with_defaults():
    req = _build_ws()
    exec_plan = req["params"]["exec"]
    assert exec_plan["build_host"] == "controller"
    assert exec_plan["flash_mode"] == "usbip"
    assert exec_plan["test_host"] == "none"
    assert exec_plan["protomq_host"] == "controller"
    assert exec_plan["pio_env"] == "adafruit_feather_esp32s3_reversetft"


def test_ws_builder_controller_protomq_points_mqtt_host_at_controller_ip():
    req = _build_ws(
        protomq_host="controller",
        controller_ip="192.168.1.169",
        mqtt_host="127.0.0.1",
        protomq_script="reverse-tft-s3-demo",
    )
    assert req["secrets"]["MQTT_HOST"] == "192.168.1.169"
    assert req["params"]["protomq"]["broker_host"] == "192.168.1.169"


def test_ws_builder_dut_protomq_keeps_given_mqtt_host():
    req = _build_ws(protomq_host="dut-host", controller_ip="192.168.1.169", mqtt_host="127.0.0.1")
    assert req["secrets"]["MQTT_HOST"] == "127.0.0.1"


def test_ws_builder_custom_build_steps_override_default():
    req = _build_ws(build_steps="echo custom-build")
    setup = req["payload"]["source"]["setup"][2]
    assert "echo custom-build" in setup
    assert "pip install platformio" not in setup


def test_parse_github_repo_variants():
    from hil_controller.web.router import _parse_github_repo

    assert _parse_github_repo("https://github.com/tyeth/protomq.git") == ("tyeth", "protomq")
    assert _parse_github_repo("https://github.com/tyeth/protomq") == ("tyeth", "protomq")
    assert _parse_github_repo("git@github.com:tyeth/protomq.git") == ("tyeth", "protomq")
    assert _parse_github_repo("https://gitlab.com/x/y.git") is None
    assert _parse_github_repo("") is None


@pytest.mark.asyncio
async def test_ws_scripts_refresh_rejects_non_github(client):
    r = await client.get(
        "/ui/jobs/arduino-ws/scripts",
        params={"protomq_repo": "https://gitlab.com/x/y.git", "protomq_ref": "main"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "only github.com repos" in r.text
