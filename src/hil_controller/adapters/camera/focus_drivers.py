"""Per-camera-kind focus drivers.

The orchestrator decides *what* to focus on (a camera-agnostic "directive");
a driver translates that into the HTTP calls a specific camera type understands.
This keeps the ROI-focus logic in one place while each camera kind maps the same
directive to its own native control surface.

Directive shape (see :func:`orchestrator.compute_focus_directive`)::

    {
      "mode":   "auto" | "window" | "manual",
      "window": (nx, ny, nw, nh) | None,   # normalized [0..1] rect
      "position": float | None,            # manual focus in the camera's native units
      "target_device": str | None,         # which DUT drove the decision (introspection)
    }

Capability tiers:
  * ``pi-camera-server`` (picamera2/libcamera) — true windowed AF; the ROI maps
    straight to ``AfWindows``.
  * ``ip-webcam`` (Android IP Webcam app) — no focus-region API at all, only
    full-frame AF. ``window`` degrades to continuous-picture AF + a ``/focus``
    trigger; ``manual`` maps to ``focusmode=off`` + ``focus_distance``.
  * unknown — no-op (logged); preserves the best-effort "a job never fails
    because the camera is unreachable or unsupported" rule.

All calls are best-effort: failures are logged and swallowed, never raised.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def resolve_camera_kind(row: Any) -> str:
    """Return the focus-driver kind for a camera row.

    Prefers the explicit ``cameras.kind`` column; falls back to inferring it from
    the source URL (same heuristic family as ``roi_snapshot.full_res_url``):
      * ``…/shot.jpg`` or ``…/photo.jpg`` -> ``ip-webcam`` (Android IP Webcam);
      * any other ``http(s)://`` source   -> ``pi-camera-server``;
      * everything else (``/dev/video*``, rtsp, empty) -> ``unknown``.
    """
    kind = _get(row, "kind")
    if kind:
        return str(kind)
    source = (_get(row, "source") or "").strip()
    base = source.split("?", 1)[0]
    if base.endswith("/shot.jpg") or base.endswith("/photo.jpg"):
        return "ip-webcam"
    if source.startswith(("http://", "https://")):
        return "pi-camera-server"
    return "unknown"


def _get(row: Any, key: str) -> Any:
    """Read ``key`` from a dict or sqlite Row, tolerating missing columns."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


class FocusDriver:
    """Translate a focus directive into camera-specific HTTP calls.

    Manual-focus values (``directive["position"]``, sourced from a device's
    ``manual_focus`` column) are in this driver's **native units** — there is no
    universal focus scale, so a value set for one camera kind does not translate
    to another. Each driver declares its unit name and valid range so callers /
    the UI can label inputs and clamp safely.
    """

    kind: str = "abstract"
    supports_window: bool = False
    #: Human-readable unit of ``manual_focus`` for this kind (UI label / API).
    focus_units: str = "native"
    #: Inclusive valid range for ``manual_focus``; ``None`` means unbounded.
    focus_min: float | None = None
    focus_max: float | None = None

    def clamp_focus(self, value: float) -> float:
        """Clamp a manual-focus value to this driver's declared range."""
        if self.focus_min is not None:
            value = max(self.focus_min, value)
        if self.focus_max is not None:
            value = min(value, self.focus_max)
        return value

    async def apply(
        self, client: httpx.AsyncClient, base_url: str, directive: dict[str, Any]
    ) -> dict[str, Any]:
        """Push the lens/focus directive. Returns what was attempted."""
        raise NotImplementedError

    async def apply_illuminator(
        self, client: httpx.AsyncClient, base_url: str, brightness: int | None
    ) -> dict[str, Any]:
        """Push the illuminator brightness. Default: no-op."""
        return {"illuminator": "unsupported"}


