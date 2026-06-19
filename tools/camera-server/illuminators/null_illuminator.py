"""NullIlluminator — no-op driver for hosts without a light source."""
from __future__ import annotations

from .base import Illuminator


class NullIlluminator(Illuminator):
    kind = "null"

    def __init__(self) -> None:
        self._brightness = 0

    def set_brightness(self, value: int) -> None:
        self._brightness = max(0, min(255, int(value)))

    def get_brightness(self) -> int:
        return self._brightness

    def is_available(self) -> bool:
        return False
