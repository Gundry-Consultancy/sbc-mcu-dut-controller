"""Find esp-usbip-bridges and I2C strand muxes over mDNS; turn them into proposals.

Discovery never changes the bench by itself. It produces **proposals** that an
operator (web UI ``/ui/twins/bridges``) or an agent (``/v1/bridges/*``) reviews
and applies one at a time — the same preview → apply discipline as the physical
twins inventory. It never adds, removes or disables a host on its own: the
controller's LAN link flaps and multicast does not cross Tailscale, so "not
seen right now" means nothing.

Flow (contract: esp-usbip-bridge ``docs/bridge-discovery-and-capabilities.md``):

1. Browse ``_esp-usbip._tcp`` (port 80, TXT ``product=esp-usbip-bridge``,
   ``api``, ``fw``, ``mac``, …), plus the DNS-SD subtype
   ``_esp-usbip-bridge._sub._usbip._tcp`` to catch bridges that only advertise
   USB/IP, plus ``_i2c-strand._tcp`` for strand muxes (see below) — python
   ``zeroconf`` (optional extra ``[discovery]``) when installed, else
   ``avahi-browse -rpt`` when on PATH, else discovery reports itself
   unavailable (add-by-address still works). A TXT ``product`` other than
   ``esp-usbip-bridge`` is not a bridge and is skipped.
2. Collapse the answers per bridge: one bridge advertises on every interface
   that is up (Ethernet and Wi-Fi), so it can show several addresses. Records
   are keyed by the TXT ``mac`` (base MAC), else the hostname.
3. Confirm each candidate with ``GET /api/capabilities`` — the TXT record is
   only a hint, the endpoint is the truth. Add-by-address does the same check;
   a 404 there means a pre-1.0 bridge, accepted when ``/api/info`` looks like one.
4. Match against existing hosts (base MAC from the capabilities cache, then
   hostname, then address) so a known bridge is shown as known, not new.

I2C strand muxes (``_i2c-strand._tcp``; TXT ``product``, ``api``, ``mux``,
``groups``, ``duts``, ``path``, ``mac``, ``caps``) speak the same strand-mux API
whether built into a bridge or a standalone box:

* ``product=esp-usbip-bridge``: the bridge's own mux. It is linked to the bridge
  host with the same base MAC as an aux whose interface is ``bridge:<host_id>``
  (sharing the host's URL and token) — never a second device.
* ``product=sbc-dut-analog-mux``: the standalone CircuitPython box, matched to an
  existing aux by MAC, then address; a new one is proposed at its ``.local``
  name when that resolves.

Strand routing and existing strands are never changed by discovery.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from hil_controller.adapters import esp_usbip_bridge as eub
from hil_controller.adapters.bridge_capabilities import (
    FEATURE_HUB_POWER,
    FEATURE_MUX,
    BridgeCapabilities,
    capabilities_from_json,
    fetch_capabilities,
    normalize_mac,
)
from hil_controller.adapters.esp_usbip_bridge import (
    EspBridgeError,
    EspUsbipBridgeClient,
    bridge_base_url,
)

log = logging.getLogger(__name__)

#: The bridge's own service (port 80).
SERVICE_TYPE = "_esp-usbip._tcp"
#: USB/IP service; bridges also tag it with the subtype below.
USBIP_TYPE = "_usbip._tcp"
USBIP_BRIDGE_SUBTYPE = f"_esp-usbip-bridge._sub.{USBIP_TYPE}"
#: I2C strand mux service (bridge built-in mux and the standalone mux box).
STRAND_TYPE = "_i2c-strand._tcp"
#: What is browsed. Subtype answers resolve as their base type (``_usbip._tcp``).
BROWSE_TYPES = (SERVICE_TYPE, USBIP_BRIDGE_SUBTYPE, STRAND_TYPE)
BASE_TYPES = (SERVICE_TYPE, USBIP_TYPE, STRAND_TYPE)
PRODUCT_BRIDGE = "esp-usbip-bridge"
PRODUCT_MUX_BOX = "sbc-dut-analog-mux"
TRANSPORT = "esp-usbip-bridge"
DEFAULT_BROWSE_S = 3.0
#: Short timeout for confirming a candidate: discovery pages must not hang on
#: an address that is advertised but not reachable from here.
CONFIRM_TIMEOUT_S = 5.0

_HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class MdnsRecord:
    """One resolved DNS-SD answer (one interface of one device, one service)."""

    instance: str
    hostname: str = ""
    addresses: list[str] = field(default_factory=list)
    port: int = 80
    txt: dict[str, str] = field(default_factory=dict)
    interface: str = ""
    #: Base service type (``_esp-usbip._tcp`` / ``_usbip._tcp`` / ``_i2c-strand._tcp``).
    service: str = SERVICE_TYPE

    @property
    def mac(self) -> str:
        return normalize_mac(self.txt.get("mac"))

    @property
    def product(self) -> str:
        return (self.txt.get("product") or "").strip().lower()


@dataclass
class BrowseResult:
    records: list[MdnsRecord]
    method: str  # "zeroconf" | "avahi-browse" | "unavailable"
    error: str = ""


# --------------------------------------------------------------------------- #
# avahi-browse -rpt parsing                                                   #
# --------------------------------------------------------------------------- #

_AVAHI_ESCAPE = re.compile(r"\\(\d{3}|.)")


def _avahi_unescape(text: str) -> str:
    """avahi -p escapes ``;``/``\\`` and non-printables as ``\\DDD`` (decimal)."""

    def repl(m: re.Match[str]) -> str:
        tok = m.group(1)
        return chr(int(tok)) if tok.isdigit() else tok

    return _AVAHI_ESCAPE.sub(repl, text)


def _split_avahi_fields(line: str) -> list[str]:
    """Split on unescaped ``;`` (avahi escapes literal ones as ``\\059``)."""
    out, buf, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf.append(line[i : i + 2])
            i += 2
            continue
        if ch == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def _parse_avahi_txt(field_text: str) -> dict[str, str]:
    """``"api=1.0" "fw=ac4fbed"`` → dict (quoted strings, space separated)."""
    txt: dict[str, str] = {}
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', field_text):
        item = _avahi_unescape(m.group(1))
        key, sep, value = item.partition("=")
        if key:
            txt[key.strip().lower()] = value if sep else ""
    return txt


def parse_avahi_browse(
    output: str, service_types: Iterable[str] = BASE_TYPES
) -> list[MdnsRecord]:
    """Parse ``avahi-browse -rpt <type>`` output into resolved records.

    Only ``=`` (resolved) lines of ``service_types`` count (a subtype browse
    answers with the base type); ``+``/``-`` lines and other services are
    ignored. Each resolved line is one interface/protocol of one instance; use
    :func:`merge_records` to collapse them per device.
    """
    wanted = set(service_types)
    records: list[MdnsRecord] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line.startswith("="):
            continue
        fields = _split_avahi_fields(line)
        if len(fields) < 9:
            continue
        _, iface, _proto, name, stype, _domain, host, address, port = fields[:9]
        stype = _avahi_unescape(stype)
        if stype not in wanted:
            continue
        txt = _parse_avahi_txt(";".join(fields[9:])) if len(fields) > 9 else {}
        try:
            port_n = int(port)
        except ValueError:
            port_n = 80
        records.append(
            MdnsRecord(
                instance=_avahi_unescape(name),
                hostname=_avahi_unescape(host).removesuffix("."),
                addresses=[address] if address else [],
                port=port_n,
                txt=txt,
                interface=iface,
                service=stype,
            )
        )
    return records


async def _browse_avahi(timeout_s: float) -> BrowseResult:
    exe = shutil.which("avahi-browse")
    if not exe:
        return BrowseResult([], "unavailable", "avahi-browse not on PATH")
    records: list[MdnsRecord] = []
    errors: list[str] = []
    for stype in BROWSE_TYPES:
        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                "-rpt",
                stype,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=max(timeout_s, 1.0) + 10
            )
        except (TimeoutError, OSError) as exc:
            errors.append(f"avahi-browse {stype} failed: {exc}")
            continue
        if proc.returncode != 0:
            errors.append(f"avahi-browse {stype} exit {proc.returncode}: {err.decode()[:200]}")
            continue
        records += parse_avahi_browse(out.decode("utf-8", errors="replace"))
    return BrowseResult(records, "avahi-browse", "; ".join(errors))


# --------------------------------------------------------------------------- #
# python-zeroconf                                                             #
# --------------------------------------------------------------------------- #


def zeroconf_available() -> bool:
    try:
        import zeroconf  # noqa: F401
    except ImportError:
        return False
    return True


def _base_type_of(name: str) -> str:
    """``inst._usbip._tcp.local.`` → ``_usbip._tcp`` (one of :data:`BASE_TYPES`)."""
    for base in BASE_TYPES:
        if name.endswith(f".{base}.local.") or name.endswith(f".{base}.local"):
            return base
    return ""


def record_from_service_info(info: Any, service: str = "") -> MdnsRecord:
    """Build a record from a resolved ``zeroconf.ServiceInfo`` (duck-typed for tests)."""
    txt: dict[str, str] = {}
    for key, value in (getattr(info, "properties", None) or {}).items():
        k = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
        if value is None:
            v = ""
        elif isinstance(value, bytes):
            v = value.decode("utf-8", errors="replace")
        else:
            v = str(value)
        txt[k.strip().lower()] = v
    name = str(getattr(info, "name", "") or "")
    service = service or _base_type_of(name) or SERVICE_TYPE
    instance = name.removesuffix(f".{service}.local.").removesuffix(f".{service}.local")
    addresses: list[str] = []
    parsed = getattr(info, "parsed_addresses", None)
    if callable(parsed):
        addresses = [str(a) for a in parsed()]
    return MdnsRecord(
        instance=instance,
        hostname=str(getattr(info, "server", "") or "").removesuffix("."),
        addresses=addresses,
        port=int(getattr(info, "port", 80) or 80),
        txt=txt,
        service=service,
    )


async def _browse_zeroconf(timeout_s: float) -> BrowseResult:
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    names: set[str] = set()

    def on_change(zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            names.add(name)

    azc = AsyncZeroconf()
    try:
        browser = AsyncServiceBrowser(
            azc.zeroconf, [f"{t}.local." for t in BROWSE_TYPES], handlers=[on_change]
        )
        await asyncio.sleep(timeout_s)
        await browser.async_cancel()
        records = []
        for name in sorted(names):
            base = _base_type_of(name)  # a subtype answer resolves as its base type
            if not base:
                continue
            info = AsyncServiceInfo(f"{base}.local.", name)
            if await info.async_request(azc.zeroconf, 3000):
                records.append(record_from_service_info(info, base))
        return BrowseResult(records, "zeroconf")
    finally:
        await azc.async_close()


async def browse(timeout_s: float = DEFAULT_BROWSE_S) -> BrowseResult:
    """Browse :data:`BROWSE_TYPES` with whatever this controller has. Never raises."""
    if zeroconf_available():
        try:
            return await _browse_zeroconf(timeout_s)
        except Exception as exc:  # noqa: BLE001 - fall back to avahi, then report
            log.warning("zeroconf browse failed: %s", exc)
            fallback = await _browse_avahi(timeout_s)
            if fallback.method != "unavailable":
                return fallback
            return BrowseResult([], "zeroconf", f"zeroconf browse failed: {exc}")
    result = await _browse_avahi(timeout_s)
    if result.method == "unavailable":
        result.error = (
            "mDNS discovery unavailable: install the controller's [discovery] extra "
            "(python zeroconf) or avahi-utils (avahi-browse). Add bridges by address instead."
        )
    return result


# --------------------------------------------------------------------------- #
# merge, confirm, match                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class Candidate:
    """One bridge, with every address it was seen at."""

    key: str  # base MAC when known, else hostname/instance/address
    instance: str = ""
    hostname: str = ""
    addresses: list[str] = field(default_factory=list)
    port: int = 80
    txt: dict[str, str] = field(default_factory=dict)
    source: str = "mdns"  # "mdns" | "manual"
    services: list[str] = field(default_factory=list)


def _is_ipv4(addr: str) -> bool:
    parts = addr.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _host_key(rec: MdnsRecord) -> str:
    name = (rec.hostname or rec.instance).lower().removesuffix(".").removesuffix(".local")
    return name or (rec.addresses[0] if rec.addresses else "")


def bridge_records(records: Iterable[MdnsRecord]) -> list[MdnsRecord]:
    """Answers that may be bridges: ``_esp-usbip._tcp`` and the tagged
    ``_usbip._tcp``, minus anything whose TXT ``product`` names another product."""
    return [
        r
        for r in records
        if r.service in (SERVICE_TYPE, USBIP_TYPE) and r.product in ("", PRODUCT_BRIDGE)
    ]


def merge_records(records: Iterable[MdnsRecord]) -> list[Candidate]:
    """Collapse per-interface / per-service answers into one candidate per device.

    Keyed by base MAC (TXT ``mac``). An answer without a MAC (e.g. the
    ``_usbip._tcp`` TXT carries none) joins the candidate with the same
    hostname, else is keyed by hostname. ``_esp-usbip._tcp`` answers are taken
    first so their TXT (product, api, fw, mac) wins.
    """
    ordered = sorted(records, key=lambda r: (r.service != SERVICE_TYPE, not r.mac))
    by_key: dict[str, Candidate] = {}
    by_host: dict[str, str] = {}
    for rec in ordered:
        hkey = _host_key(rec)
        key = rec.mac or by_host.get(hkey) or hkey
        if not key:
            continue
        if hkey and hkey not in by_host:
            by_host[hkey] = key
        cand = by_key.get(key)
        if cand is None:
            cand = by_key[key] = Candidate(
                key=key,
                instance=rec.instance,
                hostname=rec.hostname,
                port=rec.port,
                txt=dict(rec.txt),
            )
        for addr in rec.addresses:
            if addr and addr not in cand.addresses:
                cand.addresses.append(addr)
        if rec.service not in cand.services:
            cand.services.append(rec.service)
        if rec.service == cand_service(cand):
            cand.txt.update({k: v for k, v in rec.txt.items() if k not in cand.txt})
    # IPv4 first (stable order), link-local IPv6 last: USB/IP attach wants v4.
    for cand in by_key.values():
        cand.addresses.sort(key=lambda a: (not _is_ipv4(a), a.startswith("fe80"), a))
    return sorted(by_key.values(), key=lambda c: c.key)


def cand_service(cand: Candidate) -> str:
    """The service whose TXT describes the candidate (the first one seen)."""
    return cand.services[0] if cand.services else ""


def _url_host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlsplit(url if "://" in url else f"http://{url}").hostname or "").lower()
    except ValueError:
        return ""


def _short(name: str) -> str:
    return name.lower().removesuffix(".").removesuffix(".local")


def match_existing(
    *,
    mac: str,
    hostname: str,
    addresses: list[str],
    hosts: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Find the existing bridge host for a candidate: ``(host_row, reason)``.

    Order: base MAC (from each host's cached capabilities), then hostname
    (against the host id, its addr, or its cached hostname), then address.
    """
    mac = normalize_mac(mac)
    short = _short(hostname) if hostname else ""
    addr_set = {a.lower() for a in addresses if a}
    if mac:
        for h in hosts:
            caps = capabilities_from_json(h.get("bridge_caps_json"))
            if caps is not None and caps.mac and caps.mac == mac:
                return h, f"base MAC {mac}"
    if short:
        for h in hosts:
            caps = capabilities_from_json(h.get("bridge_caps_json"))
            names = {
                _short(str(h.get("id") or "")),
                _short(str(h.get("addr") or "")),
                _short(_url_host(h.get("api_url"))),
                _short(caps.hostname) if caps and caps.hostname else "",
            }
            if short in names:
                return h, f"hostname {short}"
    if addr_set:
        for h in hosts:
            for a in (str(h.get("addr") or "").lower(), _url_host(h.get("api_url"))):
                if a and a in addr_set:
                    return h, f"address {a}"
    return None, ""


