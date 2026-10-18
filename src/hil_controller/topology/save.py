"""Write the live DB back into the topology seed file, preserving its comments.

The DB is what the controller schedules from; ``topology.yaml`` re-seeds it on
every restart (see :mod:`hil_controller.topology.drift`). Saving updates the
seed file *in place* so a reviewer sees only real changes in ``git diff``:

* rows in both: only the fields whose value differs are rewritten; a field the
  file does not mention is added only when it is not the seeder's default;
* rows only in the DB are appended to their section;
* rows only in the file (deleted in the UI) are removed;
* strand components/routes and connections are replaced when they differ.

Mechanics: the file is split into one block per top-level section (the comment
lines just above a section belong to it). Sections without changes are kept
byte-for-byte; a changed section is loaded and re-emitted with a round-trip
editor (ruamel.yaml) in that section's own list-indent style, then any line that
differs only cosmetically (flow-mapping spacing, comment alignment) is restored
from the original. Keys the export does not manage (e.g. ``known_hosts``) are
left alone. The previous file is kept as ``<file>.bak-<UTC timestamp>`` and the
new text is written atomically. Committing it to git stays a separate step.
"""

from __future__ import annotations

import difflib
import io
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.constructor import DuplicateKeyError

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
_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*):")


def _represent_none(representer: Any, _data: None) -> Any:
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")


def _yaml(seq_offset: int = 2) -> YAML:
    """Round-trip YAML (keeps comments, key order and quoting).

    ``seq_offset`` is where a list's ``-`` sits under its parent key: 0 for
    PyYAML's indentless style (``key:\\n- item``), 2 for ``key:\\n  - item``.
    """
    y = YAML()
    y.preserve_quotes = True
    y.allow_unicode = False  # PyYAML's default: non-ASCII stays escaped
    y.width = 4096  # never re-wrap a line the file did not wrap
    y.indent(mapping=2, sequence=seq_offset + 2, offset=seq_offset)
    y.representer.add_representer(type(None), _represent_none)
    return y


# --------------------------------------------------------------------------- #
# file <-> per-section blocks                                                 #
# --------------------------------------------------------------------------- #


def _split(text: str) -> tuple[str, dict[str, str], str]:
    """``(preamble, {section: block}, trailer)``.

    A block runs from the comment lines that introduce a section up to the next
    such introduction, so a section header comment travels with its section and
    rows appended to a section land before the next section's comments. Blank
    separator lines stay with the section they follow.
    """
    lines = text.splitlines(keepends=True)
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _TOP_KEY.match(line)
        if m:
            j = i
            while j > 0 and lines[j - 1].startswith("#"):
                j -= 1
            starts.append((j, m.group(1)))
    if not starts:
        return text, {}, ""
    preamble = "".join(lines[: starts[0][0]])
    blocks: dict[str, str] = {}
    for n, (start, key) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        blocks[key] = "".join(lines[start:end])
    return preamble, blocks, ""


def _seq_offset(block: str) -> int:
    in_body = False
    for line in block.splitlines():
        if not in_body:
            in_body = bool(_TOP_KEY.match(line))
            continue
        stripped = line.lstrip(" ")
        if stripped.startswith("- "):
            return len(line) - len(stripped)
        if stripped and not stripped.startswith("#"):
            break
    return 2


def _cosmetic(line: str) -> str:
    """Comparison key that ignores formatting-only differences."""
    code, sep, comment = line.rstrip("\n").partition(" #")
    code = re.sub(r"\{\s+", "{", code)
    code = re.sub(r"\s+\}", "}", code)
    code = re.sub(r",\s+", ", ", code).rstrip()
    return code + (" #" + comment.strip() if sep else "")


def _restore_cosmetics(old: str, new: str) -> str:
    """Keep the original text of every line whose content did not change."""
    old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(
        None, [_cosmetic(x) for x in old_lines], [_cosmetic(x) for x in new_lines],
        autojunk=False,
    )
    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        out.extend(old_lines[i1:i2] if tag == "equal" else new_lines[j1:j2])
    return "".join(out)


