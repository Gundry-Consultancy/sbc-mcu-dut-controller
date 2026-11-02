"""On-bridge device rescue: drive a bridge-hosted DUT through its bridge's own API.

A 1.x bridge can interrogate, reset, peek the console of and flash the devices on
its USB host itself (esp-usbip-bridge README, "Device rescue" and "Flashing,
capture and mass storage without host tools"), so the controller does not have
to ``usbip attach`` the DUT and run esptool/picotool/bossac locally::

    GET  /api/usb/devices/{busid}/rescue            identity, serial function, reset modes
    POST /api/usb/devices/{busid}/reset             {"mode", "port"?, "wait_ms"?}
    POST /api/usb/devices/{busid}/console           {"serial_baud", "pokes", "capture_s", ...}
    PUT  /api/rescue/files/{name}                   raw bytes (staged in PSRAM)
    POST /api/usb/devices/{busid}/flash/esp         {"images": [{"file", "offset"}], ...} -> 202 job
    POST /api/usb/devices/{busid}/esp/info          -> 202 job
    POST /api/usb/devices/{busid}/flash/picoboot    {"file", ...} -> 202 job
    POST /api/usb/devices/{busid}/picoboot/info     -> 202 job
    POST /api/usb/devices/{busid}/flash/samba       {"file", "offset"?, ...} -> 202 job
    POST /api/usb/devices/{busid}/samba/info        -> 202 job
    POST /api/usb/devices/{busid}/flash/uf2         {"file", "path"?, "method"?} -> 202 job
    GET  /api/rescue/jobs/{id}                      state queued|running|done|failed, log, result

Every action is gated on the bridge's **feature versions** from
``GET /api/capabilities`` (:mod:`hil_controller.adapters.bridge_capabilities`),
never on its firmware commit: a pre-1.0 bridge gets none of this, whatever it runs.

Rules the bridge enforces and this module respects:

* USB/IP and rescue exclude each other: every rescue POST is refused (409) while a
  USB/IP client has the device imported — so a job that uses these stages never
  attaches the DUT (``FirmwareBenchAdapter`` skips the attach).
* The bridge HTTP server handles one request at a time; a console peek blocks it
  for up to 12 s and a reset waits up to 10 s, so those calls get longer timeouts.
* Rescue never changes hub port power.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hil_controller.adapters.bridge_capabilities import (
    FEATURE_CONSOLE,
    FEATURE_DEVICE_INFO,
    FEATURE_FLASH_ESP,
    FEATURE_FLASH_PICOBOOT,
    FEATURE_FLASH_SAMBA,
    FEATURE_FLASH_UF2,
    FEATURE_RESET,
    BridgeCapabilities,
)
from hil_controller.adapters.esp_usbip_bridge import EspBridgeError, EspUsbipBridgeClient

#: Console peek: pokes x settle_s + capture_s <= 12 s on the bridge; leave headroom.
CONSOLE_TIMEOUT_S = 20.0
#: Reset waits at most 10 s for re-enumeration on the bridge.
RESET_TIMEOUT_S = 18.0
#: Uploading a multi-MB image to PSRAM over 100 Mbit Ethernet.
UPLOAD_TIMEOUT_S = 120.0
DEFAULT_JOB_TIMEOUT_S = 600.0

#: action -> (feature, min version). The single source of truth for gating.
ACTION_FEATURES: dict[str, tuple[str, int]] = {
    "info": (FEATURE_DEVICE_INFO, 1),
    "console": (FEATURE_CONSOLE, 1),
    "reset": (FEATURE_RESET, 1),
    "esp_info": (FEATURE_FLASH_ESP, 1),
    "esp_flash": (FEATURE_FLASH_ESP, 1),
    "picoboot_info": (FEATURE_FLASH_PICOBOOT, 1),
    "picoboot_load": (FEATURE_FLASH_PICOBOOT, 1),
    "samba_info": (FEATURE_FLASH_SAMBA, 1),
    "samba_flash": (FEATURE_FLASH_SAMBA, 1),
    "uf2_copy": (FEATURE_FLASH_UF2, 1),
}
#: Actions that need a firmware file.
FILE_ACTIONS = frozenset({"esp_flash", "picoboot_load", "samba_flash", "uf2_copy"})

_STAGED_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


class BridgeRescueUnsupported(EspBridgeError):
    """The bridge's capabilities do not offer this action (or it is pre-1.0)."""