class PiCameraServerDriver(FocusDriver):
    """The bundled ``tools/camera-server`` (picamera2/libcamera or V4L2)."""

    kind = "pi-camera-server"
    supports_window = True
    # libcamera LensPosition is in dioptres (1/m). The exact max is sensor-
    # specific (≈0..32 on the IMX519), so leave the upper bound open and let the
    # camera-server clamp to its actual control range.
    focus_units = "dioptre"
    focus_min = 0.0
    focus_max = None

    async def apply(
        self, client: httpx.AsyncClient, base_url: str, directive: dict[str, Any]
    ) -> dict[str, Any]:
        mode = directive.get("mode", "auto")
        body: dict[str, Any]
        if mode == "window" and directive.get("window"):
            nx, ny, nw, nh = directive["window"]
            body = {"mode": "window", "window": {"x": nx, "y": ny, "w": nw, "h": nh}}
        elif mode == "manual" and directive.get("position") is not None:
            body = {"mode": "manual", "position": self.clamp_focus(float(directive["position"]))}
        else:
            body = {"mode": "auto"}
        try:
            r = await client.post(f"{base_url}/lens", json=body)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — best-effort peripheral
            logger.warning("pi-camera-server lens push failed (%s): %s", base_url, exc)
            return {"lens": body, "ok": False, "error": str(exc)}
        return {"lens": body, "ok": True}

    async def apply_illuminator(
        self, client: httpx.AsyncClient, base_url: str, brightness: int | None
    ) -> dict[str, Any]:
        body = {"brightness": int(brightness) if brightness is not None else 0}
        try:
            r = await client.post(f"{base_url}/illuminator", json=body)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("pi-camera-server illuminator push failed (%s): %s", base_url, exc)
            return {"illuminator": body, "ok": False, "error": str(exc)}
        return {"illuminator": body, "ok": True}


class IpWebcamDriver(FocusDriver):
    """Android IP Webcam app — full-frame AF only (no focus window)."""

    kind = "ip-webcam"
    supports_window = False
    # Android IP Webcam ``focus_distance`` accepts 0.0..10.0 in 0.1 steps.
    focus_units = "distance"
    focus_min = 0.0
    focus_max = 10.0

    def _snap(self, value: float) -> float:
        """Clamp to the valid range and round to the 0.1 grid Android accepts."""
        return round(self.clamp_focus(value), 1)

    async def apply(
        self, client: httpx.AsyncClient, base_url: str, directive: dict[str, Any]
    ) -> dict[str, Any]:
        mode = directive.get("mode", "auto")
        if mode == "manual" and directive.get("position") is not None:
            distance = self._snap(float(directive["position"]))
            calls = [
                f"{base_url}/settings/focusmode?set=off",
                f"{base_url}/settings/focus_distance?set={distance}",
            ]
            attempted = {"focusmode": "off", "focus_distance": distance}
        else:
            # No focus-region API on Android: continuous-picture AF over the whole
            # frame is the closest we can do. The ROI window is dropped on purpose.
            if mode == "window":
                logger.info(
                    "ip-webcam (%s) has no AF window; using full-frame continuous AF (ROI ignored)",
                    base_url,
                )
            calls = [
                f"{base_url}/settings/focusmode?set=continuous-picture",
                f"{base_url}/focus",
            ]
            attempted = {"focusmode": "continuous-picture", "trigger": "/focus"}
        ok = await _get_all(client, calls, base_url, "focus")
        return {"focus": attempted, "windowed": False, "ok": ok}

    async def apply_illuminator(
        self, client: httpx.AsyncClient, base_url: str, brightness: int | None
    ) -> dict[str, Any]:
        on = bool(brightness)
        url = f"{base_url}/{'enabletorch' if on else 'disabletorch'}"
        ok = await _get_all(client, [url], base_url, "illuminator")
        return {"illuminator": {"torch": "on" if on else "off"}, "ok": ok}


class UnknownDriver(FocusDriver):
    """A camera kind with no known focus control — log and skip."""

    kind = "unknown"
    supports_window = False

    async def apply(
        self, client: httpx.AsyncClient, base_url: str, directive: dict[str, Any]
    ) -> dict[str, Any]:
        logger.info(
            "no focus driver for camera at %s; skipping (%s)", base_url, directive.get("mode")
        )
        return {"focus": "unsupported", "ok": False}


async def _get_all(client: httpx.AsyncClient, urls: list[str], base_url: str, what: str) -> bool:
    """Fire a sequence of best-effort GETs; log and swallow any failure."""
    try:
        for url in urls:
            r = await client.get(url)
            r.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — best-effort peripheral
        logger.warning("ip-webcam %s push failed (%s): %s", what, base_url, exc)
        return False
    return True


_DRIVERS: dict[str, FocusDriver] = {
    "pi-camera-server": PiCameraServerDriver(),
    "ip-webcam": IpWebcamDriver(),
    "unknown": UnknownDriver(),
}


def get_driver(kind: str) -> FocusDriver:
    """Return the driver for a kind, falling back to the no-op driver."""
    return _DRIVERS.get(kind, _DRIVERS["unknown"])