def _load_block(block: str, section: str, notes: list[dict[str, Any]]) -> CommentedMap:
    """Load one section block, collapsing a key repeated with the *same* value.

    Repeats with *different* values are refused: the seeder (PyYAML) keeps the
    last one, a round-trip editor the first, so guessing could silently change
    what the bench seeds.
    """
    try:
        doc = _yaml().load(block)
    except DuplicateKeyError as exc:
        lenient = _yaml()
        lenient.allow_duplicate_keys = True
        doc = lenient.load(block)
        if _plain(doc) != yaml.safe_load(block):
            raise ValueError(
                f"section {section!r} repeats a key with different values; fix it by hand "
                f"first ({' '.join(str(exc).split())[:300]})"
            ) from exc
        notes.append(
            {"section": section, "id": "*", "change": "collapsed duplicate key(s)",
             "fields": [" ".join(str(exc).split())[:200]]}
        )
    if not isinstance(doc, dict):
        raise ValueError(f"section {section!r} is not a YAML mapping")
    return doc


# --------------------------------------------------------------------------- #
# merging the export into the parsed sections                                 #
# --------------------------------------------------------------------------- #


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
    for field in (*SEEDED_FIELDS[section], *EXTRA_FIELDS[section]):
        db_value = db_row.get(field)
        default = _default(section, field, db_row)
        if field in target:
            if _same(section, field, target[field], db_value):
                continue
            if db_value is None or _same(section, field, db_value, default):
                del target[field]
            else:
                target[field] = db_value
            changed.append(field)
        elif db_value is not None and not _same(section, field, db_value, default):
            # Before the row's last key: comments trailing a row belong to what follows.
            target.insert(max(len(target) - 1, 1), field, db_value)
            changed.append(field)
    return changed


def _new_row(section: str, db_row: dict[str, Any]) -> CommentedMap:
    row = CommentedMap()
    row["id"] = db_row["id"]
    for field in (*SEEDED_FIELDS[section], *EXTRA_FIELDS[section]):
        value = db_row.get(field)
        if value is not None and not _same(section, field, value, _default(section, field, db_row)):
            if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
                flow = CommentedSeq(value)
                flow.fa.set_flow_style()  # [a, b] like hand-written rows
                value = flow
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


# --------------------------------------------------------------------------- #
# plan / save                                                                 #
# --------------------------------------------------------------------------- #


def _render_section(
    key: str, value: Any, old_block: str | None, parsed: CommentedMap | None
) -> str:
    """Re-emit one section; ``parsed`` is its loaded block (header comments included)."""
    style = _seq_offset(old_block) if old_block else 0
    target: Any = {key: value}
    if parsed is not None:
        parsed[key] = value
        target = parsed
    buf = io.StringIO()
    _yaml(style).dump(target, buf)
    text = buf.getvalue()
    if not old_block:
        return text
    text = _restore_cosmetics(old_block, text)
    blank_tail = old_block[len(old_block.rstrip("\n")):]
    return text.rstrip("\n") + (blank_tail if len(blank_tail) > 1 else "\n")


async def plan_save(db_path: str, topology_file: str) -> dict[str, Any]:
    """What a save would write: new text, unified diff and change log (no writes)."""
    if not topology_file:
        raise ValueError("no topology file is configured (HIL_TOPOLOGY_FILE)")
    path = Path(topology_file)
    old_text = path.read_text() if path.exists() else ""
    preamble, blocks, trailer = _split(old_text)
    if old_text.strip() and not blocks:
        raise ValueError(f"{path} is not a YAML mapping")
    notes: list[dict[str, Any]] = []
    sections: dict[str, CommentedMap] = {k: _load_block(b, k, notes) for k, b in blocks.items()}
    doc = CommentedMap((k, d.get(k)) for k, d in sections.items())
    export = await build_export_dict(db_path)
    log = notes + merge_into(doc, export)
    changed = {c["section"] for c in log}
    out = [preamble]
    for key, value in doc.items():
        block = blocks.get(key)
        out.append(block if block is not None and key not in changed
                   else _render_section(key, value, block, sections.get(key)))
    out.append(trailer)
    new_text = "".join(out) if log or not path.exists() else old_text
    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{path} (now)",
            tofile=f"{path} (after save)",
        )
    )
    # Proof the saved file would re-seed exactly the live DB.
    drift_after = compute_drift(export, yaml.safe_load(new_text) or {})
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