def suggested_capability_tags(caps: BridgeCapabilities) -> list[str]:
    """Scheduler tags a new bridge host starts with (operator can edit before apply)."""
    tags = ["usbip-server"]
    if caps.has(FEATURE_HUB_POWER):
        tags.append("bridge-port-power")
    if caps.has(FEATURE_MUX):
        tags.append("i2c-strand-mux")
    return tags


def suggest_host_id(caps: BridgeCapabilities, fallback: str = "") -> str:
    for candidate in (caps.hostname, fallback):
        name = _short(candidate or "")
        if name and _HOST_ID_RE.match(name):
            return name
    mac = caps.mac.replace(":", "")
    return f"usbip-{mac[-6:]}" if mac else ""


def valid_host_id(host_id: str) -> bool:
    return bool(_HOST_ID_RE.match(host_id or ""))


@dataclass
class BridgeProposal:
    """What applying would do for one bridge. ``action``:

    * ``new`` — confirmed bridge not in the hosts table: apply creates a host.
      ``address`` is ``<hostname>.local`` when that name resolves on the
      controller (bridges are addressed by mDNS name so DHCP leases do not
      matter), else the IP; ``ip`` is informational either way.
    * ``known`` — matches an existing host whose address still reaches it. A
      host addressed by name is known as long as the name resolves to this
      bridge (same base MAC), whatever IP it has today.
    * ``moved`` — matches an existing host whose address no longer reaches it
      (``move_reason``: an IP it does not answer at, a name that stopped
      resolving, or a name now answered by a different MAC). Shown for the
      operator to edit the host; never changed automatically.
    * ``unconfirmed`` — advertised/typed, but the capabilities check failed.
    """

    action: str
    key: str
    addresses: list[str]
    address: str = ""
    source: str = "mdns"
    hostname: str = ""
    txt: dict[str, str] = field(default_factory=dict)
    caps: BridgeCapabilities | None = None
    error: str = ""
    matched_host_id: str | None = None
    match_reason: str = ""
    host: dict[str, Any] = field(default_factory=dict)
    #: The IPv4 the bridge answered at (informational; hosts use the name).
    ip: str = ""
    #: ``<hostname>.local`` and whether the controller resolves it.
    mdns_name: str = ""
    name_resolves: bool | None = None
    move_reason: str = ""
    note: str = ""
    #: The bridge's previous hostname when it renamed itself (same base MAC).
    renamed_from: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "key": self.key,
            "address": self.address,
            "addresses": self.addresses,
            "source": self.source,
            "hostname": self.hostname,
            "txt": self.txt,
            "error": self.error or None,
            "matched_host_id": self.matched_host_id,
            "match_reason": self.match_reason or None,
            "capabilities": self.caps.summary() if self.caps else None,
            "host": self.host or None,
            "ip": self.ip or None,
            "mdns_name": self.mdns_name or None,
            "name_resolves": self.name_resolves,
            "move_reason": self.move_reason or None,
            "note": self.note or None,
            "renamed_from": self.renamed_from or None,
        }


