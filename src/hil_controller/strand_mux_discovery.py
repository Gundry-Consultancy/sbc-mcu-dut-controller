"""I2C strand muxes found over mDNS (``_i2c-strand._tcp``) → aux proposals.

Two products advertise the service and speak the same strand-mux API
(``/api/select[/<dut>]``, ``/api/isolate``, ``/api/groups/<g>/channel|select/<c>``,
``/api/topology``, ``/api/duts``, ``/api/probe``, ``/api/status``)::

    product=esp-usbip-bridge     the bridge's built-in ADG729/ADG728 mux
    product=sbc-dut-analog-mux   the standalone CircuitPython mux box

TXT: ``product``, ``api`` (strand-mux API version), ``mux`` (adg729 | adg728 |
mixed | none), ``groups``, ``duts``, ``path`` (/api), ``mac``, ``caps`` (bridge).

Proposals, never applied automatically:

* bridge mux → the bridge **host** with the same base MAC (from its cached
  capabilities). ``known`` when an aux already points at ``bridge:<host_id>`` (or
  at the bridge's own address); otherwise ``link``: apply creates one aux with
  ``interface: bridge:<host_id>`` so strands share the host's URL and token. It
  never creates a second host or an ``http://`` aux for the bridge. No matching
  bridge host → ``needs_bridge_host`` (apply the bridge proposal first).
* mux box → an existing ``i2c_strand_mux`` aux by MAC (``auxes.discovered_mac``,
  recorded when a discovered box is applied), then by address; else ``new`` at
  ``http://<hostname>.local:<port>`` when the name resolves, else the IP.

Each candidate is confirmed with ``GET /api/status`` before it is proposed.
Strand routing and existing strands are never touched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from hil_controller.adapters import esp_usbip_bridge as eub
from hil_controller.adapters.analog_mux import AnalogMuxAdapter, AnalogMuxError
from hil_controller.adapters.bridge_capabilities import capabilities_from_json, normalize_mac
from hil_controller.bridge_discovery import (
    PRODUCT_BRIDGE,
    PRODUCT_MUX_BOX,
    STRAND_TYPE,
    ApplyError,
    Candidate,
    MdnsRecord,
    _short,
    _url_host,
    bridge_hosts,
    merge_records,
)

AUX_KIND = "i2c_strand_mux"
BRIDGE_PREFIX = "bridge:"
CONFIRM_TIMEOUT_S = 5.0
_AUX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _mux_client(base_url: str) -> AnalogMuxAdapter:
    return AnalogMuxAdapter(base_url, timeout_s=CONFIRM_TIMEOUT_S)


def _base_url(host: str, port: int) -> str:
    return f"http://{host}" if port in (80, 0) else f"http://{host}:{port}"


@dataclass
class StrandMuxProposal:
    action: str  # link | known | new | needs_bridge_host | unconfirmed
    key: str
    product: str
    hostname: str = ""
    addresses: list[str] = field(default_factory=list)
    port: int = 80
    mac: str = ""
    txt: dict[str, str] = field(default_factory=dict)
    error: str = ""
    bridge_host_id: str | None = None
    matched_aux_id: str | None = None
    match_reason: str = ""
    aux: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "key": self.key,
            "product": self.product,
            "hostname": self.hostname,
            "addresses": self.addresses,
            "port": self.port,
            "mac": self.mac or None,
            "txt": self.txt,
            "error": self.error or None,
            "bridge_host_id": self.bridge_host_id,
            "matched_aux_id": self.matched_aux_id,
            "match_reason": self.match_reason or None,
            "aux": self.aux or None,
            "note": self.note or None,
        }


async def mux_auxes(db_path: str) -> list[dict[str, Any]]:
    from hil_controller.db.connection import get_db

    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT id, kind, model, interface, discovered_mac FROM auxes WHERE kind = ?",
            (AUX_KIND,),
        )
        return [dict(r) for r in await cur.fetchall()]


def strand_records(records: list[MdnsRecord]) -> list[MdnsRecord]:
    return [r for r in records if r.service == STRAND_TYPE]


async def _confirm(base_url: str, factory: Any) -> str:
    """'' when ``GET /api/status`` answers on ``base_url``, else the error."""
    try:
        await factory(base_url).status()
    except AnalogMuxError as exc:
        return str(exc)
    return ""


def _suggest_aux_id(cand: Candidate, mac: str, *, prefix: str = "mux-") -> str:
    name = _short(cand.hostname or cand.instance)
    if name and name.startswith("mux"):
        prefix = ""
    if name and _AUX_ID_RE.match(f"{prefix}{name}"):
        return f"{prefix}{name}"
    return f"{prefix}{mac.replace(':', '')[-6:]}" if mac else ""


async def build_strand_proposal(
    cand: Candidate,
    hosts: list[dict[str, Any]],
    auxes: list[dict[str, Any]],
    *,
    mux_client_factory: Any = None,
) -> StrandMuxProposal | None:
    """Confirm one ``_i2c-strand._tcp`` candidate and classify it (None = other product)."""
    factory = mux_client_factory or _mux_client
    product = (cand.txt.get("product") or "").strip().lower()
    if product not in (PRODUCT_BRIDGE, PRODUCT_MUX_BOX):
        return None
    mac = normalize_mac(cand.txt.get("mac"))
    p = StrandMuxProposal(
        action="unconfirmed",
        key=cand.key,
        product=product,
        hostname=cand.hostname,
        addresses=list(cand.addresses),
        port=cand.port,
        mac=mac,
        txt=dict(cand.txt),
    )
    errors = []
    reached = ""
    for addr in cand.addresses:
        why = await _confirm(_base_url(addr, cand.port), factory)
        if not why:
            reached = addr
            break
        errors.append(f"{addr}: {why}")
    if not reached:
        p.error = "; ".join(errors) or "no address advertised"
        return p
    model = (cand.txt.get("mux") or "").strip().lower()

    if product == PRODUCT_BRIDGE:
        host: dict[str, Any] | None = None
        if mac:
            for h in hosts:
                caps = capabilities_from_json(h.get("bridge_caps_json"))
                if caps is not None and caps.mac == mac:
                    host = h
                    break
        if host is None:
            p.action = "needs_bridge_host"
            p.note = (
                f"no bridge host with base MAC {mac or '(none advertised)'} yet: apply the "
                "bridge's own proposal first (its capabilities give the MAC), then rediscover"
            )
            return p
        host_id = str(host["id"])
        p.bridge_host_id = host_id
        iface = f"{BRIDGE_PREFIX}{host_id}"
        bridge_addrs = {_short(str(host.get("addr") or "")), _url_host(host.get("api_url"))}
        for a in auxes:
            a_iface = str(a.get("interface") or "")
            if a_iface == iface or (
                a_iface.startswith("http") and _short(_url_host(a_iface)) in bridge_addrs
            ):
                p.action = "known"
                p.matched_aux_id = str(a["id"])
                p.match_reason = f"aux interface {a_iface}"
                return p
        p.action = "link"
        p.match_reason = f"bridge host {host_id} (base MAC {mac})"
        p.aux = {
            "id": f"mux-{host_id}",
            "kind": AUX_KIND,
            "model": model or "adg729",
            "interface": iface,
            "capabilities": ["i2c-strand-mux"],
            "pool": "public",
            "status": "available",
        }
        return p

    # standalone mux box
    seen = {a.lower() for a in cand.addresses}
    name = _short(cand.hostname) if cand.hostname else ""
    for a in auxes:
        if mac and normalize_mac(a.get("discovered_mac")) == mac:
            p.action, p.matched_aux_id, p.match_reason = "known", str(a["id"]), f"MAC {mac}"
            return p
    for a in auxes:
        target = _url_host(str(a.get("interface") or ""))
        if target and (target in seen or (name and _short(target) == name)):
            p.action, p.matched_aux_id = "known", str(a["id"])
            p.match_reason = f"address {target}"
            return p
    iface_host = reached
    if name:
        local = f"{name}.local"
        if await eub.resolve_ipv4(local):
            iface_host = local
        else:
            p.note = f"{local} does not resolve on this controller; suggesting the IP"
    p.action = "new"
    p.aux = {
        "id": _suggest_aux_id(cand, mac),
        "kind": AUX_KIND,
        "model": model or "adg729",
        "interface": _base_url(iface_host, cand.port),
        "capabilities": ["i2c-strand-mux"],
        "pool": "public",
        "status": "available",
        "discovered_mac": mac or None,
    }
    return p


async def discover_strand_muxes(
    db_path: str, records: list[MdnsRecord], *, mux_client_factory: Any = None
) -> list[dict[str, Any]]:
    """Proposals for every ``_i2c-strand._tcp`` answer. Read-only."""
    hosts = await bridge_hosts(db_path)
    auxes = await mux_auxes(db_path)
    out = []
    for cand in merge_records(strand_records(records)):
        prop = await build_strand_proposal(
            cand, hosts, auxes, mux_client_factory=mux_client_factory
        )
        if prop is not None:
            out.append(prop.view())
    return out


async def apply_strand_mux(
    db_path: str,
    *,
    interface: str,
    aux_id: str,
    model: str = "",
    mac: str = "",
    mux_client_factory: Any = None,
) -> dict[str, Any]:
    """Create one ``i2c_strand_mux`` aux after re-confirming ``GET /api/status``.

    ``interface`` is ``bridge:<host_id>`` (a bridge's built-in mux; the host must
    exist) or ``http(s)://…`` (a mux box). 409 when an aux already uses that
    interface or MAC, or the id is taken; 422 when it does not confirm. Never
    touches strands.
    """
    from hil_controller.db.connection import get_db
    from hil_controller.hosts.esp_bridge import resolve_aux_interface

    factory = mux_client_factory or _mux_client
    interface = interface.strip()
    aux_id = aux_id.strip()
    if not _AUX_ID_RE.match(aux_id):
        raise ApplyError(422, f"invalid aux id {aux_id!r}")
    if interface.startswith(BRIDGE_PREFIX):
        base, _token = await resolve_aux_interface(db_path, interface)
        if not base:
            raise ApplyError(422, f"{interface}: no such esp-usbip-bridge host")
    elif re.match(r"^https?://", interface):
        base = interface
    else:
        raise ApplyError(422, "interface must be bridge:<host_id> or http(s)://…")
    why = await _confirm(base, factory)
    if why:
        raise ApplyError(422, f"strand mux at {base} did not answer /api/status: {why}")
    mac = normalize_mac(mac)
    for a in await mux_auxes(db_path):
        if str(a.get("interface") or "") == interface:
            raise ApplyError(409, f"aux {a['id']!r} already uses {interface}")
        if mac and normalize_mac(a.get("discovered_mac")) == mac:
            raise ApplyError(409, f"aux {a['id']!r} is already this mux (MAC {mac})")
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT 1 FROM auxes WHERE id = ?", (aux_id,))
        if await cur.fetchone() is not None:
            raise ApplyError(409, f"aux id {aux_id!r} already exists")
        await db.execute(
            """INSERT INTO auxes (id, kind, model, capabilities_json, interface,
                                  observability, pool, status, discovered_mac)
               VALUES (?, ?, ?, ?, ?, 'none', 'public', 'available', ?)""",
            (
                aux_id,
                AUX_KIND,
                model.strip() or "adg729",
                json.dumps(["i2c-strand-mux"]),
                interface,
                mac or None,
            ),
        )
        await db.commit()
    return {"id": aux_id, "kind": AUX_KIND, "interface": interface, "discovered_mac": mac or None}


__all__ = [
    "StrandMuxProposal",
    "apply_strand_mux",
    "build_strand_proposal",
    "discover_strand_muxes",
    "strand_records",
]
