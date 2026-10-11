"""Read-only, provenance-preserving identity collectors; never bind a USB port to a DUT."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue, ValidationError

from hil_controller.twin_models import Identity, Observation, Port, Slot


class CollectionError(ValueError):
    """An inventory cannot be collected safely or interpreted unambiguously."""


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip("\x00\r\n ") or None
    except (OSError, UnicodeError):
        return None


def _observation_id(scan_id: str, host_id: str, source: str, locator: str) -> str:
    if not scan_id.strip() or not host_id.strip():
        raise CollectionError("scan_id and host_id must be non-empty")
    key = json.dumps([scan_id, host_id, source, locator], separators=(",", ":"))
    return "discovery-" + hashlib.sha256(key.encode()).hexdigest()


def _hex_id(value: object) -> str:
    if isinstance(value, bool):
        raise CollectionError("USB VID/PID must be 16-bit identifiers")
    if isinstance(value, int) and 0 <= value <= 65535:
        return f"{value:04x}"
    if isinstance(value, str) and re.fullmatch(r"(?:0x)?[0-9a-fA-F]{1,4}", value):
        return f"{int(value, 16):04x}"
    raise CollectionError("USB VID/PID must be 16-bit identifiers")


def _endpoint(
    *,
    scan_id: str,
    host_id: str,
    source: str,
    observed_at: datetime,
    busid: str,
    vid: str,
    pid: str,
    serial: str | None,
    model: str | None,
    manufacturer: str | None,
    attributes: dict[str, JsonValue],
    pool: str,
) -> Observation:
    identities = []
    if serial:
        identities.append(
            Identity(
                namespace=f"usb:{vid}:{pid}:iserial",
                kind="endpoint_serial",
                value=serial,
                owner="endpoint",
                component="usb",
                unique=False,
                confidence="reported",
                evidence=f"{source}: USB iSerial at host {host_id}, bus {busid}; "
                "identifies this endpoint only; silicon derivation and uniqueness not verified",
            )
        )
    return Observation(
        observation_id=_observation_id(scan_id, host_id, source, busid),
        source=source,
        observed_at=observed_at,
        kind="usb_endpoint",
        label=(model or f"USB {vid}:{pid}")[:256],
        pool=pool,
        identities=identities,
        attributes={
            "model": model,
            "manufacturer": manufacturer,
            "host_id": host_id,
            "identity_status": "reported" if serial else "unavailable",
            "identity_reason": "USB iSerial reported" if serial else "USB iSerial not readable",
            "physical_device_association": "unverified",
            "scan_id": scan_id,
        },
        ports=[
            Port(
                id="usb",
                kind="usb",
                attributes={
                    "host_id": host_id,
                    "busid": busid,
                    "vid": vid,
                    "pid": pid,
                    "iserial": serial,
                    **attributes,
                },
            )
        ],
    )


def _serial_links(root: Path, usb: Path) -> dict[str, JsonValue]:
    """Match a tty's sysfs ancestry, never substring-match a serial or port name."""
    links: dict[str, JsonValue] = {}
    for directory in ("by-id", "by-path"):
        for link in sorted((root / "dev/serial" / directory).glob("*")):
            try:
                tty = link.resolve(strict=True)
                parent = (root / "sys/class/tty" / tty.name / "device").resolve(strict=True)
                if parent.is_relative_to(usb.resolve(strict=True)):
                    links[f"/dev/serial/{directory}/{link.name}"] = f"/dev/{tty.name}"
            except OSError:
                continue  # Unplugged between the USB and tty snapshots.
    return links