def _client(address: str, token: str | None = None) -> EspUsbipBridgeClient:
    return EspUsbipBridgeClient(
        bridge_base_url(address), token=token, timeout_s=CONFIRM_TIMEOUT_S
    )


async def confirm_address(
    address: str, *, client: EspUsbipBridgeClient | None = None
) -> BridgeCapabilities:
    """Capabilities check for one address (raises :class:`EspBridgeError`)."""
    return await fetch_capabilities(client or _client(address))


def _host_target(host: dict[str, Any]) -> str:
    """The name/IP a host row points the controller at (api_url host wins)."""
    return (_url_host(host.get("api_url")) or str(host.get("addr") or "")).lower().rstrip(".")


async def _host_still_reaches(
    host: dict[str, Any],
    proposal: BridgeProposal,
    factory: Any,
) -> tuple[bool, str]:
    """Does the existing host's address still reach this bridge? ``(ok, why_not)``.

    An IP must be one the bridge answers at. A name (``usbip-s31-d19bee.local``) is
    fine whatever IP it resolves to today — DHCP leases are why bridges are
    addressed by mDNS name — unless it no longer resolves, or whoever answers
    it now is a different bridge (base MAC; hostname for pre-1.0 bridges).
    """
    target = _host_target(host)
    seen = {a.lower() for a in [*proposal.addresses, proposal.ip] if a}
    if not target:
        return False, "the host has no address"
    if target in seen:
        return True, ""
    if _is_ipv4(target) or ":" in target:
        answers = ", ".join(proposal.addresses)
        return False, f"host address {target} is not where the bridge answers ({answers})"
    ips = await eub.resolve_ipv4(target)
    if not ips:
        return False, f"{target} no longer resolves on the controller"
    if set(ips) & seen:
        return True, ""
    try:
        other = await confirm_address(target, client=factory(target))
    except EspBridgeError as exc:
        return False, f"{target} resolves to {ips[0]}, which does not answer as a bridge: {exc}"
    mine = proposal.caps
    if mine is not None and mine.mac and other.mac:
        if other.mac == mine.mac:
            return True, ""
        return False, f"{target} now resolves to a different bridge (base MAC {other.mac})"
    if mine is not None and other.hostname and other.hostname == mine.hostname:
        return True, ""
    return False, f"{target} resolves to {ips[0]}, which is not this bridge"