def gate(caps: BridgeCapabilities | None, action: str, *, mode: str = "") -> str:
    """'' when ``caps`` allows ``action`` (and reset ``mode``), else the reason it does not."""
    if action not in ACTION_FEATURES:
        return f"unknown bridge rescue action {action!r}"
    if caps is None:
        return "bridge capabilities unknown (bridge unreachable?)"
    if caps.pre_1_0:
        return (
            "bridge is pre-1.0 (no /api/capabilities): on-bridge rescue is not offered; "
            "use the USB/IP path"
        )
    if not caps.compatible:
        return f"bridge api_version {caps.api_version} is not a 1.x API this controller supports"
    feature, min_version = ACTION_FEATURES[action]
    have = caps.feature_version(feature)
    if have < min_version:
        return f"bridge feature {feature!r} is {have or 'absent'} (needs >= {min_version})"
    if action == "reset" and mode and mode not in caps.reset_modes():
        return f"bridge does not offer reset mode {mode!r} (offers {caps.reset_modes()})"
    return ""


def available_actions(caps: BridgeCapabilities | None) -> list[str]:
    """Actions the bridge allows, in display order."""
    return [a for a in ACTION_FEATURES if not gate(caps, a)]


def staged_name(job_id: str, filename: str) -> str:
    """A bridge-safe staged file name (``[A-Za-z0-9._-]``), unique per job."""
    base = _STAGED_NAME_RE.sub("_", Path(filename).name) or "image.bin"
    return f"hil-{job_id[:8]}-{base}"[:64]


def _int_or_hex(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "0").strip().lower()
    return int(text, 16) if text.startswith("0x") else int(text)