def collect_linux(
    *,
    host_id: str,
    scan_id: str,
    observed_at: datetime,
    root: Path = Path("/"),
    architecture: str | None = None,
    pool: str = "public",
    self_slot: Slot | None = None,
    expected_board_serial: str | None = None,
) -> list[Observation]:
    """Read local Linux sysfs. ``root``/``architecture`` allow offline fixture inspection.

    USB records represent endpoints, not inferred MCU boards. Nothing opens a tty,
    I2C device, USB control endpoint, or database. Only a explicitly confirmed SBC
    self slot can be included; every USB endpoint is left unbound.
    """
    if root == Path("/") and platform.system() != "Linux":
        raise CollectionError("local sysfs collection requires Linux")
    model = _read(root / "sys/firmware/devicetree/base/model")
    model = model or _read(root / "sys/class/dmi/id/product_name")
    board_serial = _read(root / "sys/firmware/devicetree/base/serial-number")
    placeholder_serial = bool(
        model
        and model.startswith("Raspberry Pi ")
        and board_serial
        and board_serial.lower() in ("0000000000000000", "ffffffffffffffff")
    )
    invalid_board_serial = board_serial if placeholder_serial else None
    if placeholder_serial:
        board_serial = None
    pi_serial = bool(
        model
        and model.startswith("Raspberry Pi ")
        and board_serial
        and re.fullmatch(r"[0-9a-fA-F]{16}", board_serial)
    )
    if pi_serial and board_serial is not None:
        board_serial = board_serial.lower()
    identities = []
    if board_serial:
        identities.append(
            Identity(
                namespace="raspberrypi:board-serial" if pi_serial else "devicetree:board-serial",
                kind="board_serial",
                value=board_serial,
                owner="board",
                component="board",
                unique=pi_serial,
                confidence="verified" if pi_serial else "reported",
                evidence="Read /sys/firmware/devicetree/base/serial-number. "
                + (
                    "Raspberry Pi documents its full 64-bit board serial: "
                    "https://www.raspberrypi.com/documentation/computers/raspberry-pi.html"
                    if pi_serial
                    else "Uniqueness semantics not verified for this board."
                ),
            )
        )
    soc_serial = _read(root / "sys/devices/soc0/serial_number")
    if soc_serial:
        identities.append(
            Identity(
                namespace="linux-soc:serial",
                kind="silicon_uid",
                value=soc_serial,
                owner="chip",
                component="soc",
                unique=False,
                confidence="reported",
                evidence="Read /sys/devices/soc0/serial_number; chip identity semantics and "
                "uniqueness are platform-dependent, not independently verified",
            )
        )
    if self_slot is not None and (
        self_slot.kind != "host"
        or self_slot.id != host_id
        or not pi_serial
        or (expected_board_serial or "").lower() != board_serial
    ):
        raise CollectionError(
            "self slot requires the explicit host ID and matching verified "
            "Raspberry Pi board serial"
        )
    usb_root = root / "sys/bus/usb/devices"
    result = [
        Observation(
            observation_id=_observation_id(scan_id, host_id, "linux-sysfs", "self"),
            source="linux-sysfs",
            observed_at=observed_at,
            kind="sbc",
            label=(model or "Linux host")[:256],
            pool=pool,
            identities=identities,
            attributes={
                "host_id": host_id,
                "model": model,
                "architecture": architecture or platform.machine(),
                "identity_status": "observed" if identities else "unavailable",
                "identity_reason": "hardware serial read"
                if identities
                else (
                    "Device-tree board serial is a placeholder"
                    if placeholder_serial
                    else "No readable device-tree board serial or SoC serial"
                ),
                "invalid_board_serial": invalid_board_serial,
                "usb_discovery_status": "available" if usb_root.is_dir() else "unavailable",
                "scan_id": scan_id,
            },
            slot=self_slot,
        )
    ]
    if not usb_root.is_dir():
        return result
    for usb in sorted(usb_root.iterdir()):
        # Interface directories and OS-generated root hubs are not physical asset identities.
        if ":" in usb.name or re.fullmatch(r"usb\d+", usb.name):
            continue
        vid, pid = _read(usb / "idVendor"), _read(usb / "idProduct")
        if vid is None or pid is None:
            continue  # No device descriptor, or it disappeared during observation.
        result.append(
            _endpoint(
                scan_id=scan_id,
                host_id=host_id,
                source="linux-sysfs-usb",
                observed_at=observed_at,
                busid=usb.name,
                vid=_hex_id(vid),
                pid=_hex_id(pid),
                serial=_read(usb / "serial"),
                model=_read(usb / "product"),
                manufacturer=_read(usb / "manufacturer"),
                pool=pool,
                attributes={
                    "sysfs_path": f"/sys/bus/usb/devices/{usb.name}",
                    "serial_links": _serial_links(root, usb),
                    "bcd_device": _read(usb / "bcdDevice"),
                    "device_class": _read(usb / "bDeviceClass"),
                    "busnum": _read(usb / "busnum"),
                    "devnum": _read(usb / "devnum"),
                },
            )
        )
    return result


def normalize_bridge_devices(
    payload: object,
    *,
    host_id: str,
    scan_id: str,
    observed_at: datetime,
    pool: str = "public",
) -> list[Observation]:
    """Normalize the explicit bridge's /api/usb/devices; copy only known descriptor fields."""
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise CollectionError("bridge inventory response is not a successful object")
    devices = payload.get("devices")
    if not isinstance(devices, list):
        raise CollectionError("bridge inventory response must contain a devices list")
    rows = []
    seen: set[str] = set()
    for device in devices:
        if not isinstance(device, dict):
            raise CollectionError("bridge device must be an object")
        busid = device.get("busid")
        if not isinstance(busid, str) or not re.fullmatch(r"\d+-\d+(?:\.\d+)*", busid):
            raise CollectionError("bridge device lacks a valid USB busid")
        if busid in seen:
            raise CollectionError("bridge inventory contains duplicate busids")
        seen.add(busid)
        for field in ("serial", "product", "manufacturer"):
            if device.get(field) is not None and not isinstance(device[field], str):
                raise CollectionError("bridge string descriptors must be strings or null")
        attrs: dict[str, JsonValue] = {}
        for field in (
            "device_class",
            "bcd_device",
            "speed",
            "speed_mbps",
            "num_interfaces",
            "virtual",
            "address",
            "hub",
            "port",
        ):
            value = device.get(field)
            if value is None or isinstance(value, (str, int, bool)):
                attrs[field] = value
        rows.append(
            _endpoint(
                scan_id=scan_id,
                host_id=host_id,
                source="esp-usbip-bridge:api-usb-devices",
                observed_at=observed_at,
                busid=busid,
                vid=_hex_id(device.get("vid")),
                pid=_hex_id(device.get("pid")),
                serial=(device.get("serial") or "").strip() or None,
                model=(device.get("product") or "").strip() or None,
                manufacturer=(device.get("manufacturer") or "").strip() or None,
                attributes=attrs,
                pool=pool,
            )
        )
    return sorted(rows, key=lambda row: str(row.ports[0].attributes["busid"]))


