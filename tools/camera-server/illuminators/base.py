"""Illuminator interface."""
from __future__ import annotations

from abc import ABC, abstractmethod


class IlluminatorUnavailable(RuntimeError):
    """Raised when an illuminator's hardware or library is missing."""


class Illuminator(ABC):
    """Abstract host-attached light source.

    ``brightness`` is an 8-bit value (0..255) matching the NeoPixel
    convention — drivers map this to whatever their hardware exposes.
    """

    kind: str = "abstract"
    #: Host-wide ceiling (0..255) every request is clamped to — e.g. 102 (40%) on a
    #: bench whose ring must never run brighter. Set via ``server.py --max-brightness``.
    max_brightness: int = 255

    def clamp(self, value: int) -> int:
        """Clamp a requested brightness to 0..``max_brightness``."""
        ceiling = max(0, min(255, int(self.max_brightness)))
        return max(0, min(ceiling, int(value)))

    @abstractmethod
    def set_brightness(self, value: int) -> None:
        """Set the illuminator brightness, clamped to 0..``max_brightness``."""

    @abstractmethod
    def get_brightness(self) -> int:
        """Return the most recently applied brightness."""

    def is_available(self) -> bool:
        return True

    def close(self) -> None:
        """Release any hardware handles; called at server shutdown."""
