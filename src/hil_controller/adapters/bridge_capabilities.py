"""Bridge capabilities: parse, gate on feature versions, cache per bridge host.

Contract (esp-usbip-bridge ``docs/bridge-discovery-and-capabilities.md``):

* ``GET /api/capabilities`` (no auth) answers ``{"schema": 1, "api_version":
  "MAJOR.MINOR", "fw": {...}, "board": {...}, "network": {...}, "ports": {...},
  "auth": {...}, "limits": {...}, "features": {name: {"version": int, ...}}}``.
* A bridge that answers **404** there is **pre-1.0**: it has only ``/api/info``
  and the USB device listing, and no other feature may be assumed — whatever its
  firmware commit happens to be.
* Clients ask "is ``flash_esp`` at least 1?", never "is the firmware newer than
  <sha>?". A feature that carries ``"available": false`` is compiled in but not
  usable right now (e.g. ``mux`` with no ADG729 on the bus) and gates as absent.
* Unknown fields and features are ignored. MINOR bumps add things; a MAJOR this
  controller was not written for (anything but 1) gates every feature off, so a
  breaking firmware change can never be driven by stale assumptions.

The cache lives on the ``hosts`` row (``bridge_caps_json``, ``bridge_caps_at`` =
last good fetch, ``bridge_caps_checked_at`` = last attempt, ``bridge_caps_error``),
runtime only — the topology seeder never writes it, so it causes no drift. A
failed fetch keeps the last good document (marked stale with the error); it never
clears it and never changes the host's status.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hil_controller.adapters.esp_usbip_bridge import (
    EspBridgeError,
    EspBridgeNotFoundError,
    EspUsbipBridgeClient,
)

log = logging.getLogger(__name__)

#: The capabilities API major version this controller is written against.
SUPPORTED_API_MAJOR = 1

#: Cached documents younger than this are used as is by pages and gates.
DEFAULT_MAX_AGE_S = 300.0

# Feature names (stable identifiers in the contract; never renamed by the bridge).
FEATURE_USBIP = "usbip"
FEATURE_CLAIMS = "claims"
FEATURE_HUB_POWER = "hub_power"
FEATURE_MUX = "mux"
FEATURE_DEVICE_INFO = "device_info"
FEATURE_SERIAL_LINES = "serial_lines"
FEATURE_RESET = "reset"
FEATURE_CONSOLE = "console"
FEATURE_CAPTURE = "capture"
FEATURE_FLASH_ESP = "flash_esp"
FEATURE_FLASH_PICOBOOT = "flash_picoboot"
FEATURE_FLASH_SAMBA = "flash_samba"
FEATURE_FLASH_UF2 = "flash_uf2"
FEATURE_MSC = "msc"
FEATURE_OTA = "ota"
FEATURE_WIFI_CONFIG = "wifi_config"


def normalize_mac(value: Any) -> str:
    """``1C-29-04-D1-9B-EE`` / ``1c2904d19bee`` → ``1c:29:04:d1:9b:ee``; '' if not a MAC."""
    text = str(value or "").strip().lower().replace("-", ":")
    hexdigits = text.replace(":", "")
    if len(hexdigits) != 12 or any(c not in "0123456789abcdef" for c in hexdigits):
        return ""
    return ":".join(hexdigits[i : i + 2] for i in range(0, 12, 2))


def _parse_api_version(value: Any) -> tuple[int, int] | None:
    try:
        major, _, minor = str(value or "").strip().partition(".")
        return int(major), int(minor or 0)
    except ValueError:
        return None


@dataclass
class BridgeCapabilities:
    """One bridge's capabilities, or the pre-1.0 fallback built from ``/api/info``."""

    #: ``"1.0"``…; ``None`` for a pre-1.0 bridge (``/api/capabilities`` → 404).
    api_version: str | None
    fw: dict[str, Any] = field(default_factory=dict)
    board: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=dict)
    ports: dict[str, Any] = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    features: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``/api/info`` (always fetched for pre-1.0; kept for display).
    info: dict[str, Any] = field(default_factory=dict)
    schema: int | None = None

    # ------------------------------------------------------------------ #
    @property
    def pre_1_0(self) -> bool:
        return self.api_version is None

    @property
    def api_major_minor(self) -> tuple[int, int] | None:
        return _parse_api_version(self.api_version) if self.api_version else None

    @property
    def compatible(self) -> bool:
        """True when the API major is one this controller understands."""
        mm = self.api_major_minor
        return mm is not None and mm[0] == SUPPORTED_API_MAJOR

    @property
    def fw_version(self) -> str:
        return str(self.fw.get("version") or self.info.get("version") or "")

    @property
    def target(self) -> str:
        return str(self.board.get("target") or self.info.get("target") or "")

    @property
    def hostname(self) -> str:
        return str(self.board.get("hostname") or self.info.get("hostname") or "")

    @property
    def board_profile(self) -> str:
        return str(self.board.get("profile") or "")

    @property
    def mac(self) -> str:
        """Base MAC (``board.mac``), normalized; '' when unknown (always for pre-1.0)."""
        return normalize_mac(self.board.get("mac"))

    @property
    def auth_required(self) -> bool:
        if "required" in self.auth:
            return bool(self.auth.get("required"))
        return bool(self.info.get("auth_required"))

    def feature_version(self, name: str) -> int:
        """The feature's integer version; 0 = absent, unavailable, or not gateable.

        Pre-1.0 bridges and bridges on an unsupported API major gate everything
        off. ``available: false`` counts as absent.
        """
        if not self.compatible:
            return 0
        feat = self.features.get(name)
        if not isinstance(feat, dict):
            return 0
        if feat.get("available") is False:
            return 0
        try:
            return int(feat.get("version") or 0)
        except (TypeError, ValueError):
            return 0

    def has(self, name: str, min_version: int = 1) -> bool:
        return self.feature_version(name) >= min_version

    def reset_modes(self) -> list[str]:
        if not self.has(FEATURE_RESET):
            return []
        modes = (self.features.get(FEATURE_RESET) or {}).get("modes") or []
        return [str(m) for m in modes if isinstance(m, str)]

    def staging_bytes(self) -> int | None:
        value = self.limits.get("rescue_staging_bytes")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def eth_ip(self) -> str:
        """The up Ethernet interface's IP (preferred bridge address), else ''."""
        for iface in self.network.get("interfaces") or []:
            if isinstance(iface, dict) and iface.get("up") and iface.get("ip"):
                if str(iface.get("name") or "").startswith("eth"):
                    return str(iface["ip"])
        return ""

    def up_ips(self) -> list[str]:
        out = []
        for iface in self.network.get("interfaces") or []:
            if isinstance(iface, dict) and iface.get("up") and iface.get("ip"):
                out.append(str(iface["ip"]))
        return out

    def summary(self) -> dict[str, Any]:
        """Display/API view: what pages show and ``/v1/bridges`` returns."""
        feats = {}
        for name, feat in sorted(self.features.items()):
            if not isinstance(feat, dict):
                continue
            entry: dict[str, Any] = {"version": feat.get("version")}
            if "available" in feat:
                entry["available"] = feat.get("available")
            for extra in ("modes", "filesystems"):
                if extra in feat:
                    entry[extra] = feat.get(extra)
            entry["usable"] = self.has(name)
            feats[name] = entry
        return {
            "api_version": self.api_version,
            "pre_1_0": self.pre_1_0,
            "compatible": self.compatible,
            "fw_version": self.fw_version,
            "fw": self.fw,
            "target": self.target,
            "board_profile": self.board_profile,
            "hostname": self.hostname,
            "mac": self.mac,
            "auth_required": self.auth_required,
            "network": self.network,
            "limits": self.limits,
            "features": feats,
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": self.schema,
                "api_version": self.api_version,
                "fw": self.fw,
                "board": self.board,
                "network": self.network,
                "ports": self.ports,
                "auth": self.auth,
                "limits": self.limits,
                "features": self.features,
                "info": self.info,
            },
            sort_keys=True,
        )


