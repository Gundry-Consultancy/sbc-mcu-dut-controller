"""Tests for the pure device-availability policy."""

from __future__ import annotations

from datetime import datetime, timedelta

from hil_controller import availability as av


def _t(s: int) -> datetime:
    return datetime(2026, 6, 14, 16, 0, 0) + timedelta(seconds=s)


def test_permanent_is_not_self_healable() -> None:
    assert av.is_self_healable("permanent") is False
    assert av.is_self_healable(None) is False
    assert av.is_self_healable("temporary") is True


def test_next_retry_permanent_or_available_not_applicable() -> None:
    d = av.next_retry(kind="permanent", retry_attempts=0, retry_after=None, now=_t(0))
    assert d.action == "not_applicable"
    d = av.next_retry(kind=None, retry_attempts=0, retry_after=None, now=_t(0))
    assert d.action == "not_applicable"


def test_next_retry_temporary_runs_now_when_due() -> None:
    d = av.next_retry(kind="temporary", retry_attempts=0, retry_after=None, now=_t(0))
    assert d.action == "retry_now"
    assert d.attempts_remaining == 3


def test_next_retry_waits_until_retry_after() -> None:
    d = av.next_retry(kind="temporary", retry_attempts=1, retry_after=_t(60), now=_t(10))
    assert d.action == "wait"
    assert d.wait_until == _t(60)
    assert d.attempts_remaining == 2


def test_next_retry_gives_up_only_when_steady_disabled() -> None:
    d = av.next_retry(
        kind="temporary", retry_attempts=3, retry_after=None, now=_t(999), steady_retry_s=None
    )
    assert d.action == "give_up"
    assert d.attempts_remaining == 0


def test_next_retry_steady_recheck_after_budget() -> None:
    """Default policy: the budget spent means SLOWER, not NEVER — a due device
    is still re-probed on the steady cadence."""
    d = av.next_retry(kind="temporary", retry_attempts=3, retry_after=None, now=_t(999))
    assert d.action == "retry_now"
    assert d.attempts_remaining == 0


def test_next_retry_steady_recheck_waits_until_due() -> None:
    d = av.next_retry(kind="temporary", retry_attempts=5, retry_after=_t(900), now=_t(100))
    assert d.action == "wait"
    assert d.wait_until == _t(900)


def test_backoff_even_spacing() -> None:
    assert av.backoff(window_s=180, max_attempts=3) == timedelta(seconds=60)
    assert av.backoff(window_s=180, max_attempts=1) == timedelta(seconds=180)


def test_target_record_available_row() -> None:
    rec = av.target_record({"id": "d1", "model": "QT Py ESP32-S3", "status": "available"})
    assert rec == {
        "target": "QT Py ESP32-S3",
        "model": "QT Py ESP32-S3",
        "device_id": "d1",
        "host_id": None,
        "available": True,
        "status": "available",
        "kind": None,
        "reason": None,
        "retry_after": None,
        "host": None,
    }


def test_target_record_includes_host_hardware() -> None:
    # The device's host hardware view is surfaced under "host" so a scheduler can
    # tell SBC hosts apart even when every device shares the same static model.
    hw = {"model": "Raspberry Pi Zero W Rev 1.1", "cpu_cores": 1, "speed_score": 1.0}
    rec = av.target_record(
        {"id": "d1", "host_id": "rpi-hil002", "model": "pi5", "status": "available"},
        host_hw=hw,
    )
    assert rec["host_id"] == "rpi-hil002"
    assert rec["host"]["model"] == "Raspberry Pi Zero W Rev 1.1"
    assert rec["host"]["speed_score"] == 1.0


def test_sbc_model_comes_from_detected_host_not_static_pi5() -> None:
    # SBC device: the static topology model is a blanket "pi5"; the availability
    # record must report the detected host board instead so /v1/targets doesn't
    # show every SBC as pi5. target falls back to the same (no build_target).
    rec = av.target_record(
        {"id": "rpi-hil002-pi5-a", "host_id": "rpi-hil002", "kind": "sbc", "model": "pi5"},
        host_hw={"model": "Raspberry Pi Zero W Rev 1.1"},
    )
    assert rec["model"] == "Raspberry Pi Zero W Rev 1.1"
    assert rec["target"] == "Raspberry Pi Zero W Rev 1.1"


def test_sbc_falls_back_to_static_model_when_host_undetected() -> None:
    # Offline SBC (host not probed yet): keep the stored model rather than blank.
    rec = av.target_record(
        {"id": "rpi-hil001-pi5-a", "host_id": "rpi-hil001", "kind": "sbc", "model": "pi5"},
        host_hw={"model": None},
    )
    assert rec["model"] == "pi5"


def test_mcu_model_is_not_overridden_by_host() -> None:
    # MCU device on a Pi 4 host: the chip model must win, NOT the host board.
    rec = av.target_record(
        {"id": "mcu-qtpy", "host_id": "rpi-hil006", "kind": "microcontroller",
         "model": "QT Py ESP32-S3", "build_target": "qtpy_esp32s3_n4r2"},
        host_hw={"model": "Raspberry Pi 4 Model B Rev 1.4"},
    )
    assert rec["model"] == "QT Py ESP32-S3"
    assert rec["target"] == "qtpy_esp32s3_n4r2"


def test_target_record_prefers_build_target_tag() -> None:
    # /v1/targets keys off the arduino-cli build-target name when present.
    rec = av.target_record(
        {
            "id": "d1",
            "model": "QT Py ESP32-S3 N4R2",
            "build_target": "qtpy_esp32s3_n4r2",
            "status": "available",
        }
    )
    assert rec["target"] == "qtpy_esp32s3_n4r2"
    assert rec["model"] == "QT Py ESP32-S3 N4R2"


def test_target_record_missing_status_reads_available() -> None:
    # Rows predating the availability columns: no status → available.
    rec = av.target_record({"id": "d1", "model": "x"})
    assert rec["available"] is True
    assert rec["status"] == "available"


def test_target_record_temporary_unavailable() -> None:
    rec = av.target_record(
        {
            "id": "d2",
            "model": "feather_esp32s2",
            "status": "unavailable",
            "unavailable_kind": "temporary",
            "unavailable_reason": "USB wedged",
            "retry_after": "2026-06-14T16:20:00Z",
        }
    )
    assert rec["available"] is False
    assert rec["kind"] == "temporary"
    assert rec["reason"] == "USB wedged"
    assert rec["retry_after"] == "2026-06-14T16:20:00Z"


def test_target_record_permanent_unavailable() -> None:
    rec = av.target_record(
        {
            "id": "d3",
            "model": "metro_esp32s2",
            "status": "unavailable",
            "unavailable_kind": "permanent",
            "unavailable_reason": "not wired",
        }
    )
    assert rec["available"] is False
    assert rec["kind"] == "permanent"
    assert rec["retry_after"] is None