def _note_rename(
    proposal: BridgeProposal, host: dict[str, Any], caps: BridgeCapabilities
) -> None:
    """A bridge that renamed itself (same base MAC, new hostname) is still the
    same bridge: ``known``, never ``new`` or ``moved``. The naming scheme is not
    assumed anywhere — the old name comes from the host's cached capabilities or
    its ``.local`` address, the new one from the bridge itself.

    When the host is addressed by the old ``.local`` name (which stops resolving
    after a rename), the proposal says so and suggests the new name; the host is
    never edited automatically.
    """
    if not (caps.hostname and proposal.match_reason.startswith("base MAC")):
        return
    new = _short(caps.hostname)
    cached = capabilities_from_json(host.get("bridge_caps_json"))
    target = _host_target(host)
    old = _short(cached.hostname) if cached and cached.hostname else ""
    if not old and eub.is_mdns_name(target):
        old = _short(target)
    if not old or old == new:
        return
    proposal.renamed_from = old
    proposal.action = "known"
    if eub.is_mdns_name(target) and _short(target) == old:
        proposal.move_reason = ""
        proposal.address = f"{new}.local"
        proposal.note = (
            f"bridge renamed itself {old} -> {new} (same base MAC {caps.mac}); host "
            f"{host.get('id')} still points at {target}: edit its address to {new}.local"
        )
    else:
        proposal.note = f"bridge renamed itself {old} -> {new} (same base MAC {caps.mac})"


