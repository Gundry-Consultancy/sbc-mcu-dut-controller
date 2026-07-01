"""Target matching by I2C-strand prerequisites in the host registry."""

from hil_controller.hosts.registry import HostRegistry

_QTPY = {"id": "qtpy", "host_id": "h1", "kind": "microcontroller", "model": "esp32-s3",
         "pool": "public", "capabilities": ["i2c"], "status": "available"}
_PLAIN = {"id": "plain", "host_id": "h1", "kind": "microcontroller", "model": "esp32-s3",
          "pool": "public", "capabilities": ["i2c"], "status": "available"}

_STRANDS = [
    {
        "id": "strand-air",
        "mux_aux": "mux1",
        "mux_group": "muxA",
        "components": [
            {"id": "c1", "capabilities": ["sensor:pm25", "air-quality"]},
            {"id": "c2", "capabilities": ["sensor:voc"]},
        ],
        "routes": [{"device": "qtpy", "channel": 0}],  # only qtpy is wired to the strand
    }
]


def _reg():
    reg = HostRegistry(topology_file="")
    reg._hosts = [{"id": "h1"}]
    reg._devices = [_QTPY, _PLAIN]
    reg._index_strands(_STRANDS)
    return reg


def test_strand_requirement_restricts_to_routed_device():
    reg = _reg()
    req = {"target": {"device": {"kind": "microcontroller"},
                      "requires": [{"kind": "i2c_strand", "capabilities": ["sensor:pm25"]}]}}
    host, device = reg.find_device_for_job(req)
    assert device["id"] == "qtpy"  # 'plain' has no route to a providing strand


def test_unprovided_capability_no_match():
    reg = _reg()
    req = {"target": {"device": {"kind": "microcontroller"},
                      "requires": [{"kind": "i2c_strand", "capabilities": ["sensor:co2"]}]}}
    assert reg.find_device_for_job(req) is None


def test_capabilities_may_span_components_of_one_strand():
    reg = _reg()
    req = {"target": {"device": {},
                      "requires": [{"kind": "i2c_strand",
                                    "capabilities": ["sensor:pm25", "sensor:voc"]}]}}
    host, device = reg.find_device_for_job(req)
    assert device["id"] == "qtpy"


def test_no_strand_requirement_matches_any():
    reg = _reg()
    req = {"target": {"device": {"kind": "microcontroller"}}}
    host, device = reg.find_device_for_job(req)
    assert device["id"] in {"qtpy", "plain"}


def test_strand_for_device_returns_channel():
    reg = _reg()
    route = reg.strand_for_device("qtpy", {"air-quality"})
    assert route["strand_id"] == "strand-air"
    assert route["channel"] == 0
    assert reg.strand_for_device("plain", {"air-quality"}) is None
