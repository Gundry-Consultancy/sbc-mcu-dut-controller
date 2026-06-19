"""EsptoolFlasher tests (M3.5).

Parser-level units use real-shape esptool.py output snapshots taken from
v4.7+ runs. Adapter-level tests drive the verbs against an AsyncMock
transport and assert the exact argv that hits the wire.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.flashers import Artifact, FlasherToolFailed
from hil_controller.adapters.flashers.esptool import (
    EsptoolFlasher,
    classify_boot_state,
    family_to_chip_arg,
    parse_chip_family,
    parse_flash_size_bytes,
    parse_mac,
    parse_reset_reason,
    parse_wrote_bytes,
    stty_1200_touch_argv,
)
from hil_controller.hosts.base import ExecResult

# --------------------------------------------------------------------------- #
# Fixtures + sample outputs                                                   #
# --------------------------------------------------------------------------- #

ESPTOOL_FLASH_ID_S3 = """\
esptool.py v4.7.0
Serial port /dev/ttyACM0
Connecting....
Chip is ESP32-S3 (revision v0.2)
Features: WiFi, BLE
Crystal is 40MHz
MAC: 7c:df:a1:b2:c3:d4
Uploading stub...
Running stub...
Stub running...
Manufacturer: ef
Device: 4017
Detected flash size: 8MB
Hard resetting via RTS pin...
"""

ESPTOOL_FLASH_ID_S2 = """\
esptool.py v4.7.0
Serial port /dev/ttyACM0
Connecting....
Chip is ESP32-S2 (revision v0.0)
Features: WiFi, No Embedded Flash, No Embedded PSRAM, ADC and temp sensor calibration in efuse
Crystal is 40MHz
MAC: 80:65:99:00:11:22
Uploading stub...
Running stub...
Stub running...
Manufacturer: ef
Device: 4016
Detected flash size: 4MB
Hard resetting via RTS pin...
"""

ESPTOOL_FLASH_ID_PLAIN_ESP32 = """\
esptool.py v4.7.0
Serial port /dev/ttyUSB0
Connecting........
Chip is ESP32-D0WDQ6 (revision v1.0)
Features: WiFi, BT, Dual Core, 240MHz, VRef calibration in efuse, Coding Scheme None
Crystal is 40MHz
MAC: aa:bb:cc:dd:ee:ff
Uploading stub...
Detected flash size: 4MB
Hard resetting via RTS pin...
"""

ESPTOOL_WRITE_FLASH_SINGLE = """\
esptool.py v4.7.0
Serial port /dev/ttyACM0
Connecting....
Chip is ESP32-S3 (revision v0.2)
Configuring flash size...
Flash will be erased from 0x00000000 to 0x00100000...
Compressed 1234567 bytes to 567890...
Writing at 0x00000000... (100 %)
Wrote 1234567 bytes (compressed 567890 bytes) at 0x00000000 in 12.3 seconds (effective 800.0 kbit/s)
Hash of data verified.
Leaving...
Hard resetting via RTS pin...
"""

ESPTOOL_WRITE_FLASH_MULTI = """\
Wrote 17312 bytes (compressed 11200 bytes) at 0x00000000 in 0.5 seconds
Wrote 3072 bytes (compressed 200 bytes) at 0x00008000 in 0.1 seconds
Wrote 8192 bytes (compressed 47 bytes) at 0x0000e000 in 0.1 seconds
Wrote 1048576 bytes (compressed 600123 bytes) at 0x00010000 in 8.0 seconds
"""


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


def _transport(default: MagicMock | None = None) -> AsyncMock:
    t = AsyncMock()
    t.exec = AsyncMock(return_value=default if default is not None else _result(0))
    return t


def _argvs(mock_transport: AsyncMock) -> list[list[str]]:
    return [c.args[0] for c in mock_transport.exec.call_args_list]


# --------------------------------------------------------------------------- #
# Pure parsers                                                                #
# --------------------------------------------------------------------------- #


def test_parse_chip_family_s3() -> None:
    assert parse_chip_family(ESPTOOL_FLASH_ID_S3) == "ESP32-S3"


def test_parse_chip_family_s2() -> None:
    assert parse_chip_family(ESPTOOL_FLASH_ID_S2) == "ESP32-S2"


def test_parse_chip_family_plain_esp32_keeps_variant() -> None:
    # Real-world esptool reports "ESP32-D0WDQ6" — the device-record code
    # is free to normalise it back to "ESP32".
    assert parse_chip_family(ESPTOOL_FLASH_ID_PLAIN_ESP32) == "ESP32-D0WDQ6"


def test_parse_chip_family_none_when_absent() -> None:
    assert parse_chip_family("some unrelated output") is None
    assert parse_chip_family("") is None


def test_parse_mac() -> None:
    assert parse_mac(ESPTOOL_FLASH_ID_S3) == "7c:df:a1:b2:c3:d4"
    assert parse_mac(ESPTOOL_FLASH_ID_S2) == "80:65:99:00:11:22"
    assert parse_mac(ESPTOOL_FLASH_ID_PLAIN_ESP32) == "aa:bb:cc:dd:ee:ff"


def test_parse_mac_lowercased() -> None:
    assert parse_mac("MAC: AA:BB:CC:DD:EE:FF\n") == "aa:bb:cc:dd:ee:ff"


def test_parse_mac_none_when_absent() -> None:
    assert parse_mac("") is None
    assert parse_mac("MAC:wrong shape") is None


def test_parse_flash_size_bytes() -> None:
    assert parse_flash_size_bytes(ESPTOOL_FLASH_ID_S3) == 8 * 1024 * 1024
    assert parse_flash_size_bytes(ESPTOOL_FLASH_ID_S2) == 4 * 1024 * 1024


def test_parse_flash_size_bytes_none() -> None:
    assert parse_flash_size_bytes("") is None


def test_parse_wrote_bytes_single_segment() -> None:
    assert parse_wrote_bytes(ESPTOOL_WRITE_FLASH_SINGLE) == 1234567


def test_parse_wrote_bytes_multi_segment_sums() -> None:
    assert parse_wrote_bytes(ESPTOOL_WRITE_FLASH_MULTI) == (17312 + 3072 + 8192 + 1048576)


def test_parse_wrote_bytes_empty_returns_zero() -> None:
    assert parse_wrote_bytes("") == 0


def test_family_to_chip_arg_known_families() -> None:
    assert family_to_chip_arg("ESP32-S3") == "esp32s3"
    assert family_to_chip_arg("ESP32-S2") == "esp32s2"
    assert family_to_chip_arg("ESP32") == "esp32"
    assert family_to_chip_arg("ESP32-C3") == "esp32c3"
    assert family_to_chip_arg("esp32-s3") == "esp32s3"  # case-insensitive


def test_family_to_chip_arg_unknown_falls_back_to_auto() -> None:
    assert family_to_chip_arg(None) == "auto"
    assert family_to_chip_arg("ESP32-D0WDQ6") == "auto"
    assert family_to_chip_arg("") == "auto"


# --------------------------------------------------------------------------- #
# probe()                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_probe_runs_flash_id_with_base_argv() -> None:
    tp = _transport(_result(0, stdout=ESPTOOL_FLASH_ID_S3))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    info = await flasher.probe()

    argv = _argvs(tp)[0]
    assert argv == [
        "python3",
        "-m",
        "esptool",
        "--chip",
        "esp32s3",
        "--port",
        "/dev/ttyACM0",
        "--baud",
        "921600",
        "flash_id",
    ]
    assert info.family == "ESP32-S3"
    assert info.mac == "7c:df:a1:b2:c3:d4"
    assert info.flash_bytes == 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_probe_auto_chip_works_without_explicit_family() -> None:
    tp = _transport(_result(0, stdout=ESPTOOL_FLASH_ID_S2))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0")  # chip="auto" default
    info = await flasher.probe()
    assert info.family == "ESP32-S2"
    argv = _argvs(tp)[0]
    assert argv[:3] == ["python3", "-m", "esptool"]
    assert argv[argv.index("--chip") + 1] == "auto"


@pytest.mark.asyncio
async def test_probe_records_raw_stdout_for_diagnostics() -> None:
    tp = _transport(_result(0, stdout=ESPTOOL_FLASH_ID_S3))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0")
    info = await flasher.probe()
    assert "Chip is ESP32-S3" in info.raw["flash_id"]


@pytest.mark.asyncio
async def test_probe_raises_when_esptool_fails() -> None:
    tp = _transport(_result(1, stderr="Failed to connect"))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", connect_retries=0)
    with pytest.raises(FlasherToolFailed, match="esptool"):
        await flasher.probe()


# --------------------------------------------------------------------------- #
# erase()                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_erase_runs_erase_flash() -> None:
    tp = _transport()
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    await flasher.erase()
    argv = _argvs(tp)[0]
    assert argv[-1] == "erase_flash"
    assert "--chip" in argv and "esp32s3" in argv
    assert "--port" in argv and "/dev/ttyACM0" in argv


@pytest.mark.asyncio
async def test_erase_raises_on_failure() -> None:
    tp = _transport(_result(2, stderr="oops"))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", connect_retries=0)
    with pytest.raises(FlasherToolFailed):
        await flasher.erase()


@pytest.mark.asyncio
async def test_soft_reset_prefers_watchdog_reset() -> None:
    """No-solenoid power-cycle fallback uses --after watchdog_reset (native-USB-safe)."""
    tp = _transport(_result(0))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    await flasher.soft_reset()
    afters = [a[a.index("--after") + 1] for a in _argvs(tp) if "--after" in a]
    assert afters == ["watchdog_reset"]  # succeeded → no fallback


@pytest.mark.asyncio
async def test_soft_reset_falls_back_to_hard_reset() -> None:
    """If --after watchdog_reset fails, retry with --after hard_reset."""

    def _exec(argv, **kw):
        return (
            _result(2, stderr="reset mode not supported")
            if "watchdog_reset" in argv
            else _result(0)
        )

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3", connect_retries=0)
    await flasher.soft_reset()
    afters = [
        a[a.index("--after") + 1] for a in _argvs(tp) if "--after" in a and a[-1] == "flash_id"
    ]
    assert "watchdog_reset" in afters and "hard_reset" in afters


# --------------------------------------------------------------------------- #
# flash()                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_flash_writes_at_default_offset_zero() -> None:
    tp = _transport(_result(0, stdout=ESPTOOL_WRITE_FLASH_SINGLE))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    artifact = Artifact(path="/tmp/combined.bin", kind="combined_bin")
    result = await flasher.flash(artifact)

    argv = _argvs(tp)[0]
    assert argv[-3:] == ["write_flash", "0x0", "/tmp/combined.bin"]
    assert result.bytes_written == 1234567
    assert result.elapsed_s >= 0.0


@pytest.mark.asyncio
async def test_flash_respects_artifact_offset() -> None:
    tp = _transport(_result(0, stdout=ESPTOOL_WRITE_FLASH_SINGLE))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    artifact = Artifact(path="/tmp/firmware.bin", offset=0x10000)
    await flasher.flash(artifact)

    argv = _argvs(tp)[0]
    assert "write_flash" in argv
    assert "0x10000" in argv


@pytest.mark.asyncio
async def test_flash_returns_summed_bytes_on_multi_segment_output() -> None:
    tp = _transport(_result(0, stdout=ESPTOOL_WRITE_FLASH_MULTI))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0")
    artifact = Artifact(path="/tmp/firmware.bin", offset=0x0)
    result = await flasher.flash(artifact)
    assert result.bytes_written == 17312 + 3072 + 8192 + 1048576


@pytest.mark.asyncio
async def test_flash_propagates_failure() -> None:
    tp = _transport(_result(1, stderr="MD5 mismatch"))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", connect_retries=0)
    with pytest.raises(FlasherToolFailed, match="esptool"):
        await flasher.flash(Artifact(path="/tmp/x.bin"))


# --------------------------------------------------------------------------- #
# reset()                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reset_into_bootloader_is_a_noop() -> None:
    tp = _transport()
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0")
    await flasher.reset(into="bootloader")
    # esptool handles bootloader entry inline; no transport calls.
    assert tp.exec.await_count == 0


@pytest.mark.asyncio
async def test_reset_into_application_is_a_noop() -> None:
    tp = _transport()
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0")
    await flasher.reset(into="application")
    assert tp.exec.await_count == 0


# --------------------------------------------------------------------------- #
# Custom baud                                                                 #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# --after no_reset / verify / 1200-baud touch (firmware-bench primitives)      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_erase_after_no_reset_inserts_flag_before_command() -> None:
    tp = _transport()
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    await flasher.erase(after="no_reset")
    argv = _argvs(tp)[0]
    assert argv[-3:] == ["--after", "no_reset", "erase_flash"]


@pytest.mark.asyncio
async def test_flash_after_no_reset_inserts_flag_before_write_flash() -> None:
    tp = _transport(_result(0, stdout=ESPTOOL_WRITE_FLASH_SINGLE))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    await flasher.flash(Artifact(path="/tmp/combined.bin"), after="no_reset")
    argv = _argvs(tp)[0]
    assert "--after" in argv
    assert argv[argv.index("--after") + 1] == "no_reset"
    assert argv[-3:] == ["write_flash", "0x0", "/tmp/combined.bin"]


@pytest.mark.asyncio
async def test_after_rejects_unknown_mode() -> None:
    tp = _transport()
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0")
    with pytest.raises(ValueError, match="--after"):
        await flasher.erase(after="reboot-please")


@pytest.mark.asyncio
async def test_erase_retries_transient_connect_error() -> None:
    # The exact live failure: pySerial "Could not configure port: I/O error" on
    # the first attempt (port re-enumerated mid-open), success on the second.
    tp = AsyncMock()
    tp.exec = AsyncMock(
        side_effect=[
            _result(
                1,
                stderr="A serial exception error occurred: Could not configure port: (5, 'Input/output error')",  # noqa: E501
            ),
            _result(0, stdout="Flash memory erased successfully"),
        ]
    )
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3", connect_backoff_s=0)
    await flasher.erase(after="no_reset")
    assert tp.exec.await_count == 2  # one retry, then succeeded


@pytest.mark.asyncio
async def test_no_retry_on_nontransient_failure() -> None:
    # A real verify mismatch happens AFTER chip detection → not a connect glitch.
    tp = AsyncMock()
    tp.exec = AsyncMock(
        return_value=_result(
            1,
            stdout="Chip type: ESP32-S3\nVerifying 0x... \n",
            stderr="A fatal error occurred: MD5 of file does not match data in flash!",
        )
    )
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", connect_backoff_s=0)
    with pytest.raises(FlasherToolFailed):
        await flasher.verify(Artifact(path="/tmp/x.bin"))
    assert tp.exec.await_count == 1  # connected then failed → no retry


@pytest.mark.asyncio
async def test_retry_gives_up_after_exhausting_attempts() -> None:
    tp = AsyncMock()
    tp.exec = AsyncMock(
        return_value=_result(1, stderr="Could not configure port: (5, 'Input/output error')")
    )
    flasher = EsptoolFlasher(
        transport=tp, port="/dev/ttyACM0", connect_retries=2, connect_backoff_s=0
    )
    with pytest.raises(FlasherToolFailed):
        await flasher.erase()
    assert tp.exec.await_count == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_is_in_download_mode_true_when_chip_detected() -> None:
    tp = _transport(_result(0, stdout="Chip is ESP32-S3 (revision v0.2)\nMAC: 7c:df:a1:b2:c3:d4"))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    assert await flasher.is_in_download_mode() is True
    argv = _argvs(tp)[0]
    assert argv[argv.index("--before") + 1] == "no_reset"  # checks without resetting
    assert argv[-1] == "flash_id"


@pytest.mark.asyncio
async def test_is_in_download_mode_false_when_not_connected() -> None:
    tp = _transport(_result(1, stdout="Connecting..."))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0")
    assert await flasher.is_in_download_mode() is False


@pytest.mark.asyncio
async def test_is_in_download_mode_frees_port_on_timeout() -> None:
    # A hung probe (asyncio timeout) leaves the remote esptool holding the port;
    # is_in_download_mode must kill it (fuser -k) so the next attempt isn't wedged.
    import asyncio as _aio

    calls: list[list[str]] = []

    async def _exec(argv, **kw):
        calls.append(argv)
        if "flash_id" in argv:
            raise TimeoutError()  # esptool connect hung → wait_for fires
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    assert await flasher.is_in_download_mode() is False
    assert any(a[0] == "fuser" and "-k" in a and "/dev/ttyACM0" in a for a in calls)


@pytest.mark.asyncio
async def test_enter_download_mode_touches_until_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.flashers.esptool as esptool_mod

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(esptool_mod.asyncio, "sleep", _no_sleep)
    tp = AsyncMock()
    tp.exec = AsyncMock(
        side_effect=[
            _result(1, stdout="Connecting..."),  # check #1 → not ready
            _result(0),  # stty 1200 touch
            _result(0, stdout="Chip is ESP32-S3\nMAC: 7c:df:a1:b2:c3:d4"),  # check #2 → ready
        ]
    )
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    await flasher.enter_download_mode(attempts=5, settle_s=0)
    argvs = [c.args[0] for c in tp.exec.call_args_list]
    assert any(a[0] == "stty" and "1200" in a for a in argvs)  # did a touch
    assert any("--before" in a and a[-1] == "flash_id" for a in argvs)  # probed


@pytest.mark.asyncio
async def test_enter_download_mode_raises_after_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.flashers.esptool as esptool_mod
    from hil_controller.adapters.flashers import FlasherError

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(esptool_mod.asyncio, "sleep", _no_sleep)

    async def _exec(argv, **kw):  # stty touch succeeds; flash_id probe never connects
        if argv and argv[0] == "stty":
            return _result(0)
        return _result(1, stdout="Connecting...")

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0")
    with pytest.raises(FlasherError, match="did not enter download mode"):
        await flasher.enter_download_mode(attempts=3, settle_s=0)


@pytest.mark.asyncio
async def test_verify_runs_verify_flash_at_offset() -> None:
    tp = _transport(_result(0, stdout="-- verify OK (digest matched)"))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    out = await flasher.verify(Artifact(path="/tmp/combined.bin", offset=0x0))
    argv = _argvs(tp)[0]
    assert argv[-3:] == ["verify_flash", "0x0", "/tmp/combined.bin"]
    assert "verify OK" in out


@pytest.mark.asyncio
async def test_verify_raises_on_mismatch() -> None:
    tp = _transport(_result(1, stderr="Verify failed: digest mismatch"))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", connect_retries=0)
    with pytest.raises(FlasherToolFailed):
        await flasher.verify(Artifact(path="/tmp/x.bin"))


def test_stty_1200_touch_argv() -> None:
    assert stty_1200_touch_argv("/dev/serial/by-id/usb-Adafruit_QT_Py-if00") == [
        "stty",
        "-F",
        "/dev/serial/by-id/usb-Adafruit_QT_Py-if00",
        "1200",
    ]


@pytest.mark.asyncio
async def test_bootloader_touch_1200_runs_stty(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.flashers.esptool as esptool_mod

    sleeps: list[float] = []

    async def _fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(esptool_mod.asyncio, "sleep", _fake_sleep)
    tp = _transport()
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0")
    await flasher.bootloader_touch_1200(settle_s=2.0)
    assert _argvs(tp)[0] == ["stty", "-F", "/dev/ttyACM0", "1200"]
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_custom_baud_appears_in_argv() -> None:
    tp = _transport(_result(0, stdout=ESPTOOL_FLASH_ID_S3))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3", baud=460800)
    await flasher.probe()
    argv = _argvs(tp)[0]
    assert "--baud" in argv
    assert argv[argv.index("--baud") + 1] == "460800"


# --------------------------------------------------------------------------- #
# Boot-state classification + blank-flash rectification                       #
# --------------------------------------------------------------------------- #

# A real ESP32-S3 blank-flash boot-loop snapshot (rst:0x7 watchdog, invalid hdr).
BOOT_LOG_BLANK = """\
ESP-ROM:esp32s3-20210327
rst:0x15 (USB_UART_CHIP_RESET),boot:0x8 (SPI_FAST_FLASH_BOOT)
Saved PC:0x40049b1e
invalid header: 0xffffffff
invalid header: 0xffffffff
rst:0x7 (TG0WDT_SYS_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)
invalid header: 0xffffffff
"""


def test_parse_reset_reason() -> None:
    assert parse_reset_reason(BOOT_LOG_BLANK) == "USB_UART_CHIP_RESET"
    assert parse_reset_reason("rst:0x7 (TG0WDT_SYS_RST),boot:0x8") == "TG0WDT_SYS_RST"
    assert parse_reset_reason("no reset line here") is None


def test_classify_boot_state_blank_flash() -> None:
    info = classify_boot_state(BOOT_LOG_BLANK)
    assert info["state"] == "blank_or_corrupt_flash"
    assert info["reset_reason"] == "USB_UART_CHIP_RESET"


def test_classify_boot_state_app_running() -> None:
    log = "WipperSnapper v1.0\nConnecting to WiFi...\ncpu_start: Pro cpu up."
    assert classify_boot_state(log)["state"] == "app_running"


def test_classify_boot_state_panic() -> None:
    log = "Guru Meditation Error: Core 0 panic'ed (LoadProhibited)\nBacktrace: 0x..."
    assert classify_boot_state(log)["state"] == "app_panic"


def test_classify_boot_state_download_mode() -> None:
    assert classify_boot_state("waiting for download")["state"] == "download_mode"


def test_classify_boot_state_unknown_when_silent() -> None:
    assert classify_boot_state("")["state"] == "unknown"
    assert classify_boot_state("ffff\x00garbage")["state"] == "unknown"


@pytest.mark.asyncio
async def test_force_download_via_reset_succeeds_first_attempt() -> None:
    # A single default_reset that syncs (chip detected) → True, chip held in ROM.
    tp = _transport(_result(0, stdout="Chip is ESP32-S3 (revision v0.1)\nMAC: f4:12:fa:5a:35:b4"))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    assert await flasher.force_download_via_reset(attempts=5) is True
    argv = _argvs(tp)[0]
    assert argv[argv.index("--before") + 1] == "default_reset"  # drives IO0 low
    assert argv[argv.index("--after") + 1] == "no_reset"  # holds in download
    assert argv[-1] == "flash_id"


@pytest.mark.asyncio
async def test_force_download_via_reset_gives_up_after_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hil_controller.adapters.flashers.esptool as esptool_mod

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(esptool_mod.asyncio, "sleep", _no_sleep)
    # Never syncs (device keeps disconnecting) → False after the attempt budget.
    tp = _transport(_result(1, stdout="Connecting...."))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    assert await flasher.force_download_via_reset(attempts=3) is False
    assert len(_argvs(tp)) == 3  # exactly the attempt budget


@pytest.mark.asyncio
async def test_read_boot_log_builds_reconnecting_reader() -> None:
    tp = _transport(_result(0, stdout="ESP-ROM:esp32s3\ninvalid header: 0xffffffff"))
    flasher = EsptoolFlasher(transport=tp, port="/dev/ttyACM0", chip="esp32s3")
    text = await flasher.read_boot_log(seconds=2, baud=115200)
    argv = _argvs(tp)[0]
    assert argv[0] == "bash" and argv[1] == "-c"
    script = argv[2]
    assert "/dev/ttyACM0" in script and "115200" in script and "head -c" in script
    assert "invalid header" in text