async def build_proposal(
    cand: Candidate,
    hosts: list[dict[str, Any]],
    *,
    client_factory: Any = None,
) -> BridgeProposal:
    """Confirm one candidate and classify it against the existing bridge hosts."""
    factory = client_factory or _client
    caps: BridgeCapabilities | None = None
    errors: list[str] = []
    address = ""
    for addr in cand.addresses:
        try:
            caps = await confirm_address(addr, client=factory(addr))
            address = addr
            break
        except EspBridgeError as exc:
            errors.append(f"{addr}: {exc}")
    proposal = BridgeProposal(
        action="unconfirmed",
        key=cand.key,
        addresses=list(cand.addresses),
        address=address,
        source=cand.source,
        hostname=cand.hostname,
        txt=dict(cand.txt),
        caps=caps,
        error="; ".join(errors) if caps is None else "",
    )
    mac = (caps.mac if caps else "") or normalize_mac(cand.txt.get("mac"))
    hostname = (caps.hostname if caps else "") or cand.hostname or cand.instance
    all_addrs = list(cand.addresses)
    if caps is not None:
        all_addrs += [ip for ip in caps.up_ips() if ip not in all_addrs]
    matched, reason = match_existing(
        mac=mac, hostname=hostname, addresses=all_addrs, hosts=hosts
    )
    if matched is not None:
        proposal.matched_host_id = str(matched.get("id"))
        proposal.match_reason = reason
    if caps is None:
        return proposal
    # The IP (informational): Ethernet first (primary transport), else where it
    # answered / was advertised.
    preferred = caps.eth_ip()
    if preferred and preferred not in proposal.addresses:
        proposal.addresses.insert(0, preferred)
    v4 = [a for a in proposal.addresses if _is_ipv4(a)]
    proposal.ip = preferred or (address if _is_ipv4(address) else (v4[0] if v4 else ""))
    for ip in caps.up_ips():
        if ip not in proposal.addresses:
            proposal.addresses.append(ip)
    if matched is not None:
        ok, why = await _host_still_reaches(matched, proposal, factory)
        proposal.action = "known" if ok else "moved"
        proposal.move_reason = why
        proposal.address = _host_target(matched) if ok else (proposal.ip or address)
        _note_rename(proposal, matched, caps)
        return proposal
    # Suggest the mDNS name, so the host survives DHCP lease changes.
    proposal.mdns_name = f"{_short(caps.hostname)}.local" if caps.hostname else ""
    if proposal.mdns_name:
        if address.lower().rstrip(".") == proposal.mdns_name:
            proposal.name_resolves = True  # we just reached it by that name
        else:
            proposal.name_resolves = bool(await eub.resolve_ipv4(proposal.mdns_name))
    if proposal.mdns_name and proposal.name_resolves:
        proposal.address = proposal.mdns_name
    else:
        proposal.address = proposal.ip or address
        if proposal.mdns_name:
            proposal.note = (
                f"{proposal.mdns_name} does not resolve on this controller (no nss-mdns?); "
                f"suggesting the IP {proposal.address}, which a DHCP lease change can move"
            )
    proposal.action = "new"
    proposal.host = {
        "id": suggest_host_id(caps, hostname),
        "role": "microcontroller-fleet",
        "transport": TRANSPORT,
        "addr": proposal.address,
        "api_url": None,
        "token_env": None,
        "capabilities": suggested_capability_tags(caps),
        "status": "available",
    }
    return proposal