def _obj(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def parse_capabilities(doc: Any, *, info: dict[str, Any] | None = None) -> BridgeCapabilities:
    """Parse a ``GET /api/capabilities`` body (unknown keys ignored)."""
    if not isinstance(doc, dict):
        raise ValueError("capabilities document is not a JSON object")
    api_version = str(doc.get("api_version") or "").strip()
    if _parse_api_version(api_version) is None:
        raise ValueError(f"capabilities document has no valid api_version ({api_version!r})")
    features = {
        str(k): dict(v) for k, v in _obj(doc.get("features")).items() if isinstance(v, dict)
    }
    schema = doc.get("schema")
    return BridgeCapabilities(
        api_version=api_version,
        fw=_obj(doc.get("fw")),
        board=_obj(doc.get("board")),
        network=_obj(doc.get("network")),
        ports=_obj(doc.get("ports")),
        auth=_obj(doc.get("auth")),
        limits=_obj(doc.get("limits")),
        features=features,
        info=dict(info or {}),
        schema=int(schema) if isinstance(schema, int) else None,
    )


def pre_1_0_capabilities(info: dict[str, Any]) -> BridgeCapabilities:
    """A pre-1.0 bridge: ``/api/info`` + the USB device listing, no features."""
    return BridgeCapabilities(api_version=None, info=dict(info or {}))


def capabilities_from_json(text: str | None) -> BridgeCapabilities | None:
    """Rebuild from the cached ``to_json`` form; None for empty/corrupt cache."""
    if not text:
        return None
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    info = _obj(doc.get("info"))
    if not doc.get("api_version"):
        return pre_1_0_capabilities(info)
    try:
        return parse_capabilities(doc, info=info)
    except ValueError:
        return None


def looks_like_bridge_info(info: Any) -> bool:
    """A pre-1.0 ``/api/info`` answer from an esp-usbip-bridge (not any web server)."""
    return (
        isinstance(info, dict)
        and bool(info.get("target") or info.get("hostname"))
        and ("usbip_port" in info or "version" in info)
    )


async def fetch_capabilities(client: EspUsbipBridgeClient) -> BridgeCapabilities:
    """``GET /api/capabilities``; on 404 fall back to ``/api/info`` as pre-1.0.

    Raises :class:`EspBridgeError` when the bridge is unreachable, answers an
    error other than 404, or the pre-1.0 ``/api/info`` does not look like a bridge.
    """
    doc = await client.capabilities()
    if doc is not None:
        try:
            info = await client.info()
        except EspBridgeError:
            info = {}
        try:
            return parse_capabilities(doc, info=info)
        except ValueError as exc:
            raise EspBridgeError(f"{client.base_url}: {exc}") from exc
    try:
        info = await client.info()
    except EspBridgeNotFoundError as exc:
        raise EspBridgeError(
            f"{client.base_url} has neither /api/capabilities nor /api/info: not a bridge"
        ) from exc
    if not looks_like_bridge_info(info):
        raise EspBridgeError(f"{client.base_url}/api/info does not look like an esp-usbip-bridge")
    return pre_1_0_capabilities(info)


# --------------------------------------------------------------------------- #
# per-host cache (hosts.bridge_caps_json / bridge_caps_at / bridge_caps_error) #
# --------------------------------------------------------------------------- #


@dataclass
class CachedCapabilities:
    caps: BridgeCapabilities | None
    fetched_at: str | None
    error: str | None
    checked_at: str | None = None

    @property
    def stale(self) -> bool:
        """True when the last fetch failed (``caps`` is the last good value, if any)."""
        return bool(self.error)

    def age_s(self, now: datetime | None = None) -> float | None:
        """Seconds since the last fetch *attempt* (success or failure)."""
        stamp = self.checked_at or self.fetched_at
        if not stamp:
            return None
        try:
            then = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        return ((now or datetime.now(UTC)) - then).total_seconds()

    def view(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "checked_at": self.checked_at,
            "stale": self.stale,
            "error": self.error,
            **(self.caps.summary() if self.caps else {"api_version": None, "unknown": True}),
        }


async def load_cached(db_path: str, host_id: str) -> CachedCapabilities:
    from hil_controller.db.connection import get_db

    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT bridge_caps_json, bridge_caps_at, bridge_caps_checked_at, bridge_caps_error "
            "FROM hosts WHERE id = ?",
            (host_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return CachedCapabilities(None, None, None)
    return CachedCapabilities(
        capabilities_from_json(row["bridge_caps_json"]),
        row["bridge_caps_at"],
        row["bridge_caps_error"],
        row["bridge_caps_checked_at"],
    )


async def store_capabilities(
    db_path: str, host_id: str, caps: BridgeCapabilities | None, *, error: str | None = None
) -> None:
    """Store a fresh document, or (``caps=None``) record a failed fetch, keeping the old one."""
    from hil_controller.db.connection import get_db

    now = datetime.now(UTC).isoformat()
    async with get_db(db_path) as db:
        if caps is not None:
            await db.execute(
                "UPDATE hosts SET bridge_caps_json = ?, bridge_caps_at = ?, "
                "bridge_caps_checked_at = ?, bridge_caps_error = NULL WHERE id = ?",
                (caps.to_json(), now, now, host_id),
            )
        else:
            await db.execute(
                "UPDATE hosts SET bridge_caps_checked_at = ?, bridge_caps_error = ? WHERE id = ?",
                (now, error or "fetch failed", host_id),
            )
        await db.commit()


async def refresh_capabilities(
    db_path: str, host_id: str, client: EspUsbipBridgeClient
) -> CachedCapabilities:
    """Fetch now and update the cache. Never raises for bridge errors."""
    try:
        caps = await fetch_capabilities(client)
    except EspBridgeError as exc:
        log.info("bridge %s capabilities fetch failed: %s", host_id, exc)
        await store_capabilities(db_path, host_id, None, error=str(exc))
        return await load_cached(db_path, host_id)
    await store_capabilities(db_path, host_id, caps)
    return await load_cached(db_path, host_id)


async def get_capabilities(
    db_path: str,
    host_id: str,
    client: EspUsbipBridgeClient | None,
    *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    refresh: bool = False,
) -> CachedCapabilities:
    """The cached document, refreshed first when the last attempt is older than
    ``max_age_s`` (or ``refresh``). A recent failed attempt is not retried on
    every page view: the stale value (and error) is returned until it ages out."""
    cached = await load_cached(db_path, host_id)
    if client is None:
        return cached
    age = cached.age_s()
    if refresh or age is None or age > max_age_s:
        return await refresh_capabilities(db_path, host_id, client)
    return cached
