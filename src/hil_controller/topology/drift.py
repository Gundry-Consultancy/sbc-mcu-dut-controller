"""What a controller restart would do to the live DB: DB vs topology YAML drift.

The seeder (:mod:`hil_controller.topology.seeder`) re-applies ``topology.yaml``
on every start with ``ON CONFLICT DO UPDATE``, so an edit made in the web UI to
a row that the YAML also defines is *reverted* on restart, while rows that exist
only in the DB survive but are missing from the committed topology. This module
compares the two with the seeder's own defaults so an operator can see exactly
which UI changes need exporting and committing (``/ui/topology``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hil_controller.topology.export import build_export_dict
from hil_controller.topology.seeder import _addr

#: Per section: the fields the seeder overwrites on conflict, with the default it
#: writes when the YAML omits them. Fields it preserves (device runtime wiring,
#: statuses) are deliberately absent: a restart keeps the DB value.
SEEDED_FIELDS: dict[str, dict[str, Any]] = {
    "hosts": {
        "role": "",
        "addr": "",
        "transport": "ssh",
        "ssh_user": "pi",
        "ssh_key_path": None,
        "max_concurrent_jobs": None,
        "capabilities": [],
        "api_url": None,
        "token_env": None,
    },
    "devices": {
        "host_id": None,
        "kind": "",
        "model": "",
        "capabilities": [],
        "usb": None,
        "pool": "public",
        "flasher": None,
        "camera_id": None,
        "qr_identifier": None,
        "power_control": None,
    },
    "auxes": {
        "kind": "",
        "model": "",
        "capabilities": [],
        "interface": "",
        "observability": "none",
        "pool": "public",
    },
    "cameras": {
        "host_id": None,
        "source": "",
        "kind": None,
        "model": "",
        "resolution": None,
        "fps": None,
        "pool": "public",
        "notes": None,
    },
    "peripherals": {
        "kind": "display",
        "model": "",
        "product_url": None,
        "specs": None,
        "notes": None,
    },
    "strands": {
        "mux_aux": None,
        "mux_group": None,
        "tca_address": None,
        "pool": "public",
        "notes": None,
        "components": [],
        "routes": [],
    },
}


def _norm(value: Any) -> Any:
    """Compare-friendly form: '' / None / [] / {} are all 'unset'."""
    if value in ("", None, [], {}):
        return None
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items()) if _norm(v) is not None}
    return value


def _strand_parts(strand: dict[str, Any], key: str) -> Any:
    items = strand.get(key) or []
    if key == "components":
        keep = ("id", "model", "address", "tca_channel", "ws_types", "capabilities", "notes")
        rows = []
        for c in items:
            row = {k: c.get(k) for k in keep}
            row["address"] = _addr(row["address"])
            rows.append(_norm(row))
        return sorted(rows, key=lambda c: str(c.get("id")))
    return sorted(
        (_norm({"device": r.get("device"), "channel": r.get("channel")}) for r in items),
        key=lambda r: (str(r.get("device")), str(r.get("channel"))),
    )


def _row_diff(section: str, db_row: dict[str, Any], yaml_row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, default in SEEDED_FIELDS[section].items():
        if section == "strands" and field in ("components", "routes"):
            db_v, yaml_v = _strand_parts(db_row, field), _strand_parts(yaml_row, field)
        else:
            db_v = _norm(db_row.get(field))
            yaml_v = _norm(yaml_row.get(field, default))
            if field == "tca_address":
                yaml_v = _norm(_addr(yaml_row.get(field)))
            if section == "devices" and field == "host_id":
                yaml_v = _norm(yaml_row.get("host_id"))
        if db_v != yaml_v:
            out[field] = {"db": db_v, "yaml": yaml_v}
    return out


def compute_drift(db_export: dict[str, Any], topology: dict[str, Any] | None) -> dict[str, Any]:
    """Diff a :func:`build_export_dict` result against parsed topology YAML."""
    topology = topology or {}
    items: list[dict[str, Any]] = []
    for section in SEEDED_FIELDS:
        db_rows = {r["id"]: r for r in db_export.get(section) or []}
        yaml_rows = {r["id"]: r for r in topology.get(section) or [] if "id" in r}
        for rid, db_row in db_rows.items():
            if rid not in yaml_rows:
                items.append({"section": section, "id": rid, "change": "db_only", "fields": {}})
                continue
            fields = _row_diff(section, db_row, yaml_rows[rid])
            if fields:
                items.append(
                    {"section": section, "id": rid, "change": "reverted_on_restart",
                     "fields": fields}
                )
        for rid in yaml_rows.keys() - db_rows.keys():
            items.append(
                {"section": section, "id": rid, "change": "recreated_on_restart", "fields": {}}
            )
    db_conn = sorted(_norm(c) or {} for c in db_export.get("connections") or [])
    yaml_conn_raw = topology.get("connections") or []
    yaml_conn = sorted(_norm(c) or {} for c in yaml_conn_raw)
    # The seeder only replaces connections when the YAML lists any.
    if yaml_conn_raw and db_conn != yaml_conn:
        items.append(
            {"section": "connections", "id": "*", "change": "reverted_on_restart",
             "fields": {"connections": {"db": db_conn, "yaml": yaml_conn}}}
        )
    elif not yaml_conn_raw and db_conn:
        items.append({"section": "connections", "id": "*", "change": "db_only", "fields": {}})
    items.sort(key=lambda i: (i["change"] != "reverted_on_restart", i["section"], i["id"]))
    summary = {
        change: sum(1 for i in items if i["change"] == change)
        for change in ("reverted_on_restart", "db_only", "recreated_on_restart")
    }
    return {"items": items, "summary": summary, "total": len(items)}


async def topology_drift(db_path: str, topology_file: str | None) -> dict[str, Any]:
    """Drift between the live DB and ``topology_file`` (missing file = empty topology)."""
    path = Path(topology_file) if topology_file else None
    topology: dict[str, Any] = {}
    error = ""
    if path is not None and path.exists():
        try:
            topology = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            error = f"could not parse {path}: {exc}"
    result = compute_drift(await build_export_dict(db_path), topology)
    result["topology_file"] = str(path) if path else ""
    result["topology_exists"] = bool(path and path.exists())
    result["error"] = error
    return result
