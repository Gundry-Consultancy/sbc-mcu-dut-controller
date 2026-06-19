"""Illuminator drivers.

An illuminator is a host-attached light source (NeoPixel ring, LED panel,
etc.) the camera server can dim or brighten in response to client
requests — independent of the camera backend. Falls back to a NullIlluminator
when hardware or driver libraries are absent so the rest of the server
still runs.
"""
from .base import Illuminator, IlluminatorUnavailable
from .null_illuminator import NullIlluminator

__all__ = ["Illuminator", "IlluminatorUnavailable", "NullIlluminator", "build_illuminator"]


def build_illuminator(*, kind: str, **kwargs) -> Illuminator:
    """Factory: instantiate a driver by name, returning Null on failure."""
    if kind in ("none", "off", "null"):
        return NullIlluminator()
    if kind == "neopixel":
        try:
            from .neopixel_illuminator import NeoPixelIlluminator

            return NeoPixelIlluminator(**kwargs)
        except IlluminatorUnavailable as exc:
            print(f"WARN: neopixel illuminator unavailable, using null: {exc}", flush=True)
            return NullIlluminator()
    raise IlluminatorUnavailable(f"unknown illuminator kind: {kind}")
