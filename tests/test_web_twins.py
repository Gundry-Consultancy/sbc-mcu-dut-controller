"""Physical Twins web UI: list/detail/slots, preview/apply, bridge collection, auth."""

from __future__ import annotations

import html
import json

import httpx
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hil_controller.db.connection import get_db
from hil_controller.web import twins_views
from tests.fake_esp_bridge import BASE_URL, FakeBridge
from tests.test_auth_policy import _mint_token
from tests.test_twins import observation

TOKEN = "test-token-for-ci"
BRIDGE_HOST = "bridge-s31"
BRIDGE_ENV = "HIL_TEST_TWIN_BRIDGE_TOKEN"
BRIDGE_SECRET = "bridge-secret-value-never-rendered"


@pytest_asyncio.fixture
async def ui(seeded_app):
    """(web client with cookie, API client with bearer, app) on a seeded topology."""
    async with get_db(seeded_app.state.db_path) as db:
        await db.execute(
            "INSERT INTO hosts(id, role, addr, transport, api_url, token_env) "
            "VALUES (?,?,?,?,?,?)",
            (BRIDGE_HOST, "microcontroller-fleet", "usbip-test.local", "esp-usbip-bridge",
             BASE_URL, BRIDGE_ENV),
        )
        await db.execute(
            "INSERT INTO hosts(id, role, addr, transport, ssh_user) VALUES (?,?,?,?,?)",
            ("lab-pi", "sbc-fleet", "10.0.0.5", "ssh", "pi"),
        )
        await db.commit()
    transport = ASGITransport(app=seeded_app)
    async with (
        AsyncClient(
            transport=transport, base_url="http://test", cookies={"hil_token": TOKEN}
        ) as web,
        AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as api,
    ):
        yield web, api, seeded_app


async def _create(api, body) -> str:
    response = await api.post("/v1/twins/observations", json=body)
    assert response.status_code == 200, response.text
    return response.json()["twin_id"]


async def _twin_count(api) -> int:
    return len((await api.get("/v1/twins")).json()["twins"])


def _sensor(name: str = "sensor-1", **changes):
    body = observation(
        name,
        kind="sensor",
        label="Synthetic sensor",
        identities=[],
        ports=[{"id": "i2c", "kind": "i2c"}],
        attributes={"model": "synthetic"},
    )
    body.update(changes)
    return body


# ---------------------------------------------------------------------------
# List / detail / slots
# ---------------------------------------------------------------------------


