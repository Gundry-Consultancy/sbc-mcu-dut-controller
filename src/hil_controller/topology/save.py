"""Write the live DB back into the topology seed file, preserving its comments.

The DB is what the controller schedules from; ``topology.yaml`` re-seeds it on
every restart (see :mod:`hil_controller.topology.drift`). Saving updates the
seed file *in place* with a round-trip YAML editor so the operator's comments,
section order and row order survive:

* rows in both: only the fields whose value differs are rewritten; a field the
  file does not mention is added only when it is not the seeder's default;
* rows only in the DB are appended to their section;
* rows only in the file (deleted in the UI) are removed;
* strand components/routes and connections are replaced when they differ.

Keys the export does not manage (e.g. a host's ``known_hosts``) are left alone.
The previous file is kept as ``<file>.bak-<UTC timestamp>`` and the new text is
written atomically. Committing the file to git stays a separate, deliberate step.
"""

from __future__ import annotations

import difflib
import io
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from hil_controller.topology.drift import SEEDED_FIELDS, _norm, compute_drift
from hil_controller.topology.export import build_export_dict
from hil_controller.topology.seeder import _addr

#: Fields the export writes beyond the seeder-overwritten ones (runtime wiring the
#: seeder keeps from the DB, statuses, associations). Value = seeder default.
EXTRA_FIELDS: dict[str, dict[str, Any]] = {
    "hosts": {},
    "devices": {
        "status": "available",
        "serial_port": None,
        "hub_host_id": None,  # default: the device's host_id (handled below)
        "hub_port_path": None,
        "solenoid_channel": None,
        "usb_serial": None,
        "bootsel_channel": None,
        "bootsel_inverted": None,
        "build_target": None,
        "peripheral_ids": None,
    },
    "auxes": {"status": "available"},
    "cameras": {"status": "available"},
    "peripherals": {},
    "strands": {"status": "available"},
}
SECTIONS = tuple(SEEDED_FIELDS)


def _yaml() -> YAML:
    y = YAML()  # round-trip mode: keeps comments, key order and quoting
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _plain(value: Any) -> Any:
    """ruamel containers → plain Python, for comparison."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _same(section: str, field: str, a: Any, b: Any) -> bool:
    if field in ("tca_address",):
        a, b = _addr(a), _addr(b)
    if section == "strands" and field == "components":
        a = [{**c, "address": _addr(c.get("address"))} for c in (a or [])]
        b = [{**c, "address": _addr(c.get("address"))} for c in (b or [])]
    return _norm(_plain(a)) == _norm(_plain(b))


def _default(section: str, field: str, row: dict[str, Any]) -> Any:
    if section == "devices" and field == "hub_host_id":
        return row.get("host_id")
    if field in SEEDED_FIELDS[section]:
        return SEEDED_FIELDS[section][field]
    return EXTRA_FIELDS[section].get(field)


def _sync_row(section: str, target: CommentedMap, db_row: dict[str, Any]) -> list[str]:
    """Bring one existing YAML row in line with its DB row; returns changed fields."""
    changed: list[str] = []
    fields = [*SEEDED_FIELDS[section], *EXTRA_FIELDS[section]]
    for field in fields:
        db_value = db_row.get(field)
        if field in target:
            if _same(section, field, target[field], db_value):
                continue
            default = _default(section, field, db_row)
            if db_value is None or _same(section, field, db_value, default):
                del target[field]
            else:
                target[field] = db_value
            changed.append(field)
        elif db_value is not None and not _same(
            section, field, db_value, _default(section, field, db_row)
        ):
            target[field] = db_value
            changed.append(field)
    return changed


def _new_row(section: str, db_row: dict[str, Any]) -> CommentedMap:
    row = CommentedMap()
    row["id"] = db_row["id"]
    for field in (*SEEDED_FIELDS[section], *EXTRA_FIELDS[section]):
        value = db_row.get(field)
        if value is not None and not _same(section, field, value, _default(section, field, db_row)):
            row[field] = value
    return row


def merge_into(doc: CommentedMap, export: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the DB export onto a round-trip YAML document; returns a change log."""
    log: list[dict[str, Any]] = []
    for section in SECTIONS:
        db_rows = {r["id"]: r for r in export.get(section) or []}
        seq = doc.get(section)
        if seq is None:
            if not db_rows:
                continue
            seq = doc[section] = CommentedSeq()
        for item in list(seq):
            rid = item.get("id") if isinstance(item, dict) else None
            if rid is not None and rid not in db_rows:
                seq.remove(item)
                log.append({"section": section, "id": rid, "change": "removed"})
        present = {item.get("id") for item in seq if isinstance(item, dict)}
        for item in seq:
            if isinstance(item, dict) and item.get("id") in db_rows:
                fields = _sync_row(section, item, db_rows[item["id"]])
                if fields:
                    log.append(
                        {"section": section, "id": item["id"], "change": "updated",
                         "fields": fields}
                    )
        for rid, db_row in db_rows.items():
            if rid not in present:
                seq.append(_new_row(section, db_row))
                log.append({"section": section, "id": rid, "change": "added"})
    db_conn = export.get("connections") or []
    if db_conn or doc.get("connections"):
        if _norm(_plain(doc.get("connections") or [])) != _norm(db_conn):
            doc["connections"] = db_conn
            log.append({"section": "connections", "id": "*", "change": "replaced"})
    return log


def _dump(doc: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


async def plan_save(db_path: str, topology_file: str) -> dict[str, Any]:
    """What a save would write: new text, unified diff and change log (no writes)."""
    if not topology_file:
        raise ValueError("no topology file is configured (HIL_TOPOLOGY_FILE)")
    path = Path(topology_file)
    old_text = path.read_text() if path.exists() else ""
    doc = _yaml().load(old_text) if old_text.strip() else CommentedMap()
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    export = await build_export_dict(db_path)
    log = merge_into(doc, export)
    new_text = _dump(doc) if log or not path.exists() else old_text
    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{path} (now)",
            tofile=f"{path} (after save)",
        )
    )
    # Proof the saved file would re-seed exactly the live DB.
    drift_after = compute_drift(export, YAML(typ="safe").load(new_text) or {})
    return {
        "topology_file": str(path),
        "exists": path.exists(),
        "changes": log,
        "diff": diff,
        "new_text": new_text,
        "drift_after": drift_after["summary"],
    }


async def save_topology(db_path: str, topology_file: str) -> dict[str, Any]:
    """Write the live DB into ``topology_file`` (backup + atomic replace)."""
    plan = await plan_save(db_path, topology_file)
    path = Path(plan["topology_file"])
    result = {k: v for k, v in plan.items() if k != "new_text"}
    result["backup"] = None
    result["written"] = False
    if not plan["changes"] and path.exists():
        return result
    if path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_text(path.read_text())
        result["backup"] = str(backup)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(plan["new_text"])
    os.replace(tmp, path)
    result["written"] = True
    return result