class BridgeRescueClient:
    """Rescue calls against one bridge for one device (``busid``)."""

    def __init__(self, client: EspUsbipBridgeClient, busid: str, *, poll_s: float = 1.0) -> None:
        self.client = client
        self.busid = busid
        self.poll_s = poll_s

    def _dev(self, suffix: str) -> str:
        return f"/api/usb/devices/{self.busid}/{suffix}"

    async def info(self) -> dict[str, Any]:
        return dict(await self.client.request("GET", self._dev("rescue")) or {})

    async def reset(self, mode: str, *, port: int | None = None, wait_ms: int | None = None) -> Any:
        body: dict[str, Any] = {"mode": mode}
        if port is not None:
            body["port"] = int(port)
        if wait_ms is not None:
            body["wait_ms"] = int(wait_ms)
        return await self.client.request(
            "POST", self._dev("reset"), json=body, timeout_s=RESET_TIMEOUT_S
        )

    async def console(self, **params: Any) -> dict[str, Any]:
        body = {k: v for k, v in params.items() if v is not None}
        return dict(
            await self.client.request(
                "POST", self._dev("console"), json=body, timeout_s=CONSOLE_TIMEOUT_S
            )
            or {}
        )

    async def stage_file(self, name: str, data: bytes) -> Any:
        return await self.client.request(
            "PUT", f"/api/rescue/files/{name}", content=data, timeout_s=UPLOAD_TIMEOUT_S
        )

    async def delete_file(self, name: str) -> None:
        try:
            await self.client.request("DELETE", f"/api/rescue/files/{name}")
        except EspBridgeError:
            pass  # PSRAM staging is lost on reboot anyway

    async def start_job(self, suffix: str, body: dict[str, Any] | None = None) -> str:
        resp = await self.client.request("POST", self._dev(suffix), json=body or {})
        job = (resp or {}).get("job") if isinstance(resp, dict) else None
        if job is None:
            raise EspBridgeError(f"bridge did not return a job id for {suffix}: {resp!r}")
        return str(job)

    async def job(self, job_id: str) -> dict[str, Any]:
        return dict(await self.client.request("GET", f"/api/rescue/jobs/{job_id}") or {})

    async def wait_job(
        self,
        job_id: str,
        *,
        timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
        on_line: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Poll a bridge job until done/failed; relay new log lines. Raises on timeout."""
        deadline = time.monotonic() + timeout_s
        seen = 0
        while True:
            state = await self.job(job_id)
            log_lines = state.get("log") or []
            if isinstance(log_lines, list) and on_line is not None:
                for entry in log_lines[seen:]:
                    on_line(entry if isinstance(entry, str) else str(entry))
                seen = len(log_lines)
            if state.get("state") in ("done", "failed"):
                return state
            if time.monotonic() >= deadline:
                raise EspBridgeError(
                    f"bridge job {job_id} still {state.get('state')!r} after {timeout_s:.0f}s"
                )
            await asyncio.sleep(self.poll_s)


def check_staging_room(caps: BridgeCapabilities | None, size: int) -> str:
    """'' when ``size`` fits the bridge's ``limits.rescue_staging_bytes`` (or it is unknown)."""
    limit = caps.staging_bytes() if caps else None
    if limit is not None and size > limit:
        return f"image is {size} bytes; the bridge stages at most {limit} bytes"
    return ""


def job_body(action: str, stage: dict[str, Any], staged: str) -> tuple[str, dict[str, Any]]:
    """``(path suffix, body)`` for a job-type action, from a ``bridge_rescue`` stage."""
    if action == "esp_info":
        return "esp/info", {}
    if action == "esp_flash":
        body: dict[str, Any] = {
            "images": [{"file": staged, "offset": _int_or_hex(stage.get("address", "0x0"))}],
            "verify": bool(stage.get("verify", True)),
            "erase_all": bool(stage.get("erase_all", False)),
        }
        for key in ("before", "after", "baud", "stub"):
            if stage.get(key) is not None:
                body[key] = stage[key]
        return "flash/esp", body
    if action == "picoboot_info":
        return "picoboot/info", {}
    if action == "picoboot_load":
        body = {"file": staged, "verify": bool(stage.get("verify", True))}
        for key in ("address", "reboot", "erase_all", "flash_size"):
            if stage.get(key) is not None:
                body[key] = stage[key]
        return "flash/picoboot", body
    if action == "samba_info":
        return "samba/info", {}
    if action == "samba_flash":
        body = {"file": staged}
        for key in ("offset", "erase", "verify", "reset"):
            if stage.get(key) is not None:
                body[key] = stage[key]
        return "flash/samba", body
    if action == "uf2_copy":
        body = {"file": staged}
        for key in ("path", "method", "enter_bootloader"):
            if stage.get(key) is not None:
                body[key] = stage[key]
        return "flash/uf2", body
    raise ValueError(f"{action!r} is not a bridge job action")


# --------------------------------------------------------------------------- #
# Translate a /ui/rescue or /ui/tools form action into a bridge_rescue stage   #
# --------------------------------------------------------------------------- #


def bridge_stage_for_tool(
    tool: str,
    action: str,
    *,
    address: str = "0x0",
    esptool_before: str = "",
    esptool_after: str = "",
    reset_modes: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Map a host-tool action (esptool/picotool/bossac) onto the bridge's rescue API.

    Returns ``(stage, error)``. Actions the bridge has no equivalent for (an
    erase-only esptool/picotool/bossac run) return an error so the operator uses
    the USB/IP path for them.
    """
    modes = reset_modes or []
    if tool == "esptool":
        if action in ("chip_id", "flash_id", "read_mac"):
            return {"type": "bridge_rescue", "action": "esp_info"}, ""
        if action == "write":
            stage: dict[str, Any] = {
                "type": "bridge_rescue",
                "action": "esp_flash",
                "address": address or "0x0",
            }
            # esptool spellings that the bridge flasher accepts as is.
            if esptool_before in ("default_reset", "usb_reset", "no_reset"):
                stage["before"] = esptool_before
            if esptool_after in ("hard_reset", "no_reset"):
                stage["after"] = esptool_after
            return stage, ""
        return None, f"esptool {action} has no on-bridge equivalent; use the USB/IP path"
    if tool == "picotool":
        if action == "info":
            return {"type": "bridge_rescue", "action": "picoboot_info"}, ""
        if action == "load":
            return {"type": "bridge_rescue", "action": "picoboot_load"}, ""
        if action in ("reboot_bootsel", "reboot"):
            wanted = (
                ("rp_bootsel", "picoboot_bootsel")
                if action == "reboot_bootsel"
                else ("picoboot_reboot", "rp_reboot")
            )
            mode = next((m for m in wanted if m in modes), wanted[0])
            return {"type": "bridge_rescue", "action": "reset", "mode": mode}, ""
        return None, f"picotool {action} has no on-bridge equivalent; use the USB/IP path"
    if tool == "bossac":
        if action == "info":
            return {"type": "bridge_rescue", "action": "samba_info"}, ""
        if action == "write":
            return {
                "type": "bridge_rescue",
                "action": "samba_flash",
                "offset": address if address and address != "0x0" else None,
            }, ""
        return None, f"bossac {action} has no on-bridge equivalent; use the USB/IP path"
    return None, f"unknown tool {tool!r}"
