"""Backport the live DB (the source of truth) into reseedable topology YAML.

The DB is authoritative; ``build_export_dict`` renders it back into the same
shape :mod:`hil_controller.topology.seeder` consumes, so an operator can commit
the result to ``topology.yaml`` for a future reseed. Strands round-trip exactly;
the other sections are emitted in seeder-input shape too.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from hil_controller.api.strands import _load_strand
from hil_controller.db.connection import get_db


def _jlist(value: str | None) -> list[Any]:
    return json.loads(value or "[]")


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


async def build_export_dict(db_path: str) -> dict[str, Any]:
    async with get_db(db_path) as db:
        hosts = []
        async with db.execute("SELECT * FROM hosts ORDER BY id") as cur:
            for row in await cur.fetchall():
                r = dict(row)
                h = _drop_none(
                    {
                        k: r.get(k)
                        for k in ("id", "role", "addr", "transport", "ssh_user",
                                  "ssh_key_path", "max_concurrent_jobs")
                    }
                )
                if r.get("capabilities_json"):
                    h["capabilities"] = _jlist(r["capabilities_json"])
                hosts.append(h)

        dp: dict[str, list[str]] = {}
        async with db.execute("SELECT device_id, peripheral_id FROM device_peripherals") as cur:
            for r in await cur.fetchall():
                dp.setdefault(r["device_id"], []).append(r["peripheral_id"])

        devices = []
        async with db.execute("SELECT * FROM devices ORDER BY id") as cur:
            for row in await cur.fetchall():
                r = dict(row)
                d = _drop_none(
                    {
                        k: r.get(k)
                        for k in ("id", "host_id", "kind", "model", "pool", "status",
                                  "serial_port", "flasher", "hub_host_id", "hub_port_path",
                                  "solenoid_channel", "usb_serial", "build_target")
                    }
                )
                if r.get("capabilities_json"):
                    d["capabilities"] = _jlist(r["capabilities_json"])
                if dp.get(r["id"]):
                    d["peripheral_ids"] = dp[r["id"]]
                devices.append(d)

        auxes = []
        async with db.execute("SELECT * FROM auxes ORDER BY id") as cur:
            for row in await cur.fetchall():
                r = dict(row)
                a = _drop_none(
                    {k: r.get(k) for k in ("id", "kind", "model", "interface",
                                           "observability", "pool", "status")}
                )
                if r.get("capabilities_json"):
                    a["capabilities"] = _jlist(r["capabilities_json"])
                auxes.append(a)

        peripherals = []
        async with db.execute("SELECT * FROM peripherals ORDER BY id") as cur:
            for row in await cur.fetchall():
                r = dict(row)
                keys = ("id", "kind", "model", "product_url", "notes")
                p = _drop_none({k: r.get(k) for k in keys})
                if r.get("specs_json"):
                    p["specs"] = json.loads(r["specs_json"])
                peripherals.append(p)

        async with db.execute("SELECT id FROM strands ORDER BY id") as cur:
            strand_ids = [r["id"] for r in await cur.fetchall()]
        strands = [_drop_none(await _load_strand(db, sid)) for sid in strand_ids]

    return {
        "hosts": hosts,
        "devices": devices,
        "auxes": auxes,
        "peripherals": peripherals,
        "strands": strands,
    }


async def build_export_yaml(db_path: str) -> str:
    return yaml.safe_dump(await build_export_dict(db_path), sort_keys=False)
