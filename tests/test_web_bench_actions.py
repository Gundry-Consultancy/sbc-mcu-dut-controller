"""Tests for /ui/devices/{id}/reset + /ui/devices/{id}/install-tinyuf2 (M3.5).

These two endpoints are the operator-driven side of M3.5 — they bypass
the job queue and drive the SolenoidHubAdapter + TinyUf2Installer
directly. Tests use a stub host_registry + monkey-patched adapter
constructors so no real transports are touched.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hil_controller.adapters.tinyuf2_install import TinyUf2InstallResult
from hil_controller.db.connection import get_db
from hil_controller.hosts.base import ExecResult

TOKEN = "test-token-for-ci"
COOKIE = {"hil_token": TOKEN}


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


@pytest_asyncio.fixture
async def app_with_bench(tmp_path: Path):
    """App with a host_registry stub + a seeded device record."""
    import os

    db_file = str(tmp_path / "bench.db")
    os.environ["HIL_DB_PATH"] = db_file

    from hil_controller.main import create_app

    application = create_app(db_path=db_file)
    async with application.router.lifespan_context(application):
        async with get_db(db_file) as db:
            await db.execute(
                """INSERT INTO hosts (id, role, addr, transport, ssh_user, ssh_key_path,
                       max_concurrent_jobs, capabilities_json, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "rpi-displays",
                    "microcontroller-fleet",
                    "192.168.1.234",
                    "ssh",
                    "pi",
                    "/etc/hil/keys/rpi-displays",
                    None,
                    "[]",
                    "available",
                ),
            )
            await db.execute(
                """INSERT INTO devices (id, host_id, hub_host_id, hub_port_path,
                       solenoid_channel, kind, model, capabilities_json, status, pool)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "mcu-revtft",
                    "rpi-displays",
                    "rpi-displays",
                    "1-1.1.1.4",
                    3,
                    "microcontroller",
                    "feather_esp32s3_reverse_tft",
                    "[]",
                    "available",
                    "public",
                ),
            )
            # Device without solenoid_channel — for the "disabled" path.
            await db.execute(
                """INSERT INTO devices (id, host_id, hub_host_id, hub_port_path,
                       solenoid_channel, kind, model, capabilities_json, status, pool)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "mcu-no-solenoid",
                    "rpi-displays",
                    "rpi-displays",
                    None,
                    None,
                    "microcontroller",
                    "esp32-bare",
                    "[]",
                    "available",
                    "public",
                ),
            )
            await db.commit()

        registry = MagicMock()
        transport = AsyncMock()
        transport.exec = AsyncMock(return_value=_result(0))
        registry.transport_for = MagicMock(return_value=transport)
        application.state.host_registry = registry
        application.state.stub_transport = transport
        yield application


@pytest_asyncio.fixture
async def bench_client(app_with_bench):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_bench),
        base_url="http://test",
    ) as ac:
        yield ac, app_with_bench


# --------------------------------------------------------------------------- #
# /ui/devices/{id}/reset                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reset_calls_solenoid_port_off_then_port_on(bench_client) -> None:
    client, app = bench_client
    r = await client.post("/ui/devices/mcu-revtft/reset", cookies=COOKIE)
    assert r.status_code == 200
    assert "Power-cycled channel 3" in r.text

    argvs = [c.args[0] for c in app.state.stub_transport.exec.call_args_list]
    # power_cycle = port_off followed by port_on
    assert any(a[2:4] == ["port_off", "3"] for a in argvs if len(a) > 3)
    assert any(a[2:4] == ["port_on", "3"] for a in argvs if len(a) > 3)


@pytest.mark.asyncio
async def test_reset_uses_hub_host_id_when_set(bench_client) -> None:
    client, app = bench_client
    await client.post("/ui/devices/mcu-revtft/reset", cookies=COOKIE)
    # transport_for was asked for the hub host, not the device host (same value
    # here but the lookup must use hub_host_id).
    assert app.state.host_registry.transport_for.call_args.args == ("rpi-displays",)


@pytest.mark.asyncio
async def test_reset_refuses_when_solenoid_channel_unset(bench_client) -> None:
    client, _ = bench_client
    r = await client.post("/ui/devices/mcu-no-solenoid/reset", cookies=COOKIE)
    assert r.status_code == 200
    assert "No solenoid channel configured" in r.text
    assert "alert-error" in r.text


@pytest.mark.asyncio
async def test_reset_returns_device_not_found_for_unknown_id(bench_client) -> None:
    client, _ = bench_client
    r = await client.post("/ui/devices/no-such-dev/reset", cookies=COOKIE)
    assert r.status_code == 200
    assert "Device not found" in r.text


@pytest.mark.asyncio
async def test_reset_handles_solenoid_hub_error_cleanly(bench_client) -> None:
    client, app = bench_client
    app.state.stub_transport.exec.return_value = _result(2, stderr="I2C: no ack on 0x20")
    r = await client.post("/ui/devices/mcu-revtft/reset", cookies=COOKIE)
    assert r.status_code == 200
    assert "Reset failed" in r.text
    assert "no ack" in r.text


@pytest.mark.asyncio
async def test_reset_redirects_without_auth(bench_client) -> None:
    client, _ = bench_client
    r = await client.post("/ui/devices/mcu-revtft/reset", follow_redirects=False)
    assert r.status_code == 303


# --------------------------------------------------------------------------- #
# /ui/devices/{id}/power/on|off  (usb-ip page solenoid controls)              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_power_on_presses_port_on(bench_client) -> None:
    client, app = bench_client
    r = await client.post("/ui/devices/mcu-revtft/power/on", cookies=COOKIE)
    assert r.status_code == 200
    assert "Powered ON channel 3" in r.text
    argvs = [c.args[0] for c in app.state.stub_transport.exec.call_args_list]
    assert any(a[2:4] == ["port_on", "3"] for a in argvs if len(a) > 3)


