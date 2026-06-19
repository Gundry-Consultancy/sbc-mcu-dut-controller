"""Parse `usbip list -l` output + passively learn unseen VID/PIDs.

Passive learn runs during an exclusive_device lease: the worker polls the
hub host every few seconds via `usbip list -l` and upserts any VID/PID
appearing on the device's bus-id that we haven't seen before. New rows are
tagged `source='passive'` and `learned_from_job=<job_id>`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timezone
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

_BUSID_RE = re.compile(r"^\s*-\s*busid\s+(\S+)\s+\(([0-9a-fA-F]+):([0-9a-fA-F]+)\)")
_DESC_RE = re.compile(r"^\s*(.+?)\s+\([0-9a-fA-F]+:[0-9a-fA-F]+\)\s*$")
_LSUSB_RE = re.compile(
    r"^Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})(?:\s+(.*))?$"
)
_LSUSB_TREE_ROOT_RE = re.compile(
    r"^/:  Bus\s+(\d+)\.Port\s+(\d+):\s+Dev\s+(\d+),.*?Driver=([^,]+),\s*([^,\s]+)\s*$"
)
_LSUSB_TREE_CHILD_RE = re.compile(
    r"^(?P<indent>\s*)\|__ Port\s+(?P<port>\d+):\s+Dev\s+(?P<dev>\d+),"
    r".*?Driver=(?P<driver>[^,]+),\s*(?P<speed>[^,\s]+)\s*$"
)
_UHUBCTL_HEADER_RES = (
    re.compile(
        r"^Current status for hub\s+(\S+)\s+\[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*?)\]\s*$"
    ),
    re.compile(r"^Hub #\d+\s+at\s+(\S+)\s+\[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*?)\]\s*$"),
)
_UHUBCTL_PORT_RE = re.compile(r"^\s*Port\s+(\d+):\s+(.+)$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_usbip_list(text: str) -> list[dict[str, Any]]:
    """Parse `usbip list -l` output into [{busid, vid, pid, description}, ...]."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in (text or "").splitlines():
        m = _BUSID_RE.match(line)
        if m:
            if current:
                out.append(current)
            current = {
                "busid": m.group(1),
                "vid": m.group(2).lower(),
                "pid": m.group(3).lower(),
                "description": "",
            }
            continue
        if current and current["description"] == "":
            d = _DESC_RE.match(line)
            if d:
                current["description"] = d.group(1).strip()
    if current:
        out.append(current)
    return out


