"""Identity boundaries and safe discovery behavior with synthetic hardware only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timezone
from pathlib import Path

import httpx
import pytest

from hil_controller import twin_collectors as collectors
from hil_controller.twin_models import Observation, Slot

WHEN = datetime(2026, 1, 1, tzinfo=UTC)


def put(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def pi(root: Path) -> None:
    put(root, "sys/firmware/devicetree/base/model", "Raspberry Pi 4 Model B\x00")
    put(root, "sys/firmware/devicetree/base/serial-number", "0000000012345678\x00")


def usb(
    root: Path,
    busid: str,
    *,
    serial: str | None = None,
    vid: str = "239a",
    pid: str = "8144",
    product: str = "Example endpoint",
) -> Path:
    path = f"sys/bus/usb/devices/{busid}"
    for field, value in {"idVendor": vid, "idProduct": pid, "product": product}.items():
        put(root, f"{path}/{field}", value)
    if serial is not None:
        put(root, f"{path}/serial", serial)
    return root / path


def scan(root: Path, **kwargs):
    return collectors.collect_linux(
        root=root,
        host_id="fixture-host",
        scan_id="fixture-scan",
        observed_at=WHEN,
        architecture="aarch64",
        **kwargs,
    )


def test_pi_hardware_identity_and_explicit_self_binding(tmp_path):
    pi(tmp_path)
    rows = scan(
        tmp_path,
        self_slot=Slot(kind="host", id="fixture-host", expected_twin_id=None),
        expected_board_serial="0000000012345678",
    )
    identity = rows[0].identities[0]
    assert identity.kind == "board_serial"
    assert identity.owner == "board" and identity.unique and identity.confidence == "verified"
    assert rows[0].attributes["architecture"] == "aarch64"
    assert rows[0].slot.expected_twin_id is None
    assert Observation.model_validate(rows[0].model_dump(mode="json")).slot is not None


@pytest.mark.parametrize(
    "slot,serial",
    [
        (Slot(kind="host", id="fixture-host", expected_twin_id=None), "different"),
        (Slot(kind="host", id="another-host", expected_twin_id=None), "0000000012345678"),
        (Slot(kind="device", id="fixture-host", expected_twin_id=None), "0000000012345678"),
    ],
)
def test_rejects_unverified_or_nonself_binding(tmp_path, slot, serial):
    pi(tmp_path)
    with pytest.raises(collectors.CollectionError, match="self slot"):
        scan(tmp_path, self_slot=slot, expected_board_serial=serial)


def test_missing_serial_is_unavailable_not_address_identity(tmp_path):
    usb(tmp_path, "1-1", vid="05e3", pid="0610", product="USB Hub")
    rows = scan(tmp_path)
    assert rows[0].attributes["identity_status"] == "unavailable"
    endpoint = rows[1]
    assert endpoint.identities == [] and endpoint.slot is None
    assert endpoint.kind == "usb_endpoint"
    assert endpoint.attributes["identity_status"] == "unavailable"
    assert endpoint.ports[0].attributes["busid"] == "1-1"


def test_native_bridge_probe_and_two_ports_do_not_claim_mcuid(tmp_path):
    usb(tmp_path, "1-1.1", serial="SAME", product="Native USB")
    usb(tmp_path, "1-1.2", serial="SAME", product="Second port of same MCU")
    usb(tmp_path, "1-1.3", serial="BRIDGE", vid="10c4", pid="ea60", product="UART Bridge")
    usb(tmp_path, "1-1.4", serial="PROBE", vid="2e8a", pid="000c", product="Debug Probe")
    endpoints = scan(tmp_path)[1:]
    assert len(endpoints) == 4
    assert all(row.kind == "usb_endpoint" and row.slot is None for row in endpoints)
    assert all(row.attributes["physical_device_association"] == "unverified" for row in endpoints)
    assert all(
        identity.owner == "endpoint"
        and identity.component == "usb"
        and identity.kind == "endpoint_serial"
        and not identity.unique
        for row in endpoints
        for identity in row.identities
    )
    assert endpoints[0].observation_id != endpoints[1].observation_id


def test_os_root_hubs_and_usb_interfaces_are_not_assets(tmp_path, monkeypatch):
    usb(tmp_path, "usb1", serial="xhci-controller", vid="1d6b", pid="0002")
    usb(tmp_path, "1-1", serial="REAL")
    usb_root = tmp_path / "sys/bus/usb/devices"
    original = Path.iterdir

    def entries(path):
        values = list(original(path))
        return iter(values + [path / "1-1:1.0"] if path == usb_root else values)

    monkeypatch.setattr(Path, "iterdir", entries)
    assert [row.identities[0].value for row in scan(tmp_path)[1:]] == ["REAL"]


def test_other_soc_serial_has_explicit_unverified_scope(tmp_path):
    put(tmp_path, "sys/devices/soc0/serial_number", "1234567")
    row = scan(tmp_path)[0]
    assert row.identities[0].owner == "chip"
    assert row.identities[0].component == "soc"
    assert not row.identities[0].unique
    assert row.slot is None


def test_observation_id_is_stable_per_scan_host_source_locator(tmp_path):
    usb(tmp_path, "1-1", serial="USB-123")
    first = scan(tmp_path)
    second = scan(tmp_path)
    assert first == second
    later = collectors.collect_linux(
        root=tmp_path,
        host_id="fixture-host",
        scan_id="new-scan",
        observed_at=WHEN,
        architecture="aarch64",
    )
    assert all(a.observation_id != b.observation_id for a, b in zip(first, later, strict=True))


def test_tty_link_matches_sysfs_ancestry_not_serial_substring(tmp_path, monkeypatch):
    endpoint = usb(tmp_path, "1-1", serial="REPEATED")
    another = usb(tmp_path, "1-2", serial="REPEATED")
    put(tmp_path, "dev/serial/by-id/usb-REPEATED-if00", "")
    put(tmp_path, "dev/ttyACM0", "")
    real_resolve = Path.resolve

    def resolve(path, strict=False):
        if path == tmp_path / "dev/serial/by-id/usb-REPEATED-if00":
            return tmp_path / "dev/ttyACM0"
        if path == tmp_path / "sys/class/tty/ttyACM0/device":
            return real_resolve(endpoint) / "1-1:1.0"
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert collectors._serial_links(tmp_path, endpoint) == {
        "/dev/serial/by-id/usb-REPEATED-if00": "/dev/ttyACM0"
    }
    assert collectors._serial_links(tmp_path, another) == {}


def bridge_device(**updates):
    return {
        "busid": "1-1.1",
        "vid": "239a",
        "pid": "0x8144",
        "serial": "EXAMPLE",
        "product": "Example USB device",
        "manufacturer": "Example",
        **updates,
    }


def normalize(payload):
    return collectors.normalize_bridge_devices(
        payload, host_id="fixture-bridge", scan_id="scan-1", observed_at=WHEN
    )


def test_bridge_schema_vid_normalization_and_no_unknown_fields():
    row = normalize(
        {"devices": [bridge_device(vid=0x239A, password="DO-NOT-COPY")], "api_token": "DO-NOT-COPY"}
    )[0]
    assert row.ports[0].attributes["vid"] == "239a"
    assert row.ports[0].attributes["pid"] == "8144"
    assert row.identities[0].owner == "endpoint" and not row.identities[0].unique
    assert "DO-NOT-COPY" not in row.model_dump_json()
    assert row.slot is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ok": False, "devices": []},
        {"devices": "not a list"},
        {"devices": [bridge_device(), bridge_device()]},
        {"devices": [bridge_device(busid="../../wrong")]},
        {"devices": [bridge_device(vid=True)]},
        {"devices": [bridge_device(serial=12345)]},
    ],
)
def test_malformed_bridge_payload_is_error_not_empty_success(payload):
    with pytest.raises(collectors.CollectionError):
        normalize(payload)


def test_bridge_only_gets_explicit_inventory_with_environment_auth(monkeypatch):
    monkeypatch.setenv("FIXTURE_BRIDGE_TOKEN", "private-test-token")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"devices": [bridge_device()]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = collectors.collect_bridge(
            "https://example.test",
            host_id="fixture-bridge",
            scan_id="scan-1",
            observed_at=WHEN,
            token_env="FIXTURE_BRIDGE_TOKEN",
            client=client,
        )
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/usb/devices"
    assert requests[0].headers["Authorization"] == "Bearer private-test-token"
    assert "private-test-token" not in rows[0].model_dump_json()


@pytest.mark.parametrize("status", [302, 401, 503])
def test_bridge_errors_never_echo_response_secrets_or_follow_redirect(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-password", headers={"Location": "/elsewhere"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(collectors.CollectionError) as error:
            collectors.collect_bridge(
                "https://example.test",
                host_id="fixture-bridge",
                scan_id="scan-1",
                observed_at=WHEN,
                client=client,
            )
    assert "private-password" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test",
        "https://example.test?token=x",
        "file:///etc/passwd",
        "https://example.test#token",
    ],
)
def test_rejects_credentials_in_bridge_url(url):
    with pytest.raises(collectors.CollectionError, match="without credentials"):
        collectors.collect_bridge(url, host_id="fixture-bridge", scan_id="scan-1", observed_at=WHEN)


def test_cli_preserves_required_null_slot_occupant(tmp_path, monkeypatch, capsys):
    pi(tmp_path)
    rows = scan(
        tmp_path,
        self_slot=Slot(kind="host", id="fixture-host", expected_twin_id=None),
        expected_board_serial="0000000012345678",
    )
    monkeypatch.setattr(collectors, "collect_linux", lambda **kwargs: rows)
    assert collectors.main(["--host-id", "fixture-host", "--scan-id", "capture", "linux"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "expected_twin_id" in data[0]["slot"]
    assert data[0]["slot"]["expected_twin_id"] is None
    assert Observation.model_validate(data[0]) == rows[0]


def test_virtual_bridge_device_is_unresolved_endpoint_not_physical_board():
    row = normalize({"devices": [bridge_device(virtual=True)]})[0]
    assert row.kind == "usb_endpoint"
    assert row.attributes["physical_device_association"] == "unverified"
    assert row.ports[0].attributes["virtual"] is True
    assert row.slot is None
    assert not row.identities[0].unique


@pytest.mark.parametrize("serial", ["0000000000000000", "ffffffffffffffff", "FFFFFFFFFFFFFFFF"])
def test_pi_placeholder_serial_is_not_an_identity_or_bindable(tmp_path, serial):
    pi(tmp_path)
    put(tmp_path, "sys/firmware/devicetree/base/serial-number", serial)
    result = scan(tmp_path)[0]
    assert result.identities == []
    assert result.attributes["identity_status"] == "unavailable"
    assert "placeholder" in result.attributes["identity_reason"]
    assert result.attributes["invalid_board_serial"] == serial
    with pytest.raises(collectors.CollectionError, match="self slot"):
        scan(
            tmp_path,
            self_slot=Slot(kind="host", id="fixture-host", expected_twin_id=None),
            expected_board_serial=serial,
        )


def test_pi_hex_case_is_canonical_across_captures_and_binding(tmp_path):
    pi(tmp_path)
    put(tmp_path, "sys/firmware/devicetree/base/serial-number", "00000000ABCDEF12")
    upper = scan(
        tmp_path,
        self_slot=Slot(kind="host", id="fixture-host", expected_twin_id=None),
        expected_board_serial="00000000ABCDEF12",
    )[0]
    put(tmp_path, "sys/firmware/devicetree/base/serial-number", "00000000abcdef12")
    lower = scan(tmp_path)[0]
    assert upper.identities[0].value == lower.identities[0].value == "00000000abcdef12"
    assert upper.identities[0].unique is True
