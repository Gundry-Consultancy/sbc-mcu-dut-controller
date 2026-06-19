"""M1.5 tests: hosts, devices, aux, topology, and resolve endpoints."""

import pytest

# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hosts_requires_auth(client):
    r = await client.get("/v1/hosts")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_hosts_list_empty_when_no_topology(authed_client):
    r = await authed_client.get("/v1/hosts")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_hosts_list_returns_seeded_hosts(seeded_client):
    r = await seeded_client.get("/v1/hosts")
    assert r.status_code == 200
    body = r.json()
    ids = {h["id"] for h in body}
    assert "fake-sbc-host" in ids
    assert "fake-mcu-host" in ids


@pytest.mark.asyncio
async def test_host_detail_returns_device_count(seeded_client):
    r = await seeded_client.get("/v1/hosts/fake-sbc-host")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "fake-sbc-host"
    assert body["device_count"] == 1
    assert len(body["devices"]) == 1
    assert body["devices"][0]["id"] == "fake-pi5-01"


@pytest.mark.asyncio
async def test_host_detail_unknown_returns_404(seeded_client):
    r = await seeded_client.get("/v1/hosts/no-such-host")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_devices_requires_auth(client):
    r = await client.get("/v1/devices")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_devices_list_returns_seeded_devices(seeded_client):
    r = await seeded_client.get("/v1/devices")
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()}
    assert "fake-pi5-01" in ids
    assert "fake-qtpy-01" in ids


@pytest.mark.asyncio
async def test_devices_filter_by_kind(seeded_client):
    r = await seeded_client.get("/v1/devices?kind=sbc")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "fake-pi5-01"


@pytest.mark.asyncio
async def test_devices_filter_by_pool(seeded_client):
    r = await seeded_client.get("/v1/devices?pool=wippersnapper-python")
    assert r.status_code == 200
    body = r.json()
    assert all(d["pool"] == "wippersnapper-python" for d in body)
    assert any(d["id"] == "fake-pi5-01" for d in body)


@pytest.mark.asyncio
async def test_devices_filter_by_capability(seeded_client):
    r = await seeded_client.get("/v1/devices?capability=spi")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "fake-qtpy-01"


@pytest.mark.asyncio
async def test_device_detail_includes_host_and_auxes(seeded_client):
    r = await seeded_client.get("/v1/devices/fake-qtpy-01")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "fake-qtpy-01"
    assert body["host"] is not None
    assert body["host"]["id"] == "fake-mcu-host"
    assert len(body["auxes"]) == 1
    assert body["auxes"][0]["id"] == "fake-oled-01"


@pytest.mark.asyncio
async def test_device_detail_unknown_returns_404(seeded_client):
    r = await seeded_client.get("/v1/devices/no-such-device")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Aux
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aux_requires_auth(client):
    r = await client.get("/v1/aux")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_aux_list_returns_seeded_auxes(seeded_client):
    r = await seeded_client.get("/v1/aux")
    assert r.status_code == 200
    ids = {a["id"] for a in r.json()}
    assert "fake-oled-01" in ids


@pytest.mark.asyncio
async def test_aux_detail_includes_connections(seeded_client):
    r = await seeded_client.get("/v1/aux/fake-oled-01")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "fake-oled-01"
    assert len(body["connections"]) == 1
    assert body["connections"][0]["device_id"] == "fake-qtpy-01"


@pytest.mark.asyncio
async def test_aux_detail_unknown_returns_404(seeded_client):
    r = await seeded_client.get("/v1/aux/no-such-aux")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topology_graph_requires_auth(client):
    r = await client.get("/v1/topology")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_topology_graph_returns_full_graph(seeded_client):
    r = await seeded_client.get("/v1/topology")
    assert r.status_code == 200
    body = r.json()
    assert "hosts" in body
    assert "devices" in body
    assert "auxes" in body
    assert "connections" in body
    assert "peripherals" in body
    assert len(body["hosts"]) == 2
    assert len(body["devices"]) == 2
    assert len(body["auxes"]) == 1
    assert len(body["peripherals"]) == 1


@pytest.mark.asyncio
async def test_topology_device_includes_peripheral_ids(seeded_client):
    r = await seeded_client.get("/v1/topology")
    assert r.status_code == 200
    body = r.json()
    qtpy = next(d for d in body["devices"] if d["id"] == "fake-qtpy-01")
    assert "peripheral_ids" in qtpy
    assert "fake-oled-periph-01" in qtpy["peripheral_ids"]


@pytest.mark.asyncio
async def test_topology_peripheral_fields(seeded_client):
    r = await seeded_client.get("/v1/topology")
    assert r.status_code == 200
    body = r.json()
    periph = next(p for p in body["peripherals"] if p["id"] == "fake-oled-periph-01")
    assert periph["kind"] == "display"
    assert periph["model"] == "OLED 128x32"
    assert periph["product_url"] == "https://adafru.it/2900"


@pytest.mark.asyncio
async def test_topology_resolve_finds_matching_device(seeded_client):
    r = await seeded_client.post(
        "/v1/topology/resolve",
        json={"device": {"kind": "sbc", "model": "pi5"}, "pool": "wippersnapper-python"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["device_id"] == "fake-pi5-01"


@pytest.mark.asyncio
async def test_topology_resolve_no_match_returns_409(seeded_client):
    r = await seeded_client.post(
        "/v1/topology/resolve",
        json={"device": {"kind": "sbc", "model": "pi-zero"}, "pool": "wippersnapper-python"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_topology_resolve_with_aux_requirement(seeded_client):
    r = await seeded_client.post(
        "/v1/topology/resolve",
        json={
            "device": {"kind": "microcontroller"},
            "pool": "public",
            "requires": [{"kind": "display", "capabilities": ["display:128x32"]}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["device_id"] == "fake-qtpy-01"
    assert len(body["candidates"][0]["aux_bindings"]) == 1


@pytest.mark.asyncio
async def test_topology_resolve_unsatisfied_aux_returns_409(seeded_client):
    r = await seeded_client.post(
        "/v1/topology/resolve",
        json={
            "device": {"kind": "microcontroller"},
            "pool": "public",
            "requires": [{"kind": "display", "capabilities": ["display:480x480"]}],
        },
    )
    assert r.status_code == 409
