"""NeoPixel illuminator via Adafruit Blinka + CircuitPython NeoPixel.

CircuitPython's ``neopixel`` library on Pi uses ``rpi_ws281x`` under the
hood, which only supports a fixed set of GPIO pins:

    PWM channel 0:  GPIO12, GPIO18    (most common)
    PWM channel 1:  GPIO13, GPIO19
    PCM:            GPIO21
    SPI:            GPIO10

Pins outside that list fail at ws2811_init time with "Selected GPIO not
possible". If your STEMMA connector lands somewhere else (e.g. GPIO5 or
GPIO6 on some Adafruit bonnets), bridge the ring's signal line to one
of the supported GPIOs.

DMA/PWM access also requires root. The systemd unit ships as User=pi;
switch to User=root on hosts where the illuminator is needed, or use
--no-neopixel to skip illuminator init entirely.
"""
from __future__ import annotations

from .base import Illuminator, IlluminatorUnavailable

try:
    import board  # type: ignore[import-not-found]
    import neopixel  # type: ignore[import-not-found]
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001
    board = None  # type: ignore[assignment]
    neopixel = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


class NeoPixelIlluminator(Illuminator):
    kind = "neopixel"

    def __init__(
        self,
        *,
        pin: str = "D18",
        count: int = 32,
        color: tuple[int, int, int] = (255, 255, 255),
    ) -> None:
        if neopixel is None or board is None:
            raise IlluminatorUnavailable(
                f"adafruit_blinka/neopixel not importable: {_IMPORT_ERROR}"
            )
        pin_obj = getattr(board, pin, None)
        if pin_obj is None:
            raise IlluminatorUnavailable(f"board has no pin {pin!r}")
        # Wrap the whole init — NeoPixel() succeeds even when the GPIO
        # isn't usable (e.g. GPIO5 on Pi); the failure surfaces on the
        # first show(). Catch both so the server can fall back to Null.
        try:
            self._strip = neopixel.NeoPixel(
                pin_obj, count, brightness=0.0, auto_write=False
            )
            self._strip.fill(color)
            self._strip.show()
        except Exception as exc:
            raise IlluminatorUnavailable(
                f"NeoPixel init failed on pin {pin}: {exc}"
            ) from exc
        self._count = count
        self._color = color
        self._brightness = 0

    def set_brightness(self, value: int) -> None:
        v = max(0, min(255, int(value)))
        self._brightness = v
        self._strip.brightness = v / 255.0
        # Re-fill in case anything stomped the colour state, then show.
        self._strip.fill(self._color)
        self._strip.show()

    def get_brightness(self) -> int:
        return self._brightness

    def close(self) -> None:
        try:
            self._strip.brightness = 0.0
            self._strip.fill((0, 0, 0))
            self._strip.show()
            self._strip.deinit()
        except Exception:
            pass
