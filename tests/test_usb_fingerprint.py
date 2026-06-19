"""Tests for UsbFingerprintAdapter — active depower+repower VID/PID capture."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_TOPOLOGY = """
hosts:
  - id: hub-a
    role: microcontroller-fleet
    addr: 127.0.0.10
    transport: fake
    ssh_user: pi
    ssh_key_path: /tmp/k
    capabilities: [usbip-server, power-control]

devices:
  - id: dev1
    host_id: hub-a
    kind: microcontroller
    pool: public
    status: available
    hub_port_path: "1-1.1.3"
    solenoid_channel: 3
    hub_host_id: hub-a
  - id: no-hub
    host_id: hub-a
    kind: microcontroller
    pool: public
    status: available
"""


@pytest_asyncio.fixture
async def app(tmp_path: Path):
    db_file = str(tmp_path / "fp.db")
    topo = tmp_path / "t.yaml"
    topo.write_text(_TOPOLOGY)
    os.environ["HIL_DB_PATH"] = db_file
    from hil_controller.main import create_app

    a = create_app(db_path=db_file, topology_file=str(topo))
    async with a.router.lifespan_context(a):
        a.state._test_db = db_file
        yield a


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token-for-ci"},
    ) as ac:
        yield ac


class FakeHub:
    """Records solenoid + scan calls so tests can assert ordering."""

    def __init__(self, sequence: list[list[dict]]):
        self._sequence = list(sequence)
        self.calls: list[str] = []

    async def all_off(self):
        self.calls.append("all_off")

    async def port_on(self, ch: int):
        self.calls.append(f"port_on:{ch}")

    async def port_off(self, ch: int, **kw):
        self.calls.append(f"port_off:{ch}")

    def scan(self) -> list[dict]:
        self.calls.append("scan")
        return self._sequence.pop(0) if self._sequence else []


# -- adapter logic --------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_captures_runtime_id(app):
    from hil_controller.adapters.usb_fingerprint import UsbFingerprintAdapter

    fake_hub = FakeHub(
        sequence=[
            # baseline (everything off): empty
            [],
            # after port_on: one device appears
            [{"busid": "1-1.1.3", "vid": "239a", "pid": "8053", "description": "WipperSnapper"}],
        ]
    )
    adapter = UsbFingerprintAdapter(
        db_path=app.state._test_db,
        hub=fake_hub,
        scan_fn=fake_hub.scan,
        settle_s=0,
    )
    rows = await adapter.learn(device_id="dev1", job_id="learn-1")

    assert len(rows) == 1
    assert rows[0]["vid"] == "239a"
    assert rows[0]["pid"] == "8053"
    assert rows[0]["source"] == "learn-job"
    # ordering: all_off → scan baseline → port_on → scan capture
    assert fake_hub.calls[0] == "all_off"
    assert "port_on:3" in fake_hub.calls


@pytest.mark.asyncio
async def test_fingerprint_classifies_bootloader_vs_runtime(app):
    """With include_reset_cycle, two distinct VIDs/PIDs get role tags."""
    from hil_controller.adapters.usb_fingerprint import UsbFingerprintAdapter

    fake_hub = FakeHub(
        sequence=[
            [],  # baseline
            [
                {"busid": "1-1.1.3", "vid": "239a", "pid": "0035", "description": "UF2"}
            ],  # bootloader after first power-on
            [
                {"busid": "1-1.1.3", "vid": "239a", "pid": "8053", "description": "WipperSnapper"}
            ],  # runtime after reset
        ]
    )
    adapter = UsbFingerprintAdapter(
        db_path=app.state._test_db,
        hub=fake_hub,
        scan_fn=fake_hub.scan,
        settle_s=0,
    )
    rows = await adapter.learn(device_id="dev1", job_id="learn-2", include_reset_cycle=True)

    roles = {r["pid"]: r["role"] for r in rows}
    assert roles.get("0035") == "bootloader"
    assert roles.get("8053") == "runtime"


@pytest.mark.asyncio
async def test_fingerprint_requires_hub_port_path(app):
    from hil_controller.adapters.usb_fingerprint import (
        FingerprintError,
        UsbFingerprintAdapter,
    )

    fake_hub = FakeHub(sequence=[])
    adapter = UsbFingerprintAdapter(
        db_path=app.state._test_db,
        hub=fake_hub,
        scan_fn=fake_hub.scan,
    )
    with pytest.raises(FingerprintError):
        await adapter.learn(device_id="no-hub", job_id="x")


@pytest.mark.asyncio
async def test_fingerprint_releases_hub_lease_on_error(app):
    """Even if scan blows up, the exclusive_hub lease must be released."""
    from hil_controller.adapters.usb_fingerprint import UsbFingerprintAdapter
    from hil_controller.queue.leases import list_active

    class BrokenHub(FakeHub):
        async def all_off(self):
            raise RuntimeError("solenoid down")

    fake_hub = BrokenHub(sequence=[])
    adapter = UsbFingerprintAdapter(
        db_path=app.state._test_db,
        hub=fake_hub,
        scan_fn=fake_hub.scan,
    )
    with pytest.raises(RuntimeError):
        await adapter.learn(device_id="dev1", job_id="learn-bad")

    leases = await list_active(app.state._test_db)
    assert all(l["job_id"] != "learn-bad" for l in leases)


# -- REST endpoint -------------------------------------------------------


@pytest.mark.asyncio
async def test_learn_usb_endpoint_invokes_adapter(client, monkeypatch):
    """POST /v1/devices/{id}/learn-usb returns the rows captured by the adapter."""
    from hil_controller.api import devices as devices_api

    async def fake_learn(self, *, device_id, job_id=None, include_reset_cycle=False):
        return [
            {
                "id": 99,
                "device_id": device_id,
                "vid": "239a",
                "pid": "8053",
                "role": "runtime",
                "iserial": None,
                "description": "fake",
                "bcd_device": None,
                "first_seen_at": "now",
                "last_seen_at": "now",
                "learned_from_job": job_id,
                "source": "learn-job",
            }
        ]

    from hil_controller.adapters import usb_fingerprint as fp_mod

    monkeypatch.setattr(fp_mod.UsbFingerprintAdapter, "learn", fake_learn)

    r = await client.post("/v1/devices/dev1/learn-usb")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert body[0]["pid"] == "8053"


@pytest.mark.asyncio
async def test_learn_usb_endpoint_404_missing_device(client):
    r = await client.post("/v1/devices/does-not-exist/learn-usb")
    assert r.status_code == 404
