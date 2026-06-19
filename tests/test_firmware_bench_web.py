"""Web/API tests for the firmware-bench form, request builder, and extend route."""

from __future__ import annotations

import pytest

from hil_controller.web.router import (
    _build_firmware_bench_job_request,
    _firmware_bench_stages_from_form,
)

COOKIE = {"hil_token": "test-token-for-ci"}


# --------------------------------------------------------------------------- #
# stage assembly                                                              #
# --------------------------------------------------------------------------- #


def test_stages_from_form_full_loop_order() -> None:
    # The full loop: power-cycle (boot app → MSC enumerates) BEFORE secrets,
    # then a final power-cycle to apply them.
    form = {
        "offset": "0x0",
        "stage_erase": "on",
        "stage_flash": "on",
        "flasher": "esptool",
        "stage_verify": "on",
        "stage_power_boot": "on",
        "stage_secrets": "on",
        "stage_power_final": "on",
        "power_off_s": "1.0",
    }
    stages = _firmware_bench_stages_from_form(form)
    assert [s["type"] for s in stages] == [
        "erase",
        "flash",
        "verify",
        "power_cycle",
        "write_secrets_msc",
        "power_cycle",
    ]
    assert stages[1] == {
        "type": "flash",
        "offset": "0x0",
        "after": "no_reset",
        "flasher": "esptool",
    }


def test_stages_from_form_flash_only_order() -> None:
    form = {
        "stage_erase": "on",
        "stage_flash": "on",
        "stage_verify": "on",
        "stage_power_final": "on",
        "offset": "0x0",
    }
    stages = _firmware_bench_stages_from_form(form)
    assert [s["type"] for s in stages] == ["erase", "flash", "verify", "power_cycle"]


def test_stages_from_form_touch_optional() -> None:
    stages = _firmware_bench_stages_from_form(
        {"stage_touch": "on", "touch_settle_s": "3", "stage_flash": "on"}
    )
    assert stages[0] == {"type": "bootloader_touch", "settle_s": 3.0}


def test_stages_advanced_json_overrides_checkboxes() -> None:
    form = {
        "stage_erase": "on",
        "stage_flash": "on",
        "advanced_stages": '[{"type":"flash","offset":"0x10000"}]',
    }
    stages = _firmware_bench_stages_from_form(form)
    assert stages == [{"type": "flash", "offset": "0x10000"}]


def test_stages_advanced_json_must_be_list() -> None:
    with pytest.raises(ValueError):
        _firmware_bench_stages_from_form({"advanced_stages": '{"type":"flash"}'})


# --------------------------------------------------------------------------- #
# request builder                                                             #
# --------------------------------------------------------------------------- #


def test_build_request_shape_and_secret_omission() -> None:
    req = _build_firmware_bench_job_request(
        device_id="dut-1",
        pool="public",
        firmware_path="/srv/combined.bin",
        offset="0x0",
        stages=[{"type": "flash", "offset": "0x0"}],
        window_minutes=45,
        flash_port_filter="boot",
        log_port_filter="cdc",
        msc_filter="QT_Py",
        flash_serial_port="",
        log_serial_port="",
        esptool_chip="esp32s3",
        esptool_baud=921600,
        serial_baud=115200,
        protomq_repo="https://x/protomq.git",
        protomq_ref="displays-v2",
        protomq_script="echo",
        secrets_profile="bench-protomq",
        io_username="u",
        io_key="",
        wifi_ssid="",
        wifi_password="",
    )
    assert req["script"] == "firmware-bench"
    assert req["payload"] == {
        "kind": "firmware-bin",
        "firmware": {"path": "/srv/combined.bin", "offset": "0x0"},
    }
    assert req["target"]["device"] == {"id": "dut-1"}
    assert req["params"]["window_minutes"] == 45
    assert req["params"]["msc_filter"] == "QT_Py"
    assert req["params"]["firmware"]["path"] == "/srv/combined.bin"
    # only non-empty secrets are included
    assert req["secrets"] == {"IO_USERNAME": "u"}