def collect_bridge(
    base_url: str,
    *,
    host_id: str,
    scan_id: str,
    observed_at: datetime,
    token_env: str | None = None,
    timeout_s: float = 10,
    pool: str = "public",
    client: httpx.Client | None = None,
) -> list[Observation]:
    """One GET to a user-supplied bridge; secrets only from an environment variable.

    Redirects and URL credentials are forbidden. Error bodies, tokens and URLs are
    never included in output/errors. There is no network scanning or power API call.
    """
    url = urlsplit(base_url)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise CollectionError("bridge URL must be HTTP(S), without credentials, query or fragment")
    headers = {}
    if token_env is not None:
        token = os.environ.get(token_env)
        if not token:
            raise CollectionError("the requested bridge token environment variable is empty")
        headers["Authorization"] = f"Bearer {token}"
    try:
        if client is None:
            with httpx.Client(trust_env=False) as owned:
                response = owned.get(
                    base_url.rstrip("/") + "/api/usb/devices",
                    headers=headers,
                    timeout=timeout_s,
                    follow_redirects=False,
                )
        else:
            response = client.get(
                base_url.rstrip("/") + "/api/usb/devices",
                headers=headers,
                timeout=timeout_s,
                follow_redirects=False,
            )
    except httpx.HTTPError:
        raise CollectionError("bridge inventory transport failed") from None
    if not 200 <= response.status_code < 300:
        raise CollectionError(f"bridge inventory request failed (HTTP {response.status_code})")
    try:
        payload: object = response.json()
    except ValueError:
        raise CollectionError("bridge inventory response is not valid JSON") from None
    return normalize_bridge_devices(
        payload, host_id=host_id, scan_id=scan_id, observed_at=observed_at, pool=pool
    )


def main(argv: list[str] | None = None) -> int:
    """Print observations for review/import. Never opens a controller database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-id", required=True, help="Explicit controller host ID")
    parser.add_argument("--scan-id", required=True, help="Unique ID for this capture")
    parser.add_argument("--observed-at", help="Timezone-aware ISO timestamp (default now)")
    parser.add_argument("--pool", default="public")
    parser.add_argument("--output", type=Path, help="JSON output file (default stdout)")
    sub = parser.add_subparsers(dest="mode", required=True)
    local = sub.add_parser("linux", help="Read this Linux host's sysfs")
    local.add_argument("--bind-host-slot", action="store_true")
    local.add_argument("--expected-board-serial")
    local.add_argument("--expected-twin-id", help="Current occupant for slot compare-and-swap")
    bridge = sub.add_parser("bridge", help="Read one explicit S31/ESP USB/IP bridge")
    bridge.add_argument("--url", required=True)
    bridge.add_argument("--token-env", help="Environment variable name; never a token value")
    args = parser.parse_args(argv)
    try:
        when = datetime.fromisoformat(args.observed_at) if args.observed_at else datetime.now(UTC)
        if when.tzinfo is None:
            raise CollectionError("observed-at must include a timezone")
        kwargs: dict[str, Any] = {
            "host_id": args.host_id,
            "scan_id": args.scan_id,
            "observed_at": when,
            "pool": args.pool,
        }
        if args.mode == "linux":
            slot = (
                Slot(kind="host", id=args.host_id, expected_twin_id=args.expected_twin_id)
                if args.bind_host_slot
                else None
            )
            rows = collect_linux(
                **kwargs, self_slot=slot, expected_board_serial=args.expected_board_serial
            )
        else:
            rows = collect_bridge(args.url, **kwargs, token_env=args.token_env)
        output = json.dumps([row.model_dump(mode="json") for row in rows], indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 0
    except ValidationError:
        # Pydantic's default error prints raw input, which could be a remote error/secret.
        print(
            "Collection failed: inventory does not satisfy the observation schema", file=sys.stderr
        )
    except (CollectionError, OSError, ValueError) as exc:
        message = (
            str(exc) if isinstance(exc, CollectionError) else "invalid input or inaccessible file"
        )
        print(f"Collection failed: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
