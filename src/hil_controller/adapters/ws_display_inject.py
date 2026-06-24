"""Inject a v2 display Add over protomq ``POST /api/echo`` (i8080 ST7789).

Mirrors :mod:`ws_i2c_inject`: wraps ``ws.display.B2D`` in
``ws.signal.BrokerToDevice`` (field 36) and publishes on
``<io_user>/ws-b2d/<uid>``. The DUT initialises the panel + paints its splash /
status bar on receipt. Field numbers are from the firmware headers (ground truth):

    signal.BrokerToDevice.display          = 36
    display.B2D.add                        = 1
    display.Add{name=1,type=2,driver=3,panel=4,interface_type=5,config_display=9}
    display.InterfaceDescriptor.i8080      = 6
    display.I8080PinDescriptor{pin_d0=1..pin_d7=8, pin_cs=9, pin_dc=10, pin_rst=11}
    display.DisplayProperties{width=1,height=2,rotation=3,text_size=4,status_bar=5}
    display.DisplayClass.TFT               = 2

WR/RD/power/backlight are board-fixed (I8080_* macros in the firmware), not in
the proto, so they are not sent here.
"""
from __future__ import annotations

from typing import Any

from hil_controller.adapters.ws_i2c_inject import (
    WsI2cProbeInjector,
    _len_field,
    _vint_field,
)

_SIGNAL_B2D_DISPLAY = 36  # ws.signal.BrokerToDevice.display
_CLASS_TFT = 2            # ws.display.DisplayClass.DISPLAY_CLASS_TFT


def _str_field(field: int, value: str) -> bytes:
    return _len_field(field, value.encode("utf-8"))


def encode_i8080_pins(data_pins: list[str], cs: str, dc: str, rst: str) -> bytes:
    """ws.display.I8080PinDescriptor — pin_d0..pin_d7 (1..8), pin_cs(9), dc(10), rst(11)."""
    if len(data_pins) != 8:
        raise ValueError("i8080 needs exactly 8 data pins")
    b = b""
    for i, pin in enumerate(data_pins, start=1):
        b += _str_field(i, pin)
    b += _str_field(9, cs) + _str_field(10, dc) + _str_field(11, rst)
    return b


def encode_display_properties(
    width: int, height: int, rotation: int, text_size: int, status_bar: bool
) -> bytes:
    return (
        _vint_field(1, width)
        + _vint_field(2, height)
        + _vint_field(3, rotation)
        + _vint_field(4, text_size)
        + _vint_field(5, 1 if status_bar else 0)
    )


def build_display_add_i8080(
    *,
    name: str,
    driver: str,
    data_pins: list[str],
    cs: str,
    dc: str,
    rst: str,
    width: int,
    height: int,
    rotation: int,
    text_size: int,
    status_bar: bool,
) -> bytes:
    """Full ``ws.signal.BrokerToDevice`` bytes carrying a display Add (i8080 TFT)."""
    iface = _len_field(6, encode_i8080_pins(data_pins, cs, dc, rst))  # InterfaceDescriptor.i8080
    add = (
        _str_field(1, name)                       # Add.name
        + _vint_field(2, _CLASS_TFT)              # Add.type = TFT
        + _str_field(3, driver)                   # Add.driver = "ST7789"
        + _len_field(5, iface)                    # Add.interface_type
        + _len_field(9, encode_display_properties(width, height, rotation, text_size, status_bar))  # Add.config_display
    )
    b2d = _len_field(1, add)                      # ws.display.B2D.add
    return _len_field(_SIGNAL_B2D_DISPLAY, b2d)   # ws.signal.BrokerToDevice.display


class WsDisplayInjector(WsI2cProbeInjector):
    """Reuses the i2c injector's echo/checkin/topic plumbing; adds display Add."""

    async def add_display(self, uid: str, payload: bytes) -> dict[str, Any]:
        topic = self.b2d_topic(uid)
        resp = await self._echo(topic, payload)
        return {"topic": topic, "payload_hex": payload.hex(" "), "echo_response": resp}