def test_build_request_no_device_falls_back_to_capability_selector() -> None:
    req = _build_firmware_bench_job_request(
        device_id="",
        pool="public",
        firmware_path="/x.bin",
        offset="0x0",
        stages=[],
        window_minutes=30,
        flash_port_filter="",
        log_port_filter="",
        msc_filter="",
        flash_serial_port="",
        log_serial_port="",
        esptool_chip="auto",
        esptool_baud=921600,
        serial_baud=115200,
        protomq_repo="",
        protomq_ref="",
        protomq_script="",
        secrets_profile="bench-protomq",
        io_username="",
        io_key="",
        wifi_ssid="",
        wifi_password="",
    )
    assert req["target"]["device"] == {"kind": "microcontroller", "capabilities": ["wippersnapper"]}


# --------------------------------------------------------------------------- #
# web GET / POST                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_new_firmware_bench_page_renders(client) -> None:
    r = await client.get("/ui/jobs/new-firmware-bench", cookies=COOKIE)
    assert r.status_code == 200
    assert "Firmware Bench Session" in r.text
    assert 'name="msc_filter"' in r.text


@pytest.mark.asyncio
async def test_post_firmware_bench_with_server_path_redirects(client) -> None:
    r = await client.post(
        "/ui/jobs/firmware-bench",
        data={
            "device_id": "",
            "pool": "public",
            "firmware_source": "path",
            "firmware_path": "/srv/combined.bin",
            "offset": "0x0",
            "window_minutes": "30",
            "stage_erase": "on",
            "stage_flash": "on",
            "stage_verify": "on",
            "stage_power_boot": "on",
            "stage_secrets": "on",
            "stage_power_final": "on",
            "flasher": "esptool",
            "secrets_profile": "bench-protomq",
        },
        cookies=COOKIE,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/jobs/")


@pytest.mark.asyncio
async def test_unchecked_stage_checkbox_is_excluded(client, app) -> None:
    # Regression: an unchecked checkbox sends nothing; the handler default must
    # be "" so the stage is truly dropped (not silently re-included). Here
    # stage_secrets is omitted → no write_secrets_msc → protomq won't launch.
    import json

    import aiosqlite

    r = await client.post(
        "/ui/jobs/firmware-bench",
        data={
            "firmware_source": "path",
            "firmware_path": "/srv/x.bin",
            "offset": "0x0",
            "window_minutes": "3",
            "stage_erase": "on",
            "stage_flash": "on",
            "stage_verify": "on",
            "stage_power_final": "on",
            # stage_secrets + stage_power_boot + stage_touch intentionally omitted (unchecked)
            "secrets_profile": "bench-protomq",
        },
        cookies=COOKIE,
        follow_redirects=False,
    )
    assert r.status_code == 303
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    async with aiosqlite.connect(app.state.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT request_json FROM jobs WHERE id=?", (job_id,))
        row = await cur.fetchone()
    types = [s["type"] for s in json.loads(row["request_json"])["params"]["stages"]]
    assert types == ["erase", "flash", "verify", "power_cycle"]
    assert "write_secrets_msc" not in types


@pytest.mark.asyncio
async def test_post_firmware_bench_missing_firmware_shows_error(client) -> None:
    r = await client.post(
        "/ui/jobs/firmware-bench",
        data={"firmware_source": "path", "firmware_path": "", "stage_flash": "on"},
        cookies=COOKIE,
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Firmware path is required" in r.text


# --------------------------------------------------------------------------- #
# extend endpoint                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extend_unknown_job_404(authed_client) -> None:
    r = await authed_client.post("/v1/jobs/does-not-exist/extend", json={"minutes": 15})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_extend_job_without_lease_409(authed_client) -> None:
    # Submit a normal job (no device match → holds no lease), then try to extend.
    sub = await authed_client.post(
        "/v1/jobs",
        json={
            "target": {"device": {"kind": "microcontroller"}, "pool": "public"},
            "script": "noop",
        },
    )
    job_id = sub.json()["id"]
    r = await authed_client.post(f"/v1/jobs/{job_id}/extend", json={"minutes": 15})
    assert r.status_code == 409
