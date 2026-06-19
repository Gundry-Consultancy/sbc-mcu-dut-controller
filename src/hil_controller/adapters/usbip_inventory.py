"""Shared helper: query a host's exportable usbip busids + join against devices.

Used by both ``api/hosts.py`` (REST endpoint) and ``web/router.py`` (the
``/ui/usbip`` bench overview page) so the underlying ``usbip list -l``
parse + match-to-device logic stays in one place.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from hil_controller.adapters.usb_scan import (
    parse_dev_links,
    parse_lsusb,
    parse_lsusb_tree,
    parse_lsusb_verbose,
    parse_uhubctl,
    parse_usbip_list,
)
from hil_controller.hosts.base import ExecResult

_USBIP_LIST_CMD = ["sudo", "-n", "/usr/sbin/usbip", "list", "-l"]
_LSUSB_CMD = ["sh", "-lc", "lsusb 2>/dev/null || true"]
_LSUSB_VERBOSE_CMD = ["sh", "-lc", "sudo -n lsusb -v 2>/dev/null || lsusb -v 2>/dev/null || true"]
_LSUSB_TREE_CMD = ["sh", "-lc", "lsusb -t 2>/dev/null || true"]
_UHUBCTL_CMD = ["sh", "-lc", "sudo -n uhubctl 2>/dev/null || uhubctl 2>/dev/null || true"]
# Enumerate the stable /dev symlinks a serial-capture / UF2-MSC-flashing step
# relies on, resolving each to its current target. Emits TAB-separated
# `<base>\t<name>\t<target>` lines; parsed by parse_dev_links().
_DEV_LINKS_CMD = [
    "sh",
    "-lc",
    "for base in /dev/serial/by-id /dev/serial/by-path "
    "/dev/disk/by-id /dev/disk/by-label /dev/disk/by-path; do "
    '[ -d "$base" ] || continue; '
    'for link in "$base"/*; do '
    '[ -e "$link" ] || continue; '
    "printf '%s\\t%s\\t%s\\n' \"$base\" "
    '"$(basename "$link")" "$(readlink -f "$link")"; '
    "done; done 2>/dev/null || true",
]


@dataclass
class ExportableBusid:
    """One row of ``usbip list -l`` on a host, joined against the devices table."""

    busid: str
    vid: str
    pid: str
    description: str = ""
    matched_device_id: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    serial: str | None = None
    speed: str | None = None
    max_power: str | None = None
    num_interfaces: int | None = None
    device_class: str | None = None
    driver: str | None = None
    lsusb_description: str | None = None
    port_power_status: str | None = None
    port_connect_status: str | None = None
    port_status_text: str | None = None


@dataclass
class UsbHubInfo:
    """Summarised `uhubctl` state for one hub."""

    location: str
    hub_vid_pid: str | None = None
    hub_description: str = ""
    ports: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DevLinks:
    """Stable ``/dev`` symlinks needed for serial capture + UF2/MSC flashing.

    Each list holds ``{"name", "target"}`` dicts: ``serial_by_id`` /
    ``serial_by_path`` map persistent names to the current ``ttyACM*``;
    ``disk_by_id`` / ``disk_by_label`` map to the current block device for
    drag-and-drop bootloader drives (``WIPPER``, ``RPI-RP2``, ``*BOOT``).
    """

    serial_by_id: list[dict[str, str]] = field(default_factory=list)
    serial_by_path: list[dict[str, str]] = field(default_factory=list)
    disk_by_id: list[dict[str, str]] = field(default_factory=list)
    disk_by_label: list[dict[str, str]] = field(default_factory=list)
    # Mirrors USB topology; last-resort disambiguator, unpleasant to read.
    disk_by_path: list[dict[str, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.serial_by_id
            or self.serial_by_path
            or self.disk_by_id
            or self.disk_by_label
            or self.disk_by_path
        )


@dataclass
class HostBusidInventory:
    """Whole-host view: the busid list, daemon liveness, optional error blob."""

    host_id: str
    daemon_listening: bool
    busids: list[ExportableBusid] = field(default_factory=list)
    hub_info: list[UsbHubInfo] = field(default_factory=list)
    error: str | None = None
    dev_links: DevLinks | None = None


def _with_timeout(cmd: list[str], timeout_s: float | None) -> list[str]:
    """Prepend ``timeout <N>`` to *cmd* when a cap is requested, else return it
    unchanged so the caller waits for the full output.

    ``lsusb -v`` is just slow on a wedged/large bus (it reads every device's
    descriptors) — it *does* finish, so by default we wait. A timeout is an
    opt-in safety cap (e.g. from the page) for when the operator would rather get
    a possibly-partial table quickly. The lsusb commands already end in
    ``|| true`` so an expiry (exit 124) is harmless; usbip's non-zero is handled
    as daemon-down.
    """
    # isinstance guard: tolerate a non-numeric (e.g. a FastAPI Query sentinel
    # when a route handler is called directly in a test) → treat as no cap.
    if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        return cmd
    return ["timeout", f"{timeout_s:g}", *cmd]


async def query_host_busids(
    transport: Any,
    *,
    host_id: str,
    device_busid_map: dict[str, str],
    include_dev_links: bool = False,
    timeout_s: float | None = None,
) -> HostBusidInventory:
    """Run ``sudo -n /usr/sbin/usbip list -l`` over *transport*, annotate, return.

    ``device_busid_map`` maps ``hub_port_path`` → ``device.id`` for every
    device that names this host as its hub (caller pre-computes from the
    DB). Each parsed busid gets its ``matched_device_id`` filled in from
    that map, or stays ``None`` for unassigned ports.

    When ``include_dev_links`` is set, the stable ``/dev/serial/by-*`` and
    ``/dev/disk/by-*`` symlinks are collected too (one extra concurrent
    exec) and returned in ``dev_links`` — the paths serial-capture and
    UF2/MSC flashing need on Arduino-style hosts. The collection is
    best-effort: it survives even when usbipd itself is down.

    Non-zero exit (usbipd down, sudoers misconfigured, etc.) returns a
    response with ``daemon_listening=False`` and the captured stderr/stdout
    in ``error`` — callers never see an exception from this function.
    """
    core = asyncio.gather(
        _exec_capture(transport, _with_timeout(_USBIP_LIST_CMD, timeout_s)),
        _exec_capture(transport, _with_timeout(_LSUSB_CMD, timeout_s)),
        _exec_capture(transport, _with_timeout(_LSUSB_VERBOSE_CMD, timeout_s)),
        _exec_capture(transport, _with_timeout(_LSUSB_TREE_CMD, timeout_s)),
        _exec_capture(transport, _with_timeout(_UHUBCTL_CMD, timeout_s)),
    )
    if include_dev_links:
        core_results, dev_links_result = await asyncio.gather(
            core, _exec_capture(transport, _with_timeout(_DEV_LINKS_CMD, timeout_s))
        )
    else:
        core_results, dev_links_result = await core, None
    (
        usbip_result,
        lsusb_result,
        lsusb_verbose_result,
        lsusb_tree_result,
        uhubctl_result,
    ) = core_results
    dev_links = _build_dev_links(dev_links_result)
    if usbip_result.exit_status != 0:
        return HostBusidInventory(
            host_id=host_id,
            daemon_listening=False,
            busids=[],
            error=(usbip_result.stderr or usbip_result.stdout or "").strip()[:500],
            dev_links=dev_links,
        )
    parsed = parse_usbip_list(usbip_result.stdout or "")
    lsusb_rows = parse_lsusb(lsusb_result.stdout or "")
    verbose_rows = parse_lsusb_verbose(lsusb_verbose_result.stdout or "")
    tree_rows = parse_lsusb_tree(lsusb_tree_result.stdout or "")
    hub_rows = parse_uhubctl(uhubctl_result.stdout or "")
    short_by_key = {(row["bus"], row["device"]): row for row in lsusb_rows}
    verbose_by_key = {(row["bus"], row["device"]): row for row in verbose_rows}
    tree_by_busid = {row["busid"]: row for row in tree_rows}
    short_by_vid_pid = _group_unique_vid_pid(lsusb_rows)
    verbose_by_vid_pid = _group_unique_vid_pid(verbose_rows)
    busids = [
        _merge_busid_row(
            row=row,
            device_busid_map=device_busid_map,
            tree_by_busid=tree_by_busid,
            short_by_key=short_by_key,
            verbose_by_key=verbose_by_key,
            short_by_vid_pid=short_by_vid_pid,
            verbose_by_vid_pid=verbose_by_vid_pid,
            hub_rows=hub_rows,
        )
        for row in parsed
    ]
    hub_info = [
        UsbHubInfo(
            location=row["location"],
            hub_vid_pid=row.get("hub_vid_pid"),
            hub_description=row.get("hub_description") or "",
            ports=list(row.get("ports") or []),
        )
        for row in hub_rows
    ]
    return HostBusidInventory(
        host_id=host_id,
        daemon_listening=True,
        busids=busids,
        hub_info=hub_info,
        dev_links=dev_links,
    )


def _build_dev_links(result: ExecResult | None) -> DevLinks | None:
    """Parse a dev-links exec into DevLinks, or None when not collected/failed."""
    if result is None or result.exit_status != 0:
        return None
    grouped = parse_dev_links(result.stdout or "")
    return DevLinks(
        serial_by_id=grouped["serial_by_id"],
        serial_by_path=grouped["serial_by_path"],
        disk_by_id=grouped["disk_by_id"],
        disk_by_label=grouped["disk_by_label"],
        disk_by_path=grouped["disk_by_path"],
    )


async def _exec_capture(transport: Any, argv: list[str]) -> ExecResult:
    try:
        return await transport.exec(argv)
    except Exception as exc:
        return ExecResult(exit_status=1, stdout="", stderr=str(exc))


def _group_unique_vid_pid(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any] | None]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = ((row.get("vid") or "").lower(), (row.get("pid") or "").lower())
        if key == ("", ""):
            continue
        grouped.setdefault(key, []).append(row)
    return {key: values[0] if len(values) == 1 else None for key, values in grouped.items()}


def _merge_busid_row(
    *,
    row: dict[str, Any],
    device_busid_map: dict[str, str],
    tree_by_busid: dict[str, dict[str, Any]],
    short_by_key: dict[tuple[int, int], dict[str, Any]],
    verbose_by_key: dict[tuple[int, int], dict[str, Any]],
    short_by_vid_pid: dict[tuple[str, str], dict[str, Any] | None],
    verbose_by_vid_pid: dict[tuple[str, str], dict[str, Any] | None],
    hub_rows: list[dict[str, Any]],
) -> ExportableBusid:
    tree_row = tree_by_busid.get(row["busid"])
    short_row: dict[str, Any] | None = None
    verbose_row: dict[str, Any] | None = None
    if tree_row is not None:
        key = (int(tree_row["bus"]), int(tree_row["device"]))
        short_row = short_by_key.get(key)
        verbose_row = verbose_by_key.get(key)
    if short_row is None:
        short_row = short_by_vid_pid.get((row["vid"], row["pid"]))
    if verbose_row is None:
        verbose_row = verbose_by_vid_pid.get((row["vid"], row["pid"]))
    port_info = _match_hub_port(row["busid"], hub_rows)
    return ExportableBusid(
        busid=row["busid"],
        vid=row["vid"],
        pid=row["pid"],
        description=row.get("description") or "",
        matched_device_id=device_busid_map.get(row["busid"]),
        manufacturer=_clean_string(verbose_row, "manufacturer"),
        product=_clean_string(verbose_row, "product"),
        serial=_clean_string(verbose_row, "serial"),
        speed=_clean_string(tree_row, "speed"),
        max_power=_clean_string(verbose_row, "max_power"),
        num_interfaces=verbose_row.get("num_interfaces") if verbose_row else None,
        device_class=_clean_string(verbose_row, "device_class"),
        driver=_clean_string(tree_row, "driver"),
        lsusb_description=_clean_string(short_row, "description"),
        port_power_status=port_info.get("power_status") if port_info else None,
        port_connect_status=port_info.get("connect_status") if port_info else None,
        port_status_text=port_info.get("status") if port_info else None,
    )


def _match_hub_port(busid: str, hub_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for hub in sorted(hub_rows, key=lambda row: len(row.get("location") or ""), reverse=True):
        location = hub.get("location") or ""
        if not busid.startswith(location):
            continue
        if busid == location:
            continue
        remainder = busid[len(location) :]
        if not remainder.startswith("."):
            continue
        port_segment = remainder[1:].split(".", 1)[0]
        if not port_segment.isdigit():
            continue
        port_number = int(port_segment)
        for port in hub.get("ports") or []:
            if int(port.get("port_number") or -1) == port_number:
                return port
    return None


def _clean_string(row: dict[str, Any] | None, key: str) -> str | None:
    if not row:
        return None
    value = row.get(key)
    if isinstance(value, str):
        value = value.strip()
    return value or None
