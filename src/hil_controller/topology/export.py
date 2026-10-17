"""Backport the live DB (the source of truth) into reseedable topology YAML.

The DB is authoritative; ``build_export_dict`` renders it back into the same
shape :mod:`hil_controller.topology.seeder` consumes, so an operator can commit
the result to ``topology.yaml`` for a future reseed. Every section the seeder
reads (hosts, devices, auxes, cameras, peripherals, strands, connections) is
emitted in seeder-input shape.
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
                                  "ssh_key_path", "max_concurrent_jobs", "api_url",
                                  "token_env")
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
                                  "serial_port", "flasher", "camera_id", "qr_identifier",
                                  "hub_host_id", "hub_port_path", "solenoid_channel",
                                  "usb_serial", "bootsel_channel", "build_target",
                                  "power_control")
                    }
                )
                if r.get("bootsel_inverted"):
                    d["bootsel_inverted"] = True
                if r.get("usb_json"):
                    d["usb"] = json.loads(r["usb_json"])
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

        cameras = []
        async with db.execute("SELECT * FROM cameras ORDER BY id") as cur:
            for row in await cur.fetchall():
                r = dict(row)
                c = _drop_none(
                    {k: r.get(k) for k in ("id", "host_id", "source", "kind", "model", "fps",
                                           "pool", "status", "notes")}
                )
                if r.get("resolution_w") is not None or r.get("resolution_h") is not None:
                    c["resolution"] = [r.get("resolution_w"), r.get("resolution_h")]
                cameras.append(c)

        connections = []
        async with db.execute("SELECT * FROM connections ORDER BY id") as cur:
            for row in await cur.fetchall():
                r = dict(row)
                connections.append(
                    _drop_none(
                        {
                            "aux": r["aux_id"],
                            "device": r.get("device_id"),
                            "mux": r.get("mux_id"),
                            "channel": r.get("mux_channel"),
                        }
                    )
                )

        async with db.execute("SELECT id FROM strands ORDER BY id") as cur:
            strand_ids = [r["id"] for r in await cur.fetchall()]
        strands = [_drop_none(await _load_strand(db, sid)) for sid in strand_ids]

    return {
        "hosts": hosts,
        "devices": devices,
        "auxes": auxes,
        "cameras": cameras,
        "peripherals": peripherals,
        "strands": strands,
        "connections": connections,
    }


async def build_export_yaml(db_path: str) -> str:
    return yaml.safe_dump(await build_export_dict(db_path), sort_keys=False)