def parse_lsusb(text: str) -> list[dict[str, Any]]:
    """Parse `lsusb` short output into [{bus, device, vid, pid, description}, ...]."""
    out: list[dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _LSUSB_RE.match(line)
        if not match:
            continue
        out.append(
            {
                "bus": int(match.group(1)),
                "device": int(match.group(2)),
                "vid": match.group(3).lower(),
                "pid": match.group(4).lower(),
                "description": (match.group(5) or "").strip(),
            }
        )
    return out


def parse_lsusb_verbose(text: str) -> list[dict[str, Any]]:
    """Parse `lsusb -v` device blocks into structured descriptor fields."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        header = _LSUSB_RE.match(line.strip())
        if header:
            if current:
                out.append(current)
            current = {
                "bus": int(header.group(1)),
                "device": int(header.group(2)),
                "vid": header.group(3).lower(),
                "pid": header.group(4).lower(),
                "description": (header.group(5) or "").strip(),
                "manufacturer": "",
                "product": "",
                "serial": "",
                "device_class": "",
                "num_interfaces": None,
                "max_power": "",
            }
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("iManufacturer"):
            current["manufacturer"] = _descriptor_tail(stripped)
        elif stripped.startswith("iProduct"):
            current["product"] = _descriptor_tail(stripped)
        elif stripped.startswith("iSerial"):
            current["serial"] = _descriptor_tail(stripped)
        elif stripped.startswith("bDeviceClass"):
            current["device_class"] = _field_tail(stripped)
        elif stripped.startswith("bNumInterfaces"):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1].isdigit():
                current["num_interfaces"] = int(parts[1])
        elif stripped.startswith("MaxPower"):
            current["max_power"] = _field_tail(stripped)
    if current:
        out.append(current)
    return out


def parse_lsusb_tree(text: str) -> list[dict[str, Any]]:
    """Parse `lsusb -t` into busid-aware topology rows."""
    out: list[dict[str, Any]] = []
    current_bus: int | None = None
    path_stack: list[int] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        root = _LSUSB_TREE_ROOT_RE.match(line)
        if root:
            current_bus = int(root.group(1))
            path_stack = []
            continue
        child = _LSUSB_TREE_CHILD_RE.match(line)
        if child and current_bus is not None:
            parent_depth = max((len(child.group("indent")) // 4) - 1, 0)
            path = path_stack[:parent_depth] + [int(child.group("port"))]
            path_stack = path
            out.append(
                {
                    "bus": current_bus,
                    "device": int(child.group("dev")),
                    "busid": f"{current_bus}-" + ".".join(str(p) for p in path),
                    "driver": child.group("driver").strip(),
                    "speed": child.group("speed").strip(),
                }
            )
    return out


_DEV_LINK_BASE_TO_CATEGORY = {
    "/dev/serial/by-id": "serial_by_id",
    "/dev/serial/by-path": "serial_by_path",
    "/dev/disk/by-id": "disk_by_id",
    "/dev/disk/by-label": "disk_by_label",
    "/dev/disk/by-path": "disk_by_path",
}


# udev path_id bus-type keywords — each starts a new segment in a by-path name.
_DEV_PATH_BUS_TYPES = frozenset(
    {
        "pci",
        "usb",
        "platform",
        "acpi",
        "scsi",
        "ata",
        "sata",
        "sas",
        "nvme",
        "mmc",
        "ccw",
        "virtio",
        "ip",
        "xen",
        "vmbus",
        "serio",
        "bcma",
        "soc",
        "amba",
        "fc",
    }
)


def split_dev_path(name: str) -> list[str]:
    """Split a ``/dev/disk/by-path`` name into its bus-type segments.

    e.g. ``platform-3f980000.usb-usb-0:1.2:1.0-scsi-0:0:0:0`` ->
    ``['platform-3f980000.usb', 'usb-0:1.2:1.0', 'scsi-0:0:0:0']`` for an
    indented topology view. Tokenises on ``-`` and starts a new segment
    whenever a token is a known bus-type keyword (the ``-`` between a type and
    its value is otherwise indistinguishable from the segment separator).
    Falls back to a single segment for unrecognised shapes.
    """
    segments: list[str] = []
    for tok in (name or "").split("-"):
        if tok in _DEV_PATH_BUS_TYPES:
            segments.append(tok)
        elif segments:
            segments[-1] = f"{segments[-1]}-{tok}"
        else:
            segments.append(tok)
    return segments or [name]


def parse_dev_links(text: str) -> dict[str, list[dict[str, str]]]:
    """Parse the stable-``/dev``-symlink dump into category → [{name, target}].

    Input lines are TAB-separated ``<base-dir>\\t<link-name>\\t<resolved-target>``
    produced by listing ``/dev/serial/by-id``, ``/dev/serial/by-path``,
    ``/dev/disk/by-id``, ``/dev/disk/by-label`` and ``/dev/disk/by-path`` then
    ``readlink``-resolving each entry. These are the persistent paths a
    serial-capture or UF2/MSC-flashing step needs (``/dev/ttyACM*`` numbering
    and bootloader drive labels like ``WIPPER``/``RPI-RP2`` shuffle across
    reboots; the by-id / by-label names don't). ``disk_by_path`` mirrors the
    physical USB topology — handy when nothing else disambiguates, painful to
    read otherwise.

    Returns all categories (empty lists when none seen), each sorted by link
    name. Lines whose base dir isn't a known one are ignored.
    """
    grouped: dict[str, list[dict[str, str]]] = {
        cat: [] for cat in _DEV_LINK_BASE_TO_CATEGORY.values()
    }
    for raw_line in (text or "").splitlines():
        parts = raw_line.split("\t")
        if len(parts) != 3:
            continue
        base, name, target = (p.strip() for p in parts)
        category = _DEV_LINK_BASE_TO_CATEGORY.get(base)
        if category is None or not name:
            continue
        grouped[category].append({"name": name, "target": target})
    for entries in grouped.values():
        entries.sort(key=lambda e: e["name"])
    return grouped


def parse_uhubctl(text: str) -> list[dict[str, Any]]:
    """Parse `uhubctl` status output into hubs + per-port power state."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        header = _match_first(line.strip(), _UHUBCTL_HEADER_RES)
        if header:
            if current:
                out.append(current)
            current = {
                "location": header.group(1),
                "hub_vid_pid": header.group(2).lower(),
                "hub_description": (header.group(3) or "").strip(" ,"),
                "ports": [],
            }
            continue
        if current is None:
            continue
        port_match = _UHUBCTL_PORT_RE.match(line)
        if not port_match:
            continue
        status_text = port_match.group(2).strip()
        status_lower = status_text.lower()
        current["ports"].append(
            {
                "port_number": int(port_match.group(1)),
                "status": status_text,
                "power_status": _power_status(status_lower),
                "connect_status": _connect_status(status_lower),
            }
        )
    if current:
        out.append(current)
    return out


def _descriptor_tail(line: str) -> str:
    parts = line.split(None, 2)
    if len(parts) < 3:
        return ""
    return parts[2].strip()


def _field_tail(line: str) -> str:
    parts = line.split(None, 1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def _match_first(line: str, patterns: Iterable[re.Pattern[str]]) -> re.Match[str] | None:
    for pattern in patterns:
        match = pattern.match(line)
        if match:
            return match
    return None


def _power_status(status_lower: str) -> str | None:
    if "off" in status_lower:
        return "off"
    if "power" in status_lower:
        return "on"
    return None


def _connect_status(status_lower: str) -> str | None:
    if "connect" in status_lower and "disconnect" not in status_lower:
        return "connected"
    if "disconnect" in status_lower:
        return "disconnected"
    return None


ScanFn = Callable[[], list[dict[str, Any]] | Awaitable[list[dict[str, Any]]]]


async def _call_scan(scan_fn: ScanFn) -> list[dict[str, Any]]:
    try:
        result = scan_fn()
        if asyncio.iscoroutine(result):
            result = await result
        return result or []
    except Exception as exc:
        log.warning("usb scan failed: %s", exc)
        return []


async def learn_once(
    db_path: str,
    *,
    device_id: str,
    hub_port_path: str | None,
    job_id: str | None,
    scan_fn: ScanFn,
) -> int:
    """Run a single scan, upsert matching rows, return # newly added.

    Existing rows have their `last_seen_at` refreshed (and description
    filled in if missing) but `source` and `learned_from_job` are NOT
    overwritten — manual/seeder rows keep their provenance.
    """
    entries = await _call_scan(scan_fn)
    if not entries or not hub_port_path:
        return 0

    matches = [e for e in entries if e.get("busid") == hub_port_path]
    if not matches:
        return 0

    now = _now_iso()
    added = 0
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        for e in matches:
            vid = (e.get("vid") or "").lower()
            pid = (e.get("pid") or "").lower()
            if not (vid and pid):
                continue
            description = e.get("description") or None

            async with db.execute(
                "SELECT id, description FROM device_usb_ids "
                "WHERE device_id=? AND vid=? AND pid=? "
                "AND COALESCE(iserial,'')=''",
                (device_id, vid, pid),
            ) as cur:
                row = await cur.fetchone()

            if row:
                await db.execute(
                    "UPDATE device_usb_ids "
                    "SET last_seen_at=?, "
                    "    description=COALESCE(description, ?) "
                    "WHERE id=?",
                    (now, description, row["id"]),
                )
            else:
                await db.execute(
                    "INSERT INTO device_usb_ids "
                    "(device_id, vid, pid, role, description, "
                    " first_seen_at, last_seen_at, learned_from_job, source) "
                    "VALUES (?, ?, ?, 'unknown', ?, ?, ?, ?, 'passive')",
                    (device_id, vid, pid, description, now, now, job_id),
                )
                added += 1
        await db.commit()
    if added:
        log.info(
            "passive learn: +%d usb_ids on %s (job=%s)",
            added,
            device_id,
            job_id,
        )
    return added


async def make_ssh_scan_fn(transport, hub_host_id: str) -> ScanFn:
    """Build a scan_fn that runs `usbip list -l` over the given transport."""

    async def _scan() -> list[dict[str, Any]]:
        try:
            out = await transport.run("usbip list -l")
        except Exception as exc:
            log.debug("ssh usbip list -l failed on %s: %s", hub_host_id, exc)
            return []
        return parse_usbip_list(out if isinstance(out, str) else (out or {}).get("stdout", ""))

    return _scan


async def passive_learn_loop(
    db_path: str,
    *,
    device_id: str,
    hub_port_path: str | None,
    job_id: str | None,
    scan_fn: ScanFn,
    interval_s: float = 3.0,
) -> None:
    """Run learn_once on a loop until cancelled. Errors are swallowed."""
    if not hub_port_path:
        return
    try:
        while True:
            try:
                await learn_once(
                    db_path,
                    device_id=device_id,
                    hub_port_path=hub_port_path,
                    job_id=job_id,
                    scan_fn=scan_fn,
                )
            except Exception as exc:
                log.debug("passive_learn_loop iter failed: %s", exc)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        pass
