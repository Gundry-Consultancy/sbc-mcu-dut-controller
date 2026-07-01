"""The select_i2c_strand bench stage: resolve a strand's route + drive the mux."""

import pytest

from hil_controller.adapters import bench_stages
from hil_controller.adapters.bench_stages import (
    BenchContext,
    StageError,
    _stage_select_i2c_strand,
)
from hil_controller.db.connection import init_db
from hil_controller.topology.seeder import seed_topology

EXAMPLE = "deploy/topology.strands.example.yaml"


class _FakeMux:
    """Records constructor + select calls; swapped in for AnalogMuxAdapter."""

    base = None
    calls: list = []

    def __init__(self, base_url, token=None):
        _FakeMux.base = base_url
        _FakeMux.token = token

    async def select(self, group, channel):
        _FakeMux.calls.append((group, channel))
        return {"active": f"{group}:ch{channel}"}

    async def isolate(self):
        _FakeMux.calls.append(("isolate",))
        return {}


def _ctx(db, device_id):
    return BenchContext(
        dut_transport=object(),
        hub_transport=object(),
        flash_serial_port="",
        device={"id": device_id},
        db_path=db,
    )


async def _seed(tmp_path):
    db = str(tmp_path / "s.db")
    await init_db(db)
    await seed_topology(db, EXAMPLE)
    return db


async def test_resolves_channel_from_db_route(tmp_path, monkeypatch):
    db = await _seed(tmp_path)
    monkeypatch.setattr(bench_stages, "AnalogMuxAdapter", _FakeMux)
    _FakeMux.calls = []
    ctx = _ctx(db, "mcu-lilygo-tdisplay-s3-hil006")  # routed on channel 2
    await _stage_select_i2c_strand(
        {"type": "select_i2c_strand", "strand_id": "strand-hil006-air"}, ctx
    )
    assert _FakeMux.calls == [("muxA", 2)]
    assert _FakeMux.base == "http://192.168.1.155:8080"


async def test_pi_self_dut_routes_to_channel_3(tmp_path, monkeypatch):
    db = await _seed(tmp_path)
    monkeypatch.setattr(bench_stages, "AnalogMuxAdapter", _FakeMux)
    _FakeMux.calls = []
    ctx = _ctx(db, "sbc-rpi-hil006-self")
    await _stage_select_i2c_strand({"strand_id": "strand-hil006-air"}, ctx)
    assert _FakeMux.calls == [("muxA", 3)]


async def test_no_route_for_device_raises(tmp_path, monkeypatch):
    db = await _seed(tmp_path)
    monkeypatch.setattr(bench_stages, "AnalogMuxAdapter", _FakeMux)
    ctx = _ctx(db, "some-unrouted-device")
    with pytest.raises(StageError):
        await _stage_select_i2c_strand({"strand_id": "strand-hil006-air"}, ctx)


async def test_explicit_params_skip_db(monkeypatch):
    monkeypatch.setattr(bench_stages, "AnalogMuxAdapter", _FakeMux)
    _FakeMux.calls = []
    ctx = BenchContext(
        dut_transport=object(), hub_transport=object(), flash_serial_port="", device={}, db_path=""
    )
    await _stage_select_i2c_strand(
        {"base_url": "http://x:8080", "group": "muxB", "channel": 3}, ctx
    )
    assert _FakeMux.calls == [("muxB", 3)]
