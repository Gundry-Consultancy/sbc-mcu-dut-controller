"""Proof assertions must reject missing data and unchanged settings."""
import pytest

from hil_controller.adapters.protobuf_probe import check_i2c_samples


def samples(reference):
    p = 1010.0
    altitude = 44330.0 * (1 - (p / reference) ** 0.1903)
    return [{"readings": {6: p, 27: altitude}}] * 3


def test_real_readings_match_configured_reference():
    assert check_i2c_samples(samples(1100), {"sea_level_pressure": 1100})["ok"]


def test_unchanged_settings_fail_despite_events():
    with pytest.raises(ValueError, match="not applied"):
        check_i2c_samples(samples(1000), {"sea_level_pressure": 1100})


def test_no_events_cannot_pass():
    with pytest.raises(ValueError, match="got 0"):
        check_i2c_samples([], {"sea_level_pressure": 1100})


def test_missing_requested_sensor_fails():
    with pytest.raises(ValueError, match="missing"):
        check_i2c_samples(samples(1000), {"readings": {"13": {}}})
