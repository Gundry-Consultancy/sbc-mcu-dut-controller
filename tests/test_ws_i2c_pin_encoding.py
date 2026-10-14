"""Wire compatibility for pre-alignment and PR1014 I2C AddressSpace pins."""
import pytest

from hil_controller.adapters.ws_i2c_inject import encode_address_space


def test_current_proto_pins_are_length_delimited_strings():
    # api-v2 AddressSpace: string pin_scl=1, string pin_sda=2, uint32 mux_address=3/channel=4.
    assert encode_address_space(
        pin_scl="D40", pin_sda="D41", mux_address=119, mux_channel=1
    ) == bytes.fromhex("0a 03 44 34 30 12 03 44 34 31 18 77 20 01")


def test_legacy_proto_pins_remain_varints():
    assert encode_address_space(pin_scl=40, pin_sda=41) == bytes.fromhex("08 28 10 29")


@pytest.mark.parametrize("pin", ["", "D" * 32, "D4\0"])
def test_pin_names_reject_invalid_nanopb_strings(pin):
    with pytest.raises(ValueError, match="pin names"):
        encode_address_space(pin_scl=pin, pin_sda="D41")