@pytest.mark.asyncio
async def test_power_off_presses_port_off(bench_client) -> None:
    client, app = bench_client
    r = await client.post("/ui/devices/mcu-revtft/power/off", cookies=COOKIE)
    assert r.status_code == 200
    assert "Powered OFF channel 3" in r.text
    argvs = [c.args[0] for c in app.state.stub_transport.exec.call_args_list]
    assert any(a[2:4] == ["port_off", "3"] for a in argvs if len(a) > 3)


@pytest.mark.asyncio
async def test_power_on_refuses_when_solenoid_channel_unset(bench_client) -> None:
    client, _ = bench_client
    r = await client.post("/ui/devices/mcu-no-solenoid/power/on", cookies=COOKIE)
    assert r.status_code == 200
    assert "No solenoid channel configured" in r.text


@pytest.mark.asyncio
async def test_power_on_redirects_without_auth(bench_client) -> None:
    client, _ = bench_client
    r = await client.post("/ui/devices/mcu-revtft/power/on", follow_redirects=False)
    assert r.status_code == 303


# --------------------------------------------------------------------------- #
# /ui/devices/{id}/install-tinyuf2                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_install_tinyuf2_returns_success_panel_with_metadata(
    bench_client, monkeypatch
) -> None:
    client, _ = bench_client

    # Stub TinyUf2Installer to return a canned result without running the chain.
    async def fake_install(self, *, tag="latest", fallback_board=None):
        return TinyUf2InstallResult(
            board_name=self.board_name,
            tag="0.22.0",
            asset_name=f"tinyuf2-{self.board_name}-0.22.0.zip",
            digest_sha256="d" * 64,
            serial_port="/dev/ttyACM0",
            bytes_written=222222,
            elapsed_s=4.2,
        )

    monkeypatch.setattr(
        "hil_controller.adapters.tinyuf2_install.TinyUf2Installer.install",
        fake_install,
    )

    r = await client.post(
        "/ui/devices/mcu-revtft/install-tinyuf2",
        data={
            "board_name": "feather_esp32s3_reverse_tft",
            "fallback_board": "feather_esp32s3",
            "tag": "0.22.0",
            "chip": "esp32s3",
        },
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "TinyUF2 0.22.0 installed" in r.text
    assert "feather_esp32s3_reverse_tft" in r.text
    assert "222222" in r.text  # bytes_written rendered
    assert "alert-success" in r.text


@pytest.mark.asyncio
async def test_install_tinyuf2_refuses_when_hub_port_path_unset(
    bench_client,
) -> None:
    client, _ = bench_client
    r = await client.post(
        "/ui/devices/mcu-no-solenoid/install-tinyuf2",
        data={"board_name": "esp32-bare"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "hub_port_path not set" in r.text
    assert "alert-error" in r.text


@pytest.mark.asyncio
async def test_install_tinyuf2_surfaces_installer_error(bench_client, monkeypatch) -> None:
    client, _ = bench_client
    from hil_controller.adapters.tinyuf2_install import TinyUf2InstallError

    async def fake_install(self, *, tag="latest", fallback_board=None):
        raise TinyUf2InstallError("usbip attach completed but no port appeared")

    monkeypatch.setattr(
        "hil_controller.adapters.tinyuf2_install.TinyUf2Installer.install",
        fake_install,
    )

    r = await client.post(
        "/ui/devices/mcu-revtft/install-tinyuf2",
        data={"board_name": "feather_esp32s3_reverse_tft"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "TinyUF2 install failed" in r.text
    assert "no port appeared" in r.text


@pytest.mark.asyncio
async def test_install_tinyuf2_surfaces_fetcher_404_as_failure(bench_client, monkeypatch) -> None:
    client, _ = bench_client

    async def fake_install(self, *, tag="latest", fallback_board=None):
        raise FileNotFoundError("No tinyuf2 release asset matches ['bogus_board'] in tag='latest'")

    monkeypatch.setattr(
        "hil_controller.adapters.tinyuf2_install.TinyUf2Installer.install",
        fake_install,
    )

    r = await client.post(
        "/ui/devices/mcu-revtft/install-tinyuf2",
        data={"board_name": "bogus_board"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "TinyUF2 install failed" in r.text
    assert "No tinyuf2 release asset" in r.text


@pytest.mark.asyncio
async def test_install_tinyuf2_404s_for_unknown_device(bench_client) -> None:
    client, _ = bench_client
    r = await client.post(
        "/ui/devices/nope/install-tinyuf2",
        data={"board_name": "x"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "Device not found" in r.text


@pytest.mark.asyncio
async def test_install_tinyuf2_redirects_without_auth(bench_client) -> None:
    client, _ = bench_client
    r = await client.post(
        "/ui/devices/mcu-revtft/install-tinyuf2",
        data={"board_name": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# --------------------------------------------------------------------------- #
# Serial-tail color in _render_events                                         #
# --------------------------------------------------------------------------- #


def test_render_events_distinguishes_serial_stream_with_green() -> None:
    from hil_controller.web.router import _render_events

    events = [
        {
            "kind": "log",
            "at": "2026-06-07T17:00:00",
            "payload": {"stream": "serial", "msg": "rst:0x1 PASS"},
        },
        {
            "kind": "log",
            "at": "2026-06-07T17:00:01",
            "payload": {"stream": "stdout", "msg": "build ok"},
        },
    ]
    rendered = _render_events(events)
    # Serial line uses the new green; stdout uses the original grey-ish.
    assert "#7ee787" in rendered
    assert "rst:0x1 PASS" in rendered
    assert "#c9d1d9" in rendered  # stdout color preserved
    assert "build ok" in rendered