async def bridge_hosts(db_path: str) -> list[dict[str, Any]]:
    from hil_controller.db.connection import get_db

    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT id, addr, api_url, token_env, status, bridge_caps_json FROM hosts "
            "WHERE transport = ?",
            (TRANSPORT,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def discover(
    db_path: str,
    *,
    timeout_s: float = DEFAULT_BROWSE_S,
    browse_result: BrowseResult | None = None,
    client_factory: Any = None,
    mux_client_factory: Any = None,
) -> dict[str, Any]:
    """Browse + confirm + match bridges and strand muxes. Read-only: writes nothing."""
    from hil_controller.strand_mux_discovery import discover_strand_muxes

    result = browse_result or await browse(timeout_s)
    hosts = await bridge_hosts(db_path)
    proposals = [
        await build_proposal(c, hosts, client_factory=client_factory)
        for c in merge_records(bridge_records(result.records))
    ]
    strand_muxes = await discover_strand_muxes(
        db_path, result.records, mux_client_factory=mux_client_factory
    )
    seen = {p.matched_host_id for p in proposals if p.matched_host_id}
    return {
        "method": result.method,
        "error": result.error or None,
        "proposals": [p.view() for p in proposals],
        "strand_muxes": strand_muxes,
        # Hosts not seen are listed, never touched: mDNS does not cross Tailscale
        # and the controller's LAN link flaps.
        "not_seen": sorted(str(h["id"]) for h in hosts if h["id"] not in seen),
    }


async def probe_address(
    db_path: str, address: str, *, client_factory: Any = None
) -> BridgeProposal:
    """Add-by-address: the same capabilities check + matching as a discovered bridge."""
    address = address.strip()
    host = _url_host(address) or address
    cand = Candidate(key=host.lower(), hostname="", addresses=[host], source="manual")
    return await build_proposal(cand, await bridge_hosts(db_path), client_factory=client_factory)


class ApplyError(ValueError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def apply_proposal(
    db_path: str,
    address: str,
    *,
    host_id: str = "",
    token_env: str = "",
    role: str = "",
    capabilities: list[str] | None = None,
    client_factory: Any = None,
) -> dict[str, Any]:
    """Create the host for a ``new`` proposal, after re-running the capabilities check.

    Refuses (409) when the bridge already matches a host or the id is taken, and
    (422) when the address does not confirm as a bridge. Caches the capabilities
    on the new row. Returns the created host row.
    """
    import json

    from hil_controller.adapters.bridge_capabilities import store_capabilities
    from hil_controller.db.connection import get_db

    proposal = await probe_address(db_path, address, client_factory=client_factory)
    if proposal.action == "unconfirmed" or proposal.caps is None:
        raise ApplyError(422, f"{address} did not confirm as a bridge: {proposal.error}")
    if proposal.matched_host_id:
        raise ApplyError(
            409,
            f"this bridge is already host {proposal.matched_host_id!r} "
            f"({proposal.match_reason}); edit that host instead",
        )
    host = dict(proposal.host)
    if host_id.strip():
        host["id"] = host_id.strip()
    if not valid_host_id(host["id"]):
        raise ApplyError(422, f"invalid host id {host['id']!r}")
    if token_env.strip():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env.strip()):
            raise ApplyError(
                422, "token_env must be an environment variable NAME, never the token itself"
            )
        host["token_env"] = token_env.strip()
    if role.strip():
        host["role"] = role.strip()
    if capabilities is not None:
        host["capabilities"] = [c.strip() for c in capabilities if c.strip()]
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT 1 FROM hosts WHERE id = ?", (host["id"],))
        if await cur.fetchone() is not None:
            raise ApplyError(409, f"host id {host['id']!r} already exists")
        await db.execute(
            """INSERT INTO hosts
               (id, role, addr, transport, ssh_user, max_concurrent_jobs,
                capabilities_json, status, api_url, token_env)
               VALUES (?, ?, ?, ?, 'pi', NULL, ?, 'available', NULL, ?)""",
            (
                host["id"],
                host["role"],
                host["addr"],
                TRANSPORT,
                json.dumps(host["capabilities"]),
                host.get("token_env"),
            ),
        )
        await db.commit()
    await store_capabilities(db_path, host["id"], proposal.caps)
    return host