async def test_requires_login(seeded_app):
    async with AsyncClient(transport=ASGITransport(app=seeded_app), base_url="http://t") as c:
        r = await c.get("/ui/twins", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


async def test_empty_state_explains_how_to_populate(ui):
    web, _, _ = ui
    r = await web.get("/ui/twins")
    assert r.status_code == 200
    assert "No physical twins recorded yet" in r.text
    assert "hil-twins-import --db controller.db --input observations.json" in r.text
    assert "Collect from bridge" in r.text
    assert 'action="/ui/twins/observations/preview"' in r.text


async def test_list_shows_counts_slots_and_filters(ui):
    web, api, _ = ui
    slot = {"kind": "device", "id": "fake-qtpy-01", "expected_twin_id": None}
    board = await _create(api, observation(slot=slot))
    sensor = await _create(api, _sensor())
    r = await web.get("/ui/twins")
    assert r.status_code == 200
    assert board in r.text and sensor in r.text
    assert "device:fake-qtpy-01" in r.text
    assert "test:synthetic" in r.text  # last observation source
    row = r.text.split(f'id="twin-row-{board}"')[1].split("</tr>")[0]
    assert "<td>1</td>" in row  # one identity, one port

    by_kind = await web.get("/ui/twins", params={"kind": "sensor"})
    assert sensor in by_kind.text and board not in by_kind.text
    by_identity = await web.get("/ui/twins", params={"q": "FAKE-001"})
    assert board in by_identity.text and sensor not in by_identity.text
    by_slot = await web.get("/ui/twins", params={"q": "qtpy"})
    assert board in by_slot.text and sensor not in by_slot.text
    wildcard = await web.get("/ui/twins", params={"q": "%"})
    assert "No physical twins match" in wildcard.text


async def test_detail_renders_everything(ui):
    web, api, _ = ui
    slot = {"kind": "device", "id": "fake-qtpy-01", "expected_twin_id": None}
    board = await _create(api, observation(slot=slot))
    later = observation("scan-2", observed_at="2026-01-02T00:00:00Z", twin_id=board,
                        label="Renamed board", identities=[], ports=[])
    await _create(api, later)
    r = await web.get(f"/ui/twins/{board}")
    assert r.status_code == 200
    text = r.text
    assert "Renamed board" in text
    assert "FAKE-001" in text and "Synthetic silicon-register fixture" in text
    assert "usb-native" in text and "&#34;busid&#34;: &#34;1-2&#34;" in text
    assert 'href="/ui/devices/fake-qtpy-01/form"' in text
    observations = text.split("newest first</span>")[1]
    assert observations.index("scan-2") < observations.index("scan-1")
    assert "<details><summary class=\"form-hint\">payload</summary>" in text

    missing = await web.get("/ui/twins/does-not-exist")
    assert missing.status_code == 404
    assert "Physical twin not found" in missing.text


async def test_slots_page_highlights_unassigned(ui):
    web, api, _ = ui
    slot = {"kind": "device", "id": "fake-qtpy-01", "expected_twin_id": None}
    board = await _create(api, observation(slot=slot))
    r = await web.get("/ui/twins/slots", params={"kind": "device"})
    assert r.status_code == 200
    unassigned = r.text.split('id="unassigned"')[1].split("Current occupants")[0]
    assert "fake-pi5-01" in unassigned and "fake-qtpy-01" not in unassigned
    assert "1 of 2 device slot(s) have no physical twin" in r.text
    assert f'href="/ui/twins/{board}"' in r.text
    assert "UNASSIGNED" in r.text

    hosts = await web.get("/ui/twins/slots", params={"kind": "host"})
    assert hosts.status_code == 200 and 'href="/ui/hosts"' in hosts.text
    bad = await web.get("/ui/twins/slots", params={"kind": "bogus"})
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Preview / apply
# ---------------------------------------------------------------------------


async def test_preview_does_not_write(ui):
    web, api, _ = ui
    r = await web.post(
        "/ui/twins/observations/preview",
        data={"observations_json": json.dumps(observation())},
    )
    assert r.status_code == 200
    assert "are valid against the current database" in r.text
    assert ">create</span>" in r.text
    assert 'action="/ui/twins/observations/apply"' in r.text
    assert await _twin_count(api) == 0

    uploaded = await web.post(
        "/ui/twins/observations/preview",
        data={"observations_json": ""},
        files={"file": ("obs.json", json.dumps([observation(), _sensor()]), "application/json")},
    )
    assert uploaded.status_code == 200
    assert "All 2 observation(s) are valid" in uploaded.text
    assert await _twin_count(api) == 0


async def test_apply_writes_and_replays(ui):
    web, api, _ = ui
    payload = json.dumps([observation(), _sensor()])
    r = await web.post("/ui/twins/observations/apply", data={"observations_json": payload})
    assert r.status_code == 200, r.text
    assert "Committed 2 observation(s)" in r.text
    twins = (await api.get("/v1/twins")).json()["twins"]
    assert len(twins) == 2
    for twin in twins:
        assert f'href="/ui/twins/{twin["id"]}"' in r.text
    detail = (await api.get(f"/v1/twins/{twins[0]['id']}")).json()
    assert detail["observations"][0]["actor"] == "static"

    again = await web.post("/ui/twins/observations/preview", data={"observations_json": payload})
    assert again.text.count(">replay</span>") == 2


async def test_conflict_rendering_adopt_and_refused_apply(ui):
    web, api, _ = ui
    twin = await _create(api, observation())
    later = observation("scan-2", observed_at="2026-01-02T00:00:00Z")
    r = await web.post(
        "/ui/twins/observations/preview", data={"observations_json": json.dumps(later)}
    )
    assert r.status_code == 200
    assert "HTTP 409" in r.text
    assert "Verified identity already known; preview and choose explicit twin_id" in r.text
    assert f'href="/ui/twins/{twin}"' in r.text  # candidate
    assert "Use this twin" in r.text
    assert 'action="/ui/twins/observations/apply"' not in r.text

    adopted = await web.post(
        "/ui/twins/observations/preview",
        data={"observations_json": json.dumps(later), "adopt_index": "0", "adopt_twin_id": twin},
    )
    assert "are valid against the current database" in adopted.text
    assert ">update</span>" in adopted.text

    # Applying the unreviewed conflicting payload directly is refused, nothing written.
    refused = await web.post(
        "/ui/twins/observations/apply", data={"observations_json": json.dumps([_sensor(), later])}
    )
    assert refused.status_code == 409
    assert "Apply refused (HTTP 409); nothing was written." in refused.text
    assert "observation[1] scan-2" in refused.text
    assert await _twin_count(api) == 1


async def test_slot_compare_and_swap_conflict_is_shown(ui):
    web, api, _ = ui
    slot = {"kind": "device", "id": "fake-qtpy-01", "expected_twin_id": None}
    await _create(api, observation(slot=slot))
    stale = _sensor(slot=slot)
    r = await web.post(
        "/ui/twins/observations/preview", data={"observations_json": json.dumps(stale)}
    )
    assert "Slot occupant changed; refresh expected_twin_id" in r.text
    missing_slot = _sensor(slot={"kind": "device", "id": "nope", "expected_twin_id": None})
    r = await web.post(
        "/ui/twins/observations/preview", data={"observations_json": json.dumps(missing_slot)}
    )
    assert "HTTP 422" in r.text and "Logical slot does not exist" in r.text


async def test_invalid_input_returns_422_with_details(ui):
    web, api, _ = ui
    bad_json = await web.post("/ui/twins/observations/preview", data={"observations_json": "{"})
    assert bad_json.status_code == 422 and "Not valid JSON" in bad_json.text
    body = observation()
    del body["kind"]
    schema = await web.post(
        "/ui/twins/observations/apply", data={"observations_json": json.dumps(body)}
    )
    assert schema.status_code == 422
    assert "new twins require kind and label" in schema.text
    batch = await web.post(
        "/ui/twins/observations/preview",
        data={"observations_json": json.dumps({"observations": [], "connections": [{}]})},
    )
    assert batch.status_code == 422 and "hil-twins-import" in batch.text
    assert await _twin_count(api) == 0


# ---------------------------------------------------------------------------
# Bridge collection / Linux command
# ---------------------------------------------------------------------------


def _fake_bridge_client(monkeypatch, fake: FakeBridge, seen: list[httpx.Request]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fake.handle(request)

    monkeypatch.setattr(
        twins_views,
        "_bridge_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


async def test_bridge_collect_feeds_preview_never_applies(ui, monkeypatch):
    web, api, _ = ui
    monkeypatch.setenv(BRIDGE_ENV, BRIDGE_SECRET)
    seen: list[httpx.Request] = []
    _fake_bridge_client(monkeypatch, FakeBridge(), seen)

    page = await web.get("/ui/twins")
    assert BRIDGE_ENV in page.text and ">set</span>" in page.text
    assert BRIDGE_SECRET not in page.text

    r = await web.post(f"/ui/twins/collect/bridge/{BRIDGE_HOST}")
    assert r.status_code == 200, r.text
    assert "Collected from esp-usbip-bridge host bridge-s31" in r.text
    assert "usb_endpoint" in r.text and "QT Py ESP32-S3" in r.text
    assert ">create</span>" in r.text
    assert 'action="/ui/twins/observations/apply"' in r.text
    assert BRIDGE_SECRET not in r.text
    assert [str(q.url) for q in seen] == [f"{BASE_URL}/api/usb/devices"]
    assert seen[0].headers["Authorization"] == f"Bearer {BRIDGE_SECRET}"
    assert await _twin_count(api) == 0  # never auto-applied

    # The previewed payload applies through the normal flow.
    normalized = r.text.split('id="apply-form"')[1].split('value="')[1].split('">')[0]


    applied = await web.post(
        "/ui/twins/observations/apply", data={"observations_json": html.unescape(normalized)}
    )
    assert applied.status_code == 200 and "Committed 1 observation(s)" in applied.text
    assert BRIDGE_SECRET not in applied.text
    assert await _twin_count(api) == 1
    twin_id = (await api.get("/v1/twins")).json()["twins"][0]["id"]
    detail = await web.get(f"/ui/twins/{twin_id}")
    assert BRIDGE_SECRET not in detail.text


async def test_twin_pages_render_session_token_only_in_base_hx_headers(ui):
    """base.html (not owned here) puts the session token in hx-headers; nothing else may."""
    web, api, _ = ui
    twin = await _create(api, observation())
    pages = [
        await web.get("/ui/twins"),
        await web.get(f"/ui/twins/{twin}"),
        await web.get("/ui/twins/slots"),
        await web.post(
            "/ui/twins/observations/preview",
            data={"observations_json": json.dumps(_sensor())},
        ),
    ]
    for page in pages:
        assert page.status_code == 200
        assert page.text.count(TOKEN) <= 1
        if TOKEN in page.text:
            assert f'"Authorization": "Bearer {TOKEN}"' in page.text


async def test_bridge_collect_unset_token_and_failure(ui, monkeypatch):
    web, _, _ = ui
    monkeypatch.delenv(BRIDGE_ENV, raising=False)
    fake = FakeBridge()
    seen: list[httpx.Request] = []
    _fake_bridge_client(monkeypatch, fake, seen)
    r = await web.post(f"/ui/twins/collect/bridge/{BRIDGE_HOST}")
    assert r.status_code == 200
    assert "is not set in the controller environment" in r.text
    assert "Authorization" not in seen[0].headers

    fake.fail_status = 500
    failed = await web.post(f"/ui/twins/collect/bridge/{BRIDGE_HOST}")
    assert failed.status_code == 502
    assert "bridge inventory request failed (HTTP 500)" in failed.text
    assert "forced failure" not in failed.text

    not_bridge = await web.post("/ui/twins/collect/bridge/lab-pi")
    assert not_bridge.status_code == 422
    unknown = await web.post("/ui/twins/collect/bridge/nope")
    assert unknown.status_code == 404


async def test_linux_hosts_show_collector_command(ui):
    web, _, _ = ui
    r = await web.get("/ui/twins")
    assert "python -m hil_controller.twin_collectors --host-id lab-pi --scan-id inventory-" in (
        r.text
    )
    assert "--output observations.json linux" in r.text
    assert "ssh pi@10.0.0.5" in r.text


# ---------------------------------------------------------------------------
# Close connection
# ---------------------------------------------------------------------------


async def _connected_pair(api) -> tuple[str, str]:
    board = await _create(api, observation())
    sensor = await _create(api, _sensor())
    connection = {
        "id": "wire-1",
        "from_twin_id": board,
        "from_port": "usb-native",
        "to_twin_id": sensor,
        "to_port": "i2c",
        "kind": "i2c",
        "source": "test:synthetic",
        "evidence": "Synthetic wiring",
        "started_at": "2026-01-01T00:00:00Z",
    }
    response = await api.post("/v1/twins/connections", json=connection)
    assert response.status_code == 200, response.text
    return board, sensor


async def test_close_connection_and_validation(ui):
    web, api, _ = ui
    board, sensor = await _connected_pair(api)
    page = await web.get(f"/ui/twins/{sensor}")
    assert "wire-1" in page.text and "&larr; in" in page.text
    assert f'action="/ui/twins/{sensor}/connections/wire-1/close"' in page.text

    early = await web.post(
        f"/ui/twins/{board}/connections/wire-1/close",
        data={"ended_at": "2025-01-01T00:00:00Z", "source": "op", "reason": "moved"},
    )
    assert early.status_code == 422
    assert "Connection cannot end before it starts" in early.text
    naive = await web.post(
        f"/ui/twins/{board}/connections/wire-1/close",
        data={"ended_at": "2026-02-01T00:00:00", "source": "op", "reason": "moved"},
    )
    assert naive.status_code == 422 and "ended_at" in naive.text

    close = {"ended_at": "2026-02-01T00:00:00Z", "source": "op", "reason": "cable moved"}
    r = await web.post(
        f"/ui/twins/{board}/connections/wire-1/close", data=close, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/ui/twins/{board}?closed=wire-1"
    detail = (await api.get(f"/v1/twins/{board}")).json()
    assert detail["connections"][0]["ended_at"] == "2026-02-01T00:00:00+00:00"
    assert detail["connections"][0]["close_reason"] == "cable moved"
    shown = await web.get(r.headers["location"])
    assert "Connection wire-1 closed." in shown.text and "cable moved" in shown.text
    assert 'action="/ui/twins/' not in shown.text.split('id="connections"')[1]

    repeat = await web.post(
        f"/ui/twins/{board}/connections/wire-1/close", data=close, follow_redirects=False
    )
    assert repeat.status_code == 303  # exact repeat is idempotent
    different = await web.post(
        f"/ui/twins/{board}/connections/wire-1/close", data={**close, "reason": "other"}
    )
    assert different.status_code == 409
    assert "already closed with different evidence" in different.text
    unknown = await web.post(f"/ui/twins/{board}/connections/nope/close", data=close)
    assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


async def _web_as(app, token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies={"hil_token": token}
    )


async def test_partial_pool_or_missing_capability_gets_explanatory_403(ui):
    _, api, app = ui
    board, _ = await _connected_pair(api)
    db_path = app.state.db_path
    partial = await _mint_token(db_path, allowed_pools=["public"], capabilities=["inventory:write"])
    no_cap = await _mint_token(db_path, allowed_pools=["*"], capabilities=[])
    reader = await _mint_token(db_path, allowed_pools=["*"], capabilities=["inventory:read"])

    async with await _web_as(app, partial) as c:
        r = await c.get("/ui/twins")
        assert r.status_code == 403
        assert "access to all pools" in r.text and board not in r.text
    async with await _web_as(app, no_cap) as c:
        r = await c.get(f"/ui/twins/{board}")
        assert r.status_code == 403
        assert "capability inventory:read (or inventory:write)" in r.text
        assert "FAKE-001" not in r.text
    async with await _web_as(app, reader) as c:
        listing = await c.get("/ui/twins")
        assert listing.status_code == 200 and board in listing.text
        assert "Collect from bridge" not in listing.text  # write-only action hidden
        preview = await c.post(
            "/ui/twins/observations/preview",
            data={"observations_json": json.dumps(_sensor("s-2"))},
        )
        assert preview.status_code == 200
        for path, data in [
            ("/ui/twins/observations/apply", {"observations_json": json.dumps(_sensor("s-3"))}),
            (f"/ui/twins/collect/bridge/{BRIDGE_HOST}", {}),
            (
                f"/ui/twins/{board}/connections/wire-1/close",
                {"ended_at": "2026-02-01T00:00:00Z", "source": "op", "reason": "x"},
            ),
        ]:
            denied = await c.post(path, data=data)
            assert denied.status_code == 403, path
            assert "capability inventory:write" in denied.text
    assert await _twin_count(api) == 2
    assert (await api.get(f"/v1/twins/{board}")).json()["connections"][0]["ended_at"] is None
