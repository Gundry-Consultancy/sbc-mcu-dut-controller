"""Tests for the composable firmware-bench stage pipeline.

Each stage dispatches to a real adapter (EsptoolFlasher / PioUploadFlasher /
SolenoidHubAdapter) driven against an AsyncMock transport; assertions are on
the exact argv that reaches the wire and on pipeline ordering/validation.
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.bench_stages import (
    DEFAULT_FLASH_STAGES,
    BenchContext,
    HostUsbWedgedError,
    StageError,
    _await_serial_reboot,
    _detect_reboot,
    _recover_download_via_hub,
    run_stages,
    validate_stages,
)
from hil_controller.adapters.flashers.base import Artifact, FlasherError
from hil_controller.hosts.base import ExecResult


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


def _transport() -> AsyncMock:
    t = AsyncMock()
    t.exec = AsyncMock(
        return_value=_result(0, stdout="Wrote 1000 bytes at 0x00000000 in 1.0 seconds")
    )
    return t


def _argvs(t: AsyncMock) -> list[list[str]]:
    return [c.args[0] for c in t.exec.call_args_list]


def _ctx(transport: AsyncMock, hub: AsyncMock | None = None, **kw) -> BenchContext:
    return BenchContext(
        dut_transport=transport,
        hub_transport=hub or transport,
        flash_serial_port="/dev/serial/by-id/usb-Adafruit_QT_Py-if00",
        artifact=Artifact(path="/tmp/combined.bin", kind="combined_bin", offset=0),
        esptool_chip="esp32s3",
        **kw,
    )


# --------------------------------------------------------------------------- #
# validation                                                                  #
# --------------------------------------------------------------------------- #


def test_validate_stages_rejects_unknown_type() -> None:
    with pytest.raises(StageError, match="unknown bench stage type"):
        validate_stages([{"type": "flash"}, {"type": "frobnicate"}])


def test_validate_stages_accepts_default_cycle() -> None:
    validate_stages(DEFAULT_FLASH_STAGES)  # no raise


@pytest.mark.asyncio
async def test_run_stages_aborts_on_unknown_type() -> None:
    tp = _transport()
    with pytest.raises(StageError):
        await run_stages([{"type": "nope"}], _ctx(tp))
    assert tp.exec.await_count == 0  # fails before touching the wire


# --------------------------------------------------------------------------- #
# default cycle ordering: erase(no_reset) → flash(no_reset) → verify → power   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_enter_bootloader_skips_reboot_when_already_in_download_mode() -> None:
    # flash_id probe returns chip info → already in download mode → no reboot/touch.
    tp = AsyncMock()
    tp.exec = AsyncMock(return_value=_result(0, stdout="Chip is ESP32-S3\nMAC: aa:bb:cc:dd:ee:ff"))
    ctx = _ctx(tp, device={"solenoid_channel": 4})
    await run_stages([{"type": "enter_bootloader"}], ctx)
    argvs = _argvs(tp)
    assert all(a[0] != "stty" for a in argvs)  # no 1200-touch needed
    assert all("port_off" not in a for a in argvs)  # no solenoid reboot


_BLANK_BOOT_LOG = (
    "ESP-ROM:esp32s3-20210327\n"
    "rst:0x7 (TG0WDT_SYS_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)\n"
    "invalid header: 0xffffffff\n"
)


@pytest.mark.asyncio
async def test_enter_bootloader_rectifies_blank_flash_via_usbjtag_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Blank/boot-looping board: the 1200-touch never lands, so enter_bootloader
    # diagnoses (reads boot log → invalid header) and rectifies via the
    # USB-Serial/JTAG reset (--before default_reset) — no hub power-cycle needed.
    import hil_controller.adapters.flashers.esptool as esptool_mod

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(esptool_mod.asyncio, "sleep", _no_sleep)

    async def _exec(argv, **kw):
        if argv and argv[0] == "stty":
            return _result(0)  # touch succeeds
        if argv and argv[0] == "bash":
            return _result(0, stdout=_BLANK_BOOT_LOG)  # read_boot_log
        if "flash_id" in argv:
            if "default_reset" in argv:  # force_download → syncs
                return _result(0, stdout="Chip is ESP32-S3\nMAC: f4:12:fa:5a:35:b4")
            return _result(1, stdout="Connecting....")  # no_reset probe → not in ROM
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    ctx = _ctx(tp, device={"solenoid_channel": 4})
    await run_stages(
        [
            {
                "type": "enter_bootloader",
                "attempts": 2,
                "settle_s": 0,
                "diagnose_s": 1,
                "reset_attempts": 3,
            }
        ],
        ctx,
    )
    argvs = _argvs(tp)
    assert any(a and a[0] == "bash" for a in argvs)  # diagnosed (read boot log)
    assert any(
        "flash_id" in a and "default_reset" in a for a in argvs
    )  # rectified via USB-JTAG reset
    assert all("port_off" not in a for a in argvs)  # no hub power-cycle needed


@pytest.mark.asyncio
async def test_inject_pixelwrite_stage_records_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.bench_stages as bs

    class _FakeInjector:
        def __init__(self, **kw) -> None:
            self.api_url = "http://127.0.0.1:5173"

        async def wait_for_checkin(self, timeout):
            return "io-wipper-qtpyX"

        async def fire_pixel_write(self, uid, pin="D0", color=200):
            return {
                "topic": f"hil/wprsnpr/{uid}/signals/broker/pixel",
                "payload_hex": "1a 09 08 01 12 02 44 30 18 c8 01",
                "echo_response": {"status": "OK"},
            }

        async def observe_reboot(self, uid, timeout):
            return True  # crash build → device re-checks-in

    monkeypatch.setattr(bs, "WsSignalInjector", _FakeInjector)
    tp = _transport()
    emitted: list[str] = []
    ctx = _ctx(
        tp,
        emit=emitted.append,
        protomq_host="127.0.0.1",
        protomq_port=1884,
        secrets={"IO_USERNAME": "hil"},
    )
    await run_stages([{"type": "inject_pixelwrite", "pin": "D0", "color": 200}], ctx)
    assert any("PIXELWRITE_VERDICT rebooted=true" in m for m in emitted)
    assert any("1a 09 08 01 12 02 44 30 18 c8 01" in m for m in emitted)  # exact payload logged
    assert getattr(ctx, "pixelwrite_rebooted") is True


@pytest.mark.asyncio
async def test_inject_pixelwrite_requires_protomq() -> None:
    tp = _transport()
    ctx = _ctx(tp)  # no protomq_host/port set
    with pytest.raises(StageError, match="needs protomq"):
        await run_stages([{"type": "inject_pixelwrite"}], ctx)


@pytest.mark.asyncio
async def test_inject_pixelwrite_fails_when_no_checkin(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.bench_stages as bs

    class _FakeInjector:
        def __init__(self, **kw) -> None:
            self.api_url = "http://127.0.0.1:5173"

        async def wait_for_checkin(self, timeout):
            return None  # device never checked in

    monkeypatch.setattr(bs, "WsSignalInjector", _FakeInjector)
    ctx = _ctx(_transport(), protomq_host="127.0.0.1", protomq_port=1884)
    with pytest.raises(StageError, match="no DUT checkin"):
        await run_stages([{"type": "inject_pixelwrite", "checkin_timeout_s": 1}], ctx)


@pytest.mark.asyncio
async def test_verify_checkin_records_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.bench_stages as bs

    class _FakeInjector:
        def __init__(self, **kw) -> None:
            pass

        async def wait_for_checkin(self, timeout):
            return "io-wipper-qtpyX"  # device checked in

    monkeypatch.setattr(bs, "WsSignalInjector", _FakeInjector)
    emitted: list[str] = []
    ctx = _ctx(
        _transport(),
        emit=emitted.append,
        protomq_host="127.0.0.1",
        protomq_port=1884,
        secrets={"IO_USERNAME": "hil"},
    )
    await run_stages([{"type": "verify_checkin"}], ctx)
    assert any("CHECKIN_VERDICT ok=true" in m for m in emitted)
    assert getattr(ctx, "checkin_ok") is True


@pytest.mark.asyncio
async def test_verify_checkin_fails_when_no_checkin(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.bench_stages as bs

    class _FakeInjector:
        def __init__(self, **kw) -> None:
            pass

        async def wait_for_checkin(self, timeout):
            return None

    monkeypatch.setattr(bs, "WsSignalInjector", _FakeInjector)
    ctx = _ctx(_transport(), protomq_host="127.0.0.1", protomq_port=1884)
    with pytest.raises(StageError, match="no DUT checkin"):
        await run_stages([{"type": "verify_checkin", "checkin_timeout_s": 1}], ctx)


@pytest.mark.asyncio
async def test_verify_checkin_requires_protomq() -> None:
    ctx = _ctx(_transport())  # no protomq
    with pytest.raises(StageError, match="needs protomq"):
        await run_stages([{"type": "verify_checkin"}], ctx)


@pytest.mark.asyncio
async def test_diagnose_stage_reads_and_classifies() -> None:
    tp = AsyncMock()
    tp.exec = AsyncMock(return_value=_result(0, stdout=_BLANK_BOOT_LOG))
    emitted: list[str] = []
    ctx = _ctx(tp, emit=emitted.append)
    await run_stages([{"type": "diagnose", "diagnose_s": 1}], ctx)
    assert any(a and a[0] == "bash" for a in _argvs(tp))  # read serial
    assert any("blank_or_corrupt_flash" in m for m in emitted)  # classified + logged
    assert any("TG0WDT_SYS_RST" in m for m in emitted)  # reset reason surfaced


@pytest.mark.asyncio
async def test_flash_cycle_emits_expected_argv_in_order() -> None:
    tp = _transport()
    # The erase→flash→verify→power slice (enter_bootloader covered separately).
    # No solenoid channel → power_cycle falls back to esptool soft reset.
    cycle = [s for s in DEFAULT_FLASH_STAGES if s["type"] != "enter_bootloader"]
    await run_stages(cycle, _ctx(tp, device={}))
    argvs = _argvs(tp)
    assert len(argvs) == 4

    erase, flash, verify, reset = argvs
    assert erase[-3:] == ["--after", "no_reset", "erase_flash"]
    assert "--after" in flash and flash[-3:] == ["write_flash", "0x0", "/tmp/combined.bin"]
    assert verify[-3:] == ["verify_flash", "0x0", "/tmp/combined.bin"]
    # soft-reset fallback: flash_id with --after watchdog_reset (native-USB-safe;
    # only falls back to hard_reset if the watchdog reset itself fails)
    assert "--after" in reset and reset[reset.index("--after") + 1] == "watchdog_reset"
    assert reset[-1] == "flash_id"


@pytest.mark.asyncio
async def test_power_cycle_no_solenoid_pauses_serial_around_esptool_reset() -> None:
    """No-solenoid power_cycle must pause the serial capture around the esptool reset
    (they'd otherwise fight over the DUT's single CDC port), then resume to catch
    the boot/checkin log."""
    tp = _transport()
    calls: list[str] = []

    async def _pause() -> None:
        calls.append("pause")

    async def _resume() -> None:
        calls.append("resume")

    ctx = _ctx(tp, device={}, pause_serial=_pause, resume_serial=_resume)
    await run_stages([{"type": "power_cycle"}], ctx)
    assert calls == ["pause", "resume"]  # capture released then re-taken
    # the esptool reset (watchdog_reset flash_id) ran between them
    assert any("--after" in a and "watchdog_reset" in a and a[-1] == "flash_id" for a in _argvs(tp))


# --------------------------------------------------------------------------- #
# power_cycle with a real solenoid channel                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recover_via_hub_raises_host_wedged_when_node_absent() -> None:
    """Full recovery exhausted (touch + USB-JTAG reset + hub cycle) AND the serial
    node never appears → HostUsbWedgedError (not a generic StageError), so the
    controller can flag the host for reboot."""

    class _FakeFlasher:
        async def force_download_via_reset(self, **kw):
            return False

        async def enter_download_mode(self, **kw):
            raise FlasherError("device did not enter download mode")

    async def dut_exec(argv, **kw):
        if argv[:2] == ["test", "-e"]:
            return _result(1)  # by-path node ABSENT
        return _result(0)

    dut = _transport()
    dut.exec = AsyncMock(side_effect=dut_exec)
    ctx = _ctx(dut, hub=_transport(), device={"solenoid_channel": 4})
    ctx.make_flasher = lambda which: _FakeFlasher()  # type: ignore[method-assign]
    with pytest.raises(HostUsbWedgedError):
        await _recover_download_via_hub({}, ctx, reason="touch + USB-JTAG reset failed")


@pytest.mark.asyncio
async def test_recover_via_hub_reraises_generic_when_node_present() -> None:
    """If the node IS present but download entry still fails, it's a stuck board,
    not a wedged host — keep the generic FlasherError (no false host-reboot)."""

    class _FakeFlasher:
        async def force_download_via_reset(self, **kw):
            return False

        async def enter_download_mode(self, **kw):
            raise FlasherError("stuck")

    async def dut_exec(argv, **kw):
        return _result(0)  # node present

    dut = _transport()
    dut.exec = AsyncMock(side_effect=dut_exec)
    ctx = _ctx(dut, hub=_transport(), device={"solenoid_channel": 4})
    ctx.make_flasher = lambda which: _FakeFlasher()  # type: ignore[method-assign]
    with pytest.raises(FlasherError):
        await _recover_download_via_hub({}, ctx, reason="x")
    # and it must NOT be the wedged subtype
    try:
        await _recover_download_via_hub({}, ctx, reason="x")
    except HostUsbWedgedError:  # pragma: no cover
        pytest.fail("should not flag host wedged when the node is present")
    except FlasherError:
        pass


@pytest.mark.asyncio
async def test_power_cycle_uses_solenoid_when_channel_mapped() -> None:
    dut = _transport()
    hub = _transport()
    ctx = _ctx(dut, hub=hub, device={"solenoid_channel": 3})
    # await_enumeration:false → the timed power_cycle path (no presence probes).
    await run_stages(
        [{"type": "power_cycle", "off_s": 1.0, "settle_s": 0, "await_enumeration": False}], ctx
    )
    hub_argvs = _argvs(hub)
    # power_cycle == port_off then port_on on channel 3
    assert any("port_off" in a and "3" in a for a in hub_argvs)
    assert any("port_on" in a and "3" in a for a in hub_argvs)
    assert dut.exec.await_count == 0  # no esptool fallback when channel exists


@pytest.mark.asyncio
async def test_power_cycle_awaits_disappear_then_reappear() -> None:
    """Default path: confirm present → power off → await the by-path node vanish
    → power on → await it re-enumerate, all via `test -e` probes on the DUT."""
    hub = _transport()
    # test -e exit codes: present (pre-check) → absent (disappeared) → present (back)
    seq = iter([0, 1, 0])

    async def dut_exec(argv, **kw):
        if argv[:2] == ["test", "-e"]:
            return _result(next(seq, 0))
        return _result(0)

    dut = _transport()
    dut.exec = AsyncMock(side_effect=dut_exec)
    ctx = _ctx(dut, hub=hub, device={"solenoid_channel": 3})
    await run_stages([{"type": "power_cycle", "off_s": 0, "settle_s": 0}], ctx)
    hub_argvs = _argvs(hub)
    assert any("port_off" in a and "3" in a for a in hub_argvs)
    assert any("port_on" in a and "3" in a for a in hub_argvs)
    # presence was probed (pre-check + disappear + reappear)
    probes = [a for a in _argvs(dut) if a[:2] == ["test", "-e"]]
    assert len(probes) >= 3


# --------------------------------------------------------------------------- #
# per-stage path/offset overrides (multi-image flashing)                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_await_serial_reboot_detects_marker_past_offset(tmp_path) -> None:
    log = tmp_path / "serial.log"
    log.write_text("pre-inject boot: WipperSnapper found these WiFi networks:\n")
    offset = log.stat().st_size  # everything above is pre-inject — must be ignored
    # No reset banner yet → times out False.
    assert await _await_serial_reboot(str(log), offset, timeout_s=0.6) is False
    # Append a crash reset banner → detected.
    with open(log, "a") as fh:
        fh.write("ERROR: ...\nESP-ROM:esp32s3-20210327\n")
    assert await _await_serial_reboot(str(log), offset, timeout_s=2.0) is True


class _FakeInjector:
    def __init__(self, mqtt_rebooted: bool, delay: float = 0.0) -> None:
        self._r = mqtt_rebooted
        self._delay = delay

    async def observe_reboot(self, uid: str, *, timeout: float) -> bool:
        if not self._r:
            await asyncio.sleep(min(timeout, 5.0))  # silent until the window ends
            return False
        await asyncio.sleep(self._delay)
        return True


@pytest.mark.asyncio
async def test_detect_reboot_serial_beats_slow_mqtt(tmp_path) -> None:
    log = tmp_path / "serial.log"
    log.write_text("rst:0x1 (POWERON)\nESP-ROM:esp32s3\n")  # crash banner already present
    inj = _FakeInjector(mqtt_rebooted=False)  # MQTT would never re-checkin in time
    rebooted, how = await _detect_reboot(
        inj, "uid-1", serial_path=str(log), serial_offset=0, timeout_s=2.0
    )
    assert rebooted is True and how == "serial reset banner"


@pytest.mark.asyncio
async def test_detect_reboot_mqtt_when_no_serial(tmp_path) -> None:
    log = tmp_path / "serial.log"
    log.write_text("(graceful) ERROR: Pixel strand not found\n")  # no reset banner
    inj = _FakeInjector(mqtt_rebooted=True)
    rebooted, how = await _detect_reboot(
        inj, "uid-1", serial_path=str(log), serial_offset=0, timeout_s=2.0
    )
    assert rebooted is True and how == "MQTT re-checkin"


@pytest.mark.asyncio
async def test_detect_reboot_survived_when_neither(tmp_path) -> None:
    log = tmp_path / "serial.log"
    log.write_text("ERROR: Pixel strand not found, can not write a color!\n")
    inj = _FakeInjector(mqtt_rebooted=False)
    rebooted, how = await _detect_reboot(
        inj, "uid-1", serial_path=str(log), serial_offset=0, timeout_s=1.0
    )
    assert rebooted is False


@pytest.mark.asyncio
async def test_flash_stage_offset_override_accepts_hex_string() -> None:
    tp = _transport()
    await run_stages(
        [{"type": "flash", "path": "/tmp/app.bin", "offset": "0x10000"}],
        _ctx(tp),
    )
    argv = _argvs(tp)[0]
    assert argv[-3:] == ["write_flash", "0x10000", "/tmp/app.bin"]


@pytest.mark.asyncio
async def test_flash_stage_falls_back_to_context_artifact() -> None:
    tp = _transport()
    await run_stages([{"type": "flash"}], _ctx(tp))
    argv = _argvs(tp)[0]
    assert argv[-3:] == ["write_flash", "0x0", "/tmp/combined.bin"]


@pytest.mark.asyncio
async def test_flash_stage_without_image_raises() -> None:
    tp = _transport()
    ctx = BenchContext(
        dut_transport=tp,
        hub_transport=tp,
        flash_serial_port="/dev/ttyACM0",
        artifact=None,
    )
    with pytest.raises(StageError, match="no image"):
        await run_stages([{"type": "flash"}], ctx)


# --------------------------------------------------------------------------- #
# flasher selection                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_flash_stage_via_pio_uses_pio_run() -> None:
    tp = _transport()
    ctx = _ctx(tp, workspace_dir="/tmp/hil/ws", pio_env="adafruit_qtpy_esp32s3_n4r2")
    await run_stages([{"type": "flash", "flasher": "pio"}], ctx)
    argv = _argvs(tp)[0]
    assert argv[0] == "bash"
    assert "pio run -e adafruit_qtpy_esp32s3_n4r2 --target upload" in argv[2]


@pytest.mark.asyncio
async def test_unknown_flasher_raises() -> None:
    tp = _transport()
    with pytest.raises(StageError, match="unknown flasher"):
        await run_stages([{"type": "flash", "flasher": "no-such-flasher"}], _ctx(tp))


@pytest.mark.asyncio
async def test_flash_recovers_via_power_cycle_after_write_wedge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First write_flash wedges (JTAG "Write timeout"); the stage power-cycles +
    # re-enters download mode and the retry succeeds.
    import hil_controller.adapters.flashers.esptool as esptool_mod

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(esptool_mod.asyncio, "sleep", _no_sleep)
    writes = {"n": 0}

    async def _exec(argv, **kw):
        if "write_flash" in argv:
            writes["n"] += 1
            if writes["n"] == 1:  # non-transient fail → no internal retry, stage recovers
                return _result(1, stdout="Stub flasher running\nA fatal error occurred: wedged")
            return _result(0, stdout="Wrote 16 bytes at 0x0")
        if "flash_id" in argv:  # is_in_download_mode probe → already there after power-cycle
            return _result(0, stdout="Chip is ESP32-S3\nMAC: aa:bb:cc:dd:ee:ff")
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    lines: list[str] = []
    ctx = _ctx(tp, device={"solenoid_channel": 4}, emit=lines.append)
    # min_retry_s=0 → skip the quick-retry window so a wedge escalates straight
    # to the power-cycle recovery (kept fast + deterministic).
    await run_stages(
        [{"type": "flash", "before": "no_reset", "after": "no_reset", "min_retry_s": 0}], ctx
    )

    assert writes["n"] == 2  # failed once, succeeded on the retry
    argvs = _argvs(tp)
    assert any("port_off" in a and "4" in a for a in argvs)  # hub power-cycle happened
    assert any("port_on" in a and "4" in a for a in argvs)
    assert any("recovering" in ln for ln in lines)


@pytest.mark.asyncio
async def test_flash_no_recovery_without_solenoid_channel() -> None:
    # No channel → no power-cycle recovery; the flasher error propagates.
    async def _exec(argv, **kw):
        if "write_flash" in argv:
            return _result(1, stdout="Stub flasher running\nA fatal error occurred: wedged")
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    with pytest.raises(Exception):  # FlasherToolFailed propagates
        await run_stages([{"type": "flash", "min_retry_s": 0}], _ctx(tp, device={}))


@pytest.mark.asyncio
async def test_erase_retries_within_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transient glitch on the first erase is retried (within the ≥10s window)
    # and succeeds — no power-cycle, no failure.
    import hil_controller.adapters.bench_stages as bs

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(bs.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def _exec(argv, **kw):
        if "erase_flash" in argv:
            calls["n"] += 1
            if calls["n"] == 1:
                return _result(1, stdout="Stub flasher running\nA fatal error occurred: glitch")
            return _result(0)
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    lines: list[str] = []
    ctx = _ctx(tp, device={}, emit=lines.append)
    await run_stages([{"type": "erase", "after": "no_reset"}], ctx)  # no raise
    assert calls["n"] == 2  # retried once, then succeeded
    assert any("erase attempt 1 failed" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# bootloader touch                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bootloader_touch_runs_stty_at_1200(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.flashers.esptool as esptool_mod

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(esptool_mod.asyncio, "sleep", _no_sleep)
    tp = _transport()
    await run_stages([{"type": "bootloader_touch", "settle_s": 0}], _ctx(tp))
    argv = _argvs(tp)[0]
    assert argv == ["stty", "-F", "/dev/serial/by-id/usb-Adafruit_QT_Py-if00", "1200"]


# --------------------------------------------------------------------------- #
# log sink                                                                     #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# write_secrets_msc stage                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_write_secrets_msc_stage_uses_protomq_port_and_filter() -> None:
    async def _exec(argv, **kwargs):
        if argv[:2] == ["ls", "-1"]:
            return _result(0, stdout="usb-Adafruit_QT_Py_ESP32-S3-0:0")
        if argv[:3] == ["udisksctl", "mount", "-b"]:
            return _result(0, stdout="Mounted /dev/sda at /media/pi/WIPPER.")
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    ctx = _ctx(
        tp,
        device={},
        msc_filter="QT_Py_ESP32-S3",
        protomq_host="192.168.1.169",
        protomq_port=1885,
        secrets={"IO_USERNAME": "bench", "IO_KEY": "k", "WIFI_SSID": "N", "WIFI_PASSWORD": "p"},
    )
    await run_stages([{"type": "write_secrets_msc"}], ctx)

    tee_call = next(c for c in tp.exec.call_args_list if c.args[0][0] == "tee")
    import json as _json

    body = _json.loads(tee_call.kwargs["stdin"].decode())
    assert body["io_url"] == "192.168.1.169"
    assert body["io_port"] == 1885
    assert body["network_type_wifi"]["network_ssid"] == "N"


@pytest.mark.asyncio
async def test_write_secrets_msc_stage_requires_broker() -> None:
    tp = _transport()
    ctx = _ctx(tp, msc_filter="X", protomq_port=0)  # no port → cannot point firmware anywhere
    with pytest.raises(StageError, match="broker host"):
        await run_stages([{"type": "write_secrets_msc"}], ctx)


@pytest.mark.asyncio
async def test_transcript_captures_commands_and_output() -> None:
    chip = (
        "esptool.py v4.7\nChip is ESP32-S3 (revision v0.2)\nMAC: 7c:df:a1:b2:c3:d4\n"
        "Wrote 1000 bytes at 0x00000000\nHash of data verified.\n"
    )
    tp = AsyncMock()
    tp.exec = AsyncMock(return_value=_result(0, stdout=chip))
    lines: list[str] = []
    ctx = _ctx(tp, device={}, emit=lines.append)
    await run_stages([{"type": "flash", "offset": "0x0"}], ctx)

    assert len(ctx.transcript) == 1
    entry = ctx.transcript[0]
    assert "write_flash 0x0 /tmp/combined.bin" in entry["cmd"]
    assert entry["exit"] == 0
    assert "Chip is ESP32-S3" in entry["stdout"]
    # full transcript text (the downloadable flash.log) carries cmd + output
    txt = ctx.transcript_text()
    assert (
        "$ python3 -m esptool" in txt
        and "MAC: 7c:df:a1:b2:c3:d4" in txt
        and "Hash of data verified" in txt
    )
    # ALL output (not a whitelist) surfaced to the live log, with a UTC-ms stamp
    assert any("Chip is ESP32-S3" in ln for ln in lines)
    assert any("Hash of data verified" in ln for ln in lines)
    assert any(re.search(r"\d{4}-\d\d-\d\dT.*→ exit 0", ln) for ln in lines)


def test_record_masks_secret_values_last4() -> None:
    # `tee` echoes secrets.json to stdout; the credential value must be masked to
    # last-4 in BOTH the stored transcript (flash.log) and the live stream, while
    # the command, the field names, and the public username stay readable.
    tp = AsyncMock()
    lines: list[str] = []
    ctx = _ctx(
        tp,
        device={},
        emit=lines.append,
        secrets={
            "IO_USERNAME": "playground_example",
            "IO_KEY": "aio_realKEY9876",
            "WIFI_SSID": "bench-wifi",
            "WIFI_PASSWORD": "hunter2pass",
        },
    )
    body = (
        '{"io_username": "playground_example", "io_key": "aio_realKEY9876", '
        '"wifi_password": "hunter2pass"}'
    )
    ctx.record(["tee", "/media/pi/WIPPER/secrets.json"], _result(0, stdout=body))

    entry = ctx.transcript[0]
    blob = entry["stdout"] + "\n".join(lines)
    assert "aio_realKEY9876" not in blob and "hunter2pass" not in blob  # full secrets gone
    assert "****9876" in entry["stdout"] and "****pass" in entry["stdout"]  # last-4 shown
    assert "io_key" in entry["stdout"]  # field names (args) preserved
    assert "playground_example" in entry["stdout"]  # public username not masked
    assert "tee /media/pi/WIPPER/secrets.json" in entry["cmd"]


@pytest.mark.asyncio
async def test_full_stderr_surfaced_to_live_log_even_on_success() -> None:
    """stderr (e.g. esptool deprecation warnings) reaches the live CI feed even
    when the command exits 0 — previously stderr was only shown on failure."""
    tp = AsyncMock()
    tp.exec = AsyncMock(
        return_value=_result(
            0,
            stdout="Erasing flash memory (this may take a while)...\nFlash memory erased successfully in 17.7 seconds.",  # noqa: E501
            stderr="Warning: Deprecated: Command 'erase_flash' is deprecated. Use 'erase-flash' instead.",  # noqa: E501
        )
    )
    lines: list[str] = []
    ctx = _ctx(tp, device={}, emit=lines.append)
    await run_stages([{"type": "erase", "after": "no_reset"}], ctx)
    blob = "\n".join(lines)
    assert "Flash memory erased successfully" in blob  # erase progress now visible live
    assert "erase-flash' instead" in blob and "[stderr]" in blob  # deprecation warning surfaced


@pytest.mark.asyncio
async def test_log_level_filtered_uses_allow_list_for_live_feed() -> None:
    """log_level=filtered: the live feed shows only allow-list lines + a summary,
    but the flash.log transcript still captures EVERYTHING."""
    chip = (
        "esptool v5.2.0\nConnecting...\nChip is ESP32-S3 (revision v0.2)\n"
        "Some chatty internal detail line that is not notable\n"
        "Hash of data verified.\n"
    )
    tp = AsyncMock()
    tp.exec = AsyncMock(return_value=_result(0, stdout=chip, stderr="Warning: Deprecated: x"))
    lines: list[str] = []
    ctx = _ctx(tp, device={}, emit=lines.append, stream_log_level="filtered")
    await run_stages([{"type": "flash", "offset": "0x0"}], ctx)

    blob = "\n".join(lines)
    assert "Chip is ESP32-S3" in blob and "Hash of data verified" in blob  # allow-list kept
    assert "chatty internal detail" not in blob  # non-notable filtered out
    assert "Warning: Deprecated" not in blob  # stderr hidden on success
    # …but the downloadable transcript has the full output regardless of level
    txt = ctx.transcript_text()
    assert "chatty internal detail" in txt and "Warning: Deprecated" in txt


@pytest.mark.asyncio
async def test_emit_callback_receives_stage_progress() -> None:
    tp = _transport()
    lines: list[str] = []
    ctx = _ctx(tp, device={}, emit=lines.append)
    await run_stages([{"type": "erase", "after": "no_reset"}], ctx)
    assert any("stage 1/1: erase" in ln for ln in lines)
    assert any("erase via esptool" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# print_boot_log stage                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_print_boot_log_surfaces_contents(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.bench_stages as bs

    async def _fake_read(transport, *, msc_filter, globs):
        assert msc_filter == "QT_Py"
        return {"/tmp/hil-msc-sda/wipper_boot_out.txt": "Board: QT Py\nWipperSnapper v1.0"}

    monkeypatch.setattr(bs, "read_msc_files", _fake_read)
    lines: list[str] = []
    ctx = _ctx(_transport(), device={}, msc_filter="QT_Py", emit=lines.append)
    await run_stages([{"type": "print_boot_log"}], ctx)
    assert any("boot log: wipper_boot_out.txt" in ln for ln in lines)
    assert any("WipperSnapper v1.0" in ln for ln in lines)


@pytest.mark.asyncio
async def test_print_boot_log_tolerates_unstable_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    import hil_controller.adapters.bench_stages as bs

    async def _raise(*a, **k):
        raise bs.MscError("No medium found")

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(bs, "read_msc_files", _raise)
    monkeypatch.setattr(bs.asyncio, "sleep", _no_sleep)
    lines: list[str] = []
    ctx = _ctx(_transport(), device={}, msc_filter="QT_Py", emit=lines.append)
    # best-effort: must NOT raise even though the drive never appears
    await run_stages([{"type": "print_boot_log", "attempts": 2}], ctx)
    assert any("boot log unavailable" in ln for ln in lines)
