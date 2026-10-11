"""Protocol fixtures for identity reads; no device access or real sleeping."""

from dataclasses import replace

import pytest

from hil_controller.twin_models import Observation
from hil_controller.twin_sensor_readers import (
    SensorReadContext,
    crc8_word,
    read_sensor_identity,
)

READY = SensorReadContext(route_verified=True, lease_held=True, idle=True)
SERIAL_FRAME = bytes.fromhex("f89631 9f07c2 3bbe89")
STCC_FRAME = bytes.fromhex("090173 018ad4 f89631 9f07c2 3bbe89 abcd6f")


class FakeBus:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def write(self, address, data):
        self.calls.append(("write", address, data))

    def read(self, address, length):
        self.calls.append(("read", address, length))
        return self.take()

    def write_read(self, address, data, length):
        self.calls.append(("write_read", address, data, length))
        return self.take()

    def wait(self, seconds):
        self.calls.append(("wait", seconds))

    def take(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize("word,expected", [("beef", 0x92), ("abcd", 0x6F)])
def test_crc_manufacturer_vectors(word, expected):
    assert crc8_word(bytes.fromhex(word)) == expected


@pytest.mark.parametrize("raw", [b"", b"a", b"abc"])
def test_crc_requires_exact_word(raw):
    with pytest.raises(ValueError, match="two-byte"):
        crc8_word(raw)


@pytest.mark.parametrize("model,delay", [("sgp30", 0.0005), ("sgp40", 0.001), ("sgp41", 0.001)])
def test_sgp_exact_transactions_and_identity(model, delay):
    bus = FakeBus(SERIAL_FRAME)
    result = read_sensor_identity(model, 0x59, bus, context=READY)
    assert bus.calls == [
        ("write", 0x59, b"\x36\x82"),
        ("wait", delay),
        ("read", 0x59, 9),
    ]
    assert result.status == "ok"
    (identity,) = result.identities
    assert identity.value == "f8969f073bbe"
    assert identity.unique is True
    assert identity.owner == "chip"
    assert identity.component == model
    assert result.exchanges[0].response == SERIAL_FRAME
    assert result.exchanges[0].crc_words == (True, True, True)
    # Output fits the existing import contract without inventing a second identity shape.
    observation = Observation(
        observation_id="synthetic-read",
        source="test:fake-bus",
        observed_at="2026-01-01T00:00:00Z",
        kind="sensor",
        label="Synthetic sensor",
        identities=list(result.identities),
        attributes=result.attributes,
    )
    assert observation.identities[0].namespace == f"sensirion:{model}:serial"


@pytest.mark.parametrize("model", ["hdc302x", "hdc3020", "hdc3021", "hdc3022"])
def test_hdc_exact_repeated_start_commands_and_order(model):
    bus = FakeBus(SERIAL_FRAME[:3], SERIAL_FRAME[3:6], SERIAL_FRAME[6:])
    result = read_sensor_identity(model, 0x44, bus, context=READY)
    assert bus.calls == [
        ("write_read", 0x44, b"\x36\x83", 3),
        ("write_read", 0x44, b"\x36\x84", 3),
        ("write_read", 0x44, b"\x36\x85", 3),
    ]
    assert result.status == "ok"
    assert result.identities[0].value == "f8969f073bbe"
    assert result.identities[0].namespace == "ti:hdc302x:serial"
    assert all(exchange.repeated_start for exchange in result.exchanges)


def test_stcc_splits_product_and_serial():
    bus = FakeBus(STCC_FRAME)
    result = read_sensor_identity("stcc4", 0x64, bus, context=READY)
    assert bus.calls == [
        ("write", 0x64, b"\x36\x5b"),
        ("wait", 0.001),
        ("read", 0x64, 18),
    ]
    assert result.status == "ok"
    assert result.attributes["product_id"] == "0901018a"
    assert result.identities[0].value == "f8969f073bbeabcd"
    assert result.exchanges[0].crc_words == (True,) * 6


def test_stcc_valid_crc_wrong_product_cannot_produce_identity():
    bus = FakeBus(bytes.fromhex("abcd6f") + STCC_FRAME[3:])
    result = read_sensor_identity("stcc4", 0x64, bus, context=READY)
    assert result.status == "invalid_response"
    assert result.identities == ()
    assert result.attributes["product_id"] == "abcd018a"
    assert "Product ID" in result.reason


def test_tmp_preserves_mutable_raw_words_without_unlock_or_crc():
    bus = FakeBus(bytes.fromhex("beef"), bytes.fromhex("0000"), bytes.fromhex("abcd"))
    result = read_sensor_identity("tmp119", 0x48, bus, context=READY)
    assert bus.calls == [
        ("write_read", 0x48, b"\x05", 2),
        ("write_read", 0x48, b"\x06", 2),
        ("write_read", 0x48, b"\x08", 2),
    ]
    assert result.status == "ok"
    assert result.identities[0].value == "beef0000abcd"
    assert result.identities[0].unique is False
    assert result.identities[0].confidence == "reported"
    assert result.attributes["eeprom_words"] == {"05": "beef", "06": "0000", "08": "abcd"}
    assert result.attributes["factory_identity_mutable"] is True
    assert all(exchange.crc_words is None for exchange in result.exchanges)


@pytest.mark.parametrize(
    "model,address,frame",
    [
        ("sgp30", 0x58, SERIAL_FRAME),
        ("sgp40", 0x59, SERIAL_FRAME),
        ("sgp41", 0x59, SERIAL_FRAME),
        ("stcc4", 0x64, STCC_FRAME),
    ],
)
def test_every_crc_word_is_checked(model, address, frame):
    for crc_index in range(2, len(frame), 3):
        corrupt = bytearray(frame)
        corrupt[crc_index] ^= 1
        result = read_sensor_identity(model, address, FakeBus(bytes(corrupt)), context=READY)
        assert result.status == "invalid_response"
        assert result.identities == ()
        assert result.exchanges[0].response == bytes(corrupt)
        assert result.exchanges[0].crc_words[crc_index // 3] is False


@pytest.mark.parametrize("bad_word", [0, 1, 2])
def test_hdc_each_word_crc_failure_stops_further_reads(bad_word):
    words = [SERIAL_FRAME[index : index + 3] for index in range(0, 9, 3)]
    words[bad_word] = words[bad_word][:2] + bytes([words[bad_word][2] ^ 1])
    bus = FakeBus(*words)
    result = read_sensor_identity("hdc3020", 0x44, bus, context=READY)
    assert result.status == "invalid_response"
    assert result.identities == ()
    assert len(bus.calls) == bad_word + 1
    assert result.exchanges[-1].crc_words == (False,)


@pytest.mark.parametrize(
    "model,length",
    [("sgp30", 9), ("sgp40", 9), ("sgp41", 9), ("hdc302x", 3), ("stcc4", 18), ("tmp119", 2)],
)
@pytest.mark.parametrize("delta", [-1, 1])
def test_wrong_response_length_retained_without_identity(model, length, delta):
    raw = b"x" * (length + delta)
    result = read_sensor_identity(model, 0x44, FakeBus(raw), context=READY)
    assert result.status == "invalid_response"
    assert result.identities == ()
    assert result.exchanges[0].response == raw
    assert result.exchanges[0].crc_words is None


@pytest.mark.parametrize("model", ["unknown", "bme280", "sgp4x", "scd30"])
def test_unsupported_never_touches_bus(model):
    bus = FakeBus()
    result = read_sensor_identity(model, 0x44, bus, context=READY)
    assert result.status == "unsupported"
    assert not result.identities
    assert not bus.calls
    if model == "scd30":
        assert "Official serial API exists" in result.reason


@pytest.mark.parametrize("flag", ["route_verified", "lease_held", "idle"])
def test_missing_precondition_never_touches_bus(flag):
    bus = FakeBus()
    result = read_sensor_identity("sgp40", 0x59, bus, context=replace(READY, **{flag: False}))
    assert result.status == "blocked"
    assert not bus.calls


@pytest.mark.parametrize("address", [0, 7, 0x78, 0xFF, -1, True])
def test_reserved_or_invalid_address_never_touches_bus(address):
    bus = FakeBus()
    result = read_sensor_identity("sgp40", address, bus, context=READY)
    assert result.status == "blocked"
    assert not bus.calls


def test_io_failure_is_distinct_from_unsupported_and_retains_partial_evidence():
    bus = FakeBus(SERIAL_FRAME[:3], OSError("synthetic disconnected bus"))
    result = read_sensor_identity("hdc302x", 0x44, bus, context=READY)
    assert result.status == "read_failed"
    assert result.identities == ()
    assert len(result.exchanges) == 2
    assert result.exchanges[0].response == SERIAL_FRAME[:3]
    assert result.exchanges[1].response is None
    assert len(bus.calls) == 2


def test_write_failure_does_not_wait_read_or_retry():
    class FailedWriteBus(FakeBus):
        def write(self, address, data):
            super().write(address, data)
            raise TimeoutError("synthetic timeout")

    bus = FailedWriteBus()
    result = read_sensor_identity("sgp40", 0x59, bus, context=READY)
    assert result.status == "read_failed"
    assert result.identities == ()
    assert bus.calls == [("write", 0x59, b"\x36\x82")]
    assert result.exchanges[0].response is None


@pytest.mark.parametrize("word", ["000081", "ffffac"])
def test_placeholder_with_valid_crc_is_not_verified_identity(word):
    result = read_sensor_identity("sgp40", 0x59, FakeBus(bytes.fromhex(word) * 3), context=READY)
    assert result.status == "invalid_response"
    assert result.identities == ()
    assert result.exchanges[0].crc_words == (True, True, True)


def test_evidence_can_be_persisted_as_json_attributes_even_on_failure():
    import json

    raw = SERIAL_FRAME[:-1]
    result = read_sensor_identity("sgp40", 0x59, FakeBus(raw), context=READY)
    attributes = result.to_observation_attributes()
    encoded = json.loads(json.dumps(attributes))
    discovery = encoded["identity_discovery"]
    assert discovery["status"] == "invalid_response"
    assert discovery["exchanges"][0]["response_hex"] == raw.hex()
    assert discovery["exchanges"][0]["command_hex"] == "3682"
    assert discovery["exchanges"][0]["crc_words"] is None
