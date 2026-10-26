"""Tests for the board-rescue feature: the serial write primitive, the
serial_probe / enter_bootsel / picotool_rescue / esptool_rescue stages, the
picotool flasher, and the bare-job parameter.

Follows the test_bench_stages patterns: AsyncMock transports returning
ExecResult-shaped MagicMocks, assertions on the exact argv that reaches the
wire, and monkeypatched asyncio.sleep so nothing waits.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import hil_controller.adapters.bench_stages as bs
from hil_controller.adapters.bench_stages import (
    RESCUE_PROBE_STAGES,
    BenchContext,
    StageError,
    classify_probe_transcript,
    resolve_probe_poke,
    run_stages,
    validate_stages,
)
from hil_controller.adapters.flashers.base import Artifact
from hil_controller.adapters.flashers.picotool import (
    PicotoolFlasher,
    needs_bootsel,
    parse_picotool_info,
)
from hil_controller.adapters.serial_capture import (
    SerialWriteError,
    pyserial_write_argv,
    write_serial_bytes,
)
from hil_controller.adapters.solenoid_hub import SolenoidHubAdapter
from hil_controller.hosts.base import ExecResult


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


def _transport(result: MagicMock | None = None) -> AsyncMock:
    t = AsyncMock()
    t.exec = AsyncMock(return_value=result or _result(0))
    return t


def _argvs(t: AsyncMock) -> list[list[str]]:
    return [c.args[0] for c in t.exec.call_args_list]


def _ctx(transport: AsyncMock, **kw) -> BenchContext:
    kw.setdefault("artifact", Artifact(path="/tmp/hil/job/firmware.bin", kind="bin", offset=0))
    return BenchContext(
        dut_transport=transport,
        hub_transport=kw.pop("hub", None) or transport,
        flash_serial_port="/dev/serial/by-id/usb-Raspberry_Pi_Pico-if00",
        esptool_chip="esp32s3",
        **kw,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    async def _zero(_s: float) -> None:
        return None

    monkeypatch.setattr(bs.asyncio, "sleep", _zero)


# --------------------------------------------------------------------------- #
# serial write primitive                                                      #
# --------------------------------------------------------------------------- #


def test_pyserial_write_argv_shape() -> None:
    argv = pyserial_write_argv("/dev/serial/by-id/x", b"\x03", baud=115200)
    # sudo -n: the service user isn't in dialout on stock bench hosts, so the
    # one-shot opens the port the same way the bench's other root one-shots do.
    assert argv[:4] == ["sudo", "-n", "python3", "-c"]
    assert argv[5:] == ["/dev/serial/by-id/x", "115200", "03", "none"]
    assert "unhexlify" in argv[4] and "dtr = True" in argv[4]


def test_pyserial_write_argv_rts_flag() -> None:
    assert pyserial_write_argv("/p", b"", rts=True)[-1] == "1"
    assert pyserial_write_argv("/p", b"", rts=False)[-1] == "0"


async def test_write_serial_bytes_ok() -> None:
    tp = _transport()
    await write_serial_bytes(tp, "/dev/ttyACM0", b"\r\n")
    assert tp.exec.await_count == 1
    argv = tp.exec.call_args.args[0]
    assert argv[:2] == ["sudo", "-n"]
    assert argv[5] == "/dev/ttyACM0"


async def test_write_serial_bytes_raises_on_failure() -> None:
    tp = _transport(_result(1, stderr="could not open port"))
    with pytest.raises(SerialWriteError, match="could not open port"):
        await write_serial_bytes(tp, "/dev/ttyACM0", b"\x03")


# --------------------------------------------------------------------------- #
# probe classification                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "silent"),
        ("   \n", "silent"),
        ("Adafruit CircuitPython 9.2.0 on 2024-10-28\n>>> ", "circuitpython_repl"),
        ("KeyboardInterrupt\n>>> ", "circuitpython_repl"),
        ("MicroPython v1.22.1 on 2024-01-05\n>>> ", "micropython_repl"),
        ("Running in safe mode! Not running saved code.", "circuitpython_safe_mode"),
        ("rst:0x7 (TG0WDT_SYS_RST)\ninvalid header: 0xffffffff", "boot_banner_detected"),
        ("Adafruit CircuitPython 9.2.0; no prompt yet", "boot_banner_detected"),
        ("garbage \x00\x00 noise", "other"),
    ],
)
def test_classify_probe_transcript(text: str, expected: str) -> None:
    assert classify_probe_transcript(text) == expected


def test_resolve_probe_poke_named_and_hex() -> None:
    assert resolve_probe_poke("ctrl_c") == ("ctrl_c", b"\x03")
    assert resolve_probe_poke({"hex": "0d 0a"}) == ("hex:0d0a", b"\r\n")
    with pytest.raises(StageError, match="unknown poke"):
        resolve_probe_poke("ctrl_z")
    with pytest.raises(StageError, match="bad hex"):
        resolve_probe_poke({"hex": "zz"})


# --------------------------------------------------------------------------- #
# serial_probe stage                                                          #
# --------------------------------------------------------------------------- #


def _probe_ctx(tmp_path: Path, reply: str) -> tuple[AsyncMock, BenchContext, list[str]]:
    """A ctx whose serial.log 'receives' *reply* whenever the poke write runs."""
    serial_log = tmp_path / "serial.log"
    serial_log.write_text("")
    probe_log = tmp_path / "serial-probe.log"

    async def _exec(argv, **kw):
        if argv and argv[0] == "sudo" and "-c" in argv:  # the pyserial one-shot write
            with serial_log.open("a", encoding="utf-8") as fh:
                fh.write(reply)
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    emitted: list[str] = []
    ctx = _ctx(
        tp,
        serial_log_path=str(serial_log),
        probe_log_path=str(probe_log),
        emit=emitted.append,
    )
    return tp, ctx, emitted


async def test_serial_probe_classifies_and_writes_asset(tmp_path: Path) -> None:
    tp, ctx, emitted = _probe_ctx(tmp_path, "Adafruit CircuitPython 9.2.0\n>>> \n")
    await run_stages(
        [{"type": "serial_probe", "pokes": ["enter", "ctrl_c"], "expect": [">>>"]}], ctx
    )
    # two pyserial writes reached the wire, each under sudo -n
    writes = [a for a in _argvs(tp) if a and a[0] == "sudo" and "-c" in a]
    assert len(writes) == 2
    assert all(w[:3] == ["sudo", "-n", "python3"] for w in writes)
    assert writes[0][7] == "0d0a" and writes[1][7] == "03"  # enter then ctrl_c payloads
    # verdict + transcript + asset
    assert any(
        "SERIAL_PROBE_VERDICT classification=circuitpython_repl" in m for m in emitted
    )
    entry = next(e for e in ctx.transcript if e["cmd"].startswith("[serial_probe"))
    assert entry["classification"] == "circuitpython_repl" and entry["bytes"] > 0
    assert "classification=circuitpython_repl" in (tmp_path / "serial-probe.log").read_text()


async def test_serial_probe_silent_and_expect_failure(tmp_path: Path) -> None:
    tp, ctx, emitted = _probe_ctx(tmp_path, "")
    with pytest.raises(StageError, match="none of the expected patterns"):
        await run_stages([{"type": "serial_probe", "expect": [">>>"]}], ctx)
    assert any("classification=silent" in m for m in emitted)


async def test_serial_probe_pauses_capture_around_write(tmp_path: Path) -> None:
    calls: list[str] = []

    async def _pause() -> None:
        calls.append("pause")

    async def _resume() -> None:
        calls.append("resume")

    tp, ctx, _ = _probe_ctx(tmp_path, "x")
    ctx.pause_serial = _pause
    ctx.resume_serial = _resume
    await run_stages([{"type": "serial_probe", "pokes": ["enter"]}], ctx)
    assert calls == ["pause", "resume"]


async def test_serial_probe_requires_port() -> None:
    ctx = _ctx(_transport())
    ctx.flash_serial_port = ""
    ctx.log_serial_port = ""
    with pytest.raises(StageError, match="no serial port configured"):
        await run_stages([{"type": "serial_probe"}], ctx)


# --------------------------------------------------------------------------- #
# fail fast on an absent serial node (one test -e, no retry spin)             #
# --------------------------------------------------------------------------- #


def _absent_port_transport() -> AsyncMock:
    """Transport where the DUT's serial node does not exist (`test -e` fails)."""

    async def _exec(argv, **kw):
        if argv and argv[0] == "test":
            return _result(1)
        return _result(0, stdout="Chip is ESP32-S3")

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    return tp


async def test_serial_probe_fails_fast_when_port_absent() -> None:
    tp = _absent_port_transport()
    ctx = _ctx(tp, device={"id": "dut-1", "host_id": "rpi-hil006"})
    with pytest.raises(StageError, match="not present on host rpi-hil006"):
        await run_stages([{"type": "serial_probe"}], ctx)
    # exactly one existence check, and no pyserial write was attempted
    assert [a[0] for a in _argvs(tp)] == ["test"]


async def test_esptool_rescue_fails_fast_when_port_absent() -> None:
    tp = _absent_port_transport()
    ctx = _ctx(tp, device={"id": "dut-1", "host_id": "rpi-hil006", "solenoid_channel": 4})
    with pytest.raises(StageError, match="serial port .* not present on host rpi-hil006"):
        await run_stages([{"type": "esptool_rescue", "action": "chip_id"}], ctx)
    # no esptool invocation ever reached the wire (no retry spin)
    assert all("flash_id" not in a and "chip_id" not in a for a in _argvs(tp))


async def test_esptool_rescue_fails_fast_on_empty_port() -> None:
    tp = _transport()
    ctx = _ctx(tp, device={"id": "dut-1"})
    ctx.flash_serial_port = ""
    with pytest.raises(StageError, match="no serial port configured"):
        await run_stages([{"type": "esptool_rescue", "action": "chip_id"}], ctx)
    assert tp.exec.await_count == 0


async def test_picotool_rescue_fails_fast_when_port_absent() -> None:
    tp = _absent_port_transport()
    ctx = _ctx(tp, device={"id": "dut-1", "host_id": "rpi-hil006"})
    with pytest.raises(StageError, match="not present on host rpi-hil006"):
        await run_stages([{"type": "picotool_rescue", "action": "info"}], ctx)
    assert all(a[0] != "picotool" for a in _argvs(tp))


async def test_picotool_rescue_require_port_false_skips_check() -> None:
    # A board already sitting in BOOTSEL has no CDC node — require_port: false
    # lets picotool talk to it over raw USB anyway.
    async def _exec(argv, **kw):
        if argv and argv[0] == "test":
            return _result(1)  # node absent
        return _result(0, stdout=_PICO_INFO)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    ctx = _ctx(tp, device={"id": "dut-1"})
    await run_stages(
        [{"type": "picotool_rescue", "action": "info", "require_port": False}], ctx
    )
    assert any(a[0] == "picotool" for a in _argvs(tp))


def test_rescue_probe_preset_validates() -> None:
    validate_stages(RESCUE_PROBE_STAGES)  # no raise


# --------------------------------------------------------------------------- #
# enter_bootsel stage + shared solenoid choreography                          #
# --------------------------------------------------------------------------- #


async def test_enter_bootsel_hold_cycle_release_order() -> None:
    tp = _transport()
    ctx = _ctx(tp, device={"solenoid_channel": 4, "bootsel_channel": 12})
    await run_stages([{"type": "enter_bootsel"}], ctx)
    argvs = [a for a in _argvs(tp) if "solenoid_hub_cli.py" in a[1]]
    verbs = [(a[2], a[3]) for a in argvs]
    assert verbs[0] == ("port_on", "12")  # hold BOOTSEL first
    assert ("port_on", "4") in verbs  # power back on mid-cycle
    # release (port_off 12) comes after the power-cycle's port_on 4
    assert verbs.index(("port_on", "4")) < verbs.index(("port_off", "12"))


async def test_enter_bootsel_inverted_polarity() -> None:
    tp = _transport()
    ctx = _ctx(
        tp, device={"solenoid_channel": 4, "bootsel_channel": 12, "bootsel_inverted": 1}
    )
    await run_stages([{"type": "enter_bootsel"}], ctx)
    argvs = [a for a in _argvs(tp) if "solenoid_hub_cli.py" in a[1]]
    assert (argvs[0][2], argvs[0][3]) == ("port_off", "12")  # inverted: hold == OFF
    assert any(a[2] == "port_on" and a[3] == "12" for a in argvs)  # release == ON


async def test_enter_bootsel_requires_bootsel_channel() -> None:
    ctx = _ctx(_transport(), device={"solenoid_channel": 4})
    with pytest.raises(StageError, match="no bootsel_channel mapping"):
        await run_stages([{"type": "enter_bootsel"}], ctx)


async def test_bootsel_entry_releases_on_power_cycle_failure() -> None:
    hub = SolenoidHubAdapter(transport=_transport())
    hub.power_cycle = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    released: list[int] = []

    async def _off(ch: int, **kw) -> None:
        released.append(ch)

    hub.port_off = _off  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await hub.bootsel_entry(power_channel=4, bootsel_channel=12)
    assert released == [12]  # BOOTSEL press released despite the failure


# --------------------------------------------------------------------------- #
# picotool flasher + picotool_rescue stage                                    #
# --------------------------------------------------------------------------- #

_PICO_INFO = (
    "Program Information\n"
    " name:          blink\n"
    "Device Information\n"
    " type:                 RP2040\n"
    " flash size:           2048K\n"
)
_NO_BOOTSEL = "No accessible RP-series devices in BOOTSEL mode were found."


def test_needs_bootsel_detection() -> None:
    assert needs_bootsel(_NO_BOOTSEL)
    assert needs_bootsel("No accessible RP2040 devices in BOOTSEL mode were found.")
    assert not needs_bootsel("ERROR: The device was not rebooted")
    assert not needs_bootsel("")


def test_parse_picotool_info() -> None:
    parsed = parse_picotool_info(_PICO_INFO)
    assert parsed == {"family": "RP2040", "flash_k": "2048", "program": "blink"}


async def test_picotool_flasher_argvs() -> None:
    tp = _transport(_result(0, stdout=_PICO_INFO))
    fl = PicotoolFlasher(transport=tp, port="/dev/ttyACM0")
    await fl.info()
    await fl.reboot_bootsel()
    await fl.reboot_app()
    await fl.erase()
    await fl.flash(Artifact(path="/tmp/x.uf2", kind="uf2"))
    argvs = _argvs(tp)
    assert argvs[0] == ["picotool", "info", "-a"]
    assert argvs[1] == ["picotool", "reboot", "-f", "-u"]
    assert argvs[2] == ["picotool", "reboot"]
    assert argvs[3] == ["picotool", "erase"]
    assert argvs[4] == ["picotool", "load", "-v", "-x", "/tmp/x.uf2"]


async def test_picotool_probe_parses_chipinfo() -> None:
    tp = _transport(_result(0, stdout=_PICO_INFO))
    fl = PicotoolFlasher(transport=tp, port="/dev/ttyACM0")
    info = await fl.probe()
    assert info.family == "RP2040"
    assert info.flash_bytes == 2048 * 1024


async def test_picotool_rescue_info_records_verdict() -> None:
    tp = _transport(_result(0, stdout=_PICO_INFO))
    emitted: list[str] = []
    ctx = _ctx(tp, device={"solenoid_channel": 4}, emit=emitted.append)
    await run_stages([{"type": "picotool_rescue", "action": "info"}], ctx)
    assert any(
        "PICOTOOL_VERDICT action=info ok=true family=RP2040" in m for m in emitted
    )
    entry = next(e for e in ctx.transcript if e["cmd"] == "[picotool_rescue info]")
    assert entry["picotool"]["family"] == "RP2040"


async def test_picotool_rescue_load_bootsel_fallback_retries_once() -> None:
    loads = 0

    async def _exec(argv, **kw):
        nonlocal loads
        if argv and argv[0] == "picotool":
            loads += 1
            if loads == 1:  # app mode, nothing in BOOTSEL
                return _result(1, stderr=_NO_BOOTSEL)
            return _result(0, stdout="Loading into Flash: [==] 100%")
        return _result(0)  # solenoid CLI

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    emitted: list[str] = []
    ctx = _ctx(
        tp, device={"solenoid_channel": 4, "bootsel_channel": 12}, emit=emitted.append
    )
    await run_stages([{"type": "picotool_rescue", "action": "load"}], ctx)
    assert loads == 2  # failed once, BOOTSEL entry, retried once
    solenoid = [a for a in _argvs(tp) if "solenoid_hub_cli.py" in a[1]]
    assert (solenoid[0][2], solenoid[0][3]) == ("port_on", "12")  # BOOTSEL held
    assert any("PICOTOOL_VERDICT action=load ok=true" in m for m in emitted)


def _no_bootsel_transport() -> AsyncMock:
    """picotool always answers 'nothing in BOOTSEL'; everything else succeeds."""

    async def _exec(argv, **kw):
        if argv and argv[0] == "picotool":
            return _result(1, stderr=_NO_BOOTSEL)
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    return tp


async def test_picotool_rescue_no_fallback_without_bootsel_channel() -> None:
    tp = _no_bootsel_transport()
    ctx = _ctx(tp, device={"solenoid_channel": 4})  # no bootsel_channel
    with pytest.raises(StageError, match="picotool_rescue load failed"):
        await run_stages([{"type": "picotool_rescue", "action": "load"}], ctx)
    assert all("solenoid_hub_cli.py" not in a[1] for a in _argvs(tp) if len(a) > 1)


async def test_picotool_rescue_fallback_disabled_by_opt() -> None:
    tp = _no_bootsel_transport()
    ctx = _ctx(tp, device={"solenoid_channel": 4, "bootsel_channel": 12})
    with pytest.raises(StageError, match="picotool_rescue load failed"):
        await run_stages(
            [{"type": "picotool_rescue", "action": "load", "bootsel_fallback": False}], ctx
        )


async def test_picotool_rescue_rejects_unknown_action() -> None:
    ctx = _ctx(_transport())
    with pytest.raises(StageError, match="unknown action"):
        await run_stages([{"type": "picotool_rescue", "action": "nuke"}], ctx)


def test_make_flasher_knows_picotool() -> None:
    ctx = _ctx(_transport())
    assert isinstance(ctx.make_flasher("picotool"), PicotoolFlasher)


# --------------------------------------------------------------------------- #
# esptool_rescue stage                                                        #
# --------------------------------------------------------------------------- #

_CHIP_OUT = "Chip is ESP32-S3 (revision v0.2)\nMAC: f4:12:fa:5a:35:b4\n"

# A realistic full esptool chip_id transcript from the bench ESP32-S3 —
# including the "has no Chip ID" warning that round-2 validation showed the
# old parser scraping (verdict read chip=not). The chip must come from the
# "Chip is <NAME>" / "Detecting chip type..." lines, never the warning.
_CHIP_ID_TRANSCRIPT = (
    "esptool.py v4.8.1\n"
    "Serial port /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit-if00\n"
    "Connecting...\n"
    "Detecting chip type... ESP32-S3\n"
    "Chip is ESP32-S3 (QFN56) (revision v0.1)\n"
    "Features: WiFi, BLE, Embedded Flash 4MB (XMC)\n"
    "Crystal is 40MHz\n"
    "MAC: f4:12:fa:5a:35:b4\n"
    "Uploading stub...\n"
    "Running stub...\n"
    "Stub running...\n"
    "Warning: ESP32-S3 has no Chip ID. Reading MAC instead.\n"
    "MAC: f4:12:fa:5a:35:b4\n"
    "Staying in bootloader.\n"
)


def test_parse_chip_family_ignores_no_chip_id_warning() -> None:
    from hil_controller.adapters.flashers.esptool import parse_chip_family

    assert parse_chip_family(_CHIP_ID_TRANSCRIPT) == "ESP32-S3"
    # the warning alone (or "Chip is not ..." phrasing) must never match
    assert parse_chip_family("Warning: ESP32-S3 has no Chip ID. Reading MAC instead.") is None
    assert parse_chip_family("Chip is not in secure download mode") is None
    # newer esptool phrasings still parse
    assert parse_chip_family("Chip type: ESP32-S3 (QFN56) (revision v0.1)") == "ESP32-S3"
    assert parse_chip_family("Detecting chip type... ESP32-C5") == "ESP32-C5"


async def test_esptool_rescue_chip_id_verdict_from_realistic_transcript() -> None:
    tp = AsyncMock()
    tp.exec = AsyncMock(return_value=_result(0, stdout=_CHIP_ID_TRANSCRIPT))
    emitted: list[str] = []
    ctx = _ctx(tp, device={"solenoid_channel": 4}, emit=emitted.append)
    await run_stages([{"type": "esptool_rescue", "action": "chip_id"}], ctx)
    verdict = next(m for m in emitted if "ESPTOOL_RESCUE_VERDICT action=chip_id" in m)
    assert "chip=ESP32-S3" in verdict  # NOT chip=not (the old warning scrape)
    assert "mac=f4:12:fa:5a:35:b4" in verdict


def _esp_transport() -> AsyncMock:
    """Every esptool invocation syncs (chip already in the download ROM)."""
    tp = AsyncMock()
    tp.exec = AsyncMock(return_value=_result(0, stdout=_CHIP_OUT))
    return tp


async def test_esptool_rescue_chip_id() -> None:
    tp = _esp_transport()
    emitted: list[str] = []
    ctx = _ctx(tp, device={"solenoid_channel": 4}, emit=emitted.append)
    await run_stages([{"type": "esptool_rescue", "action": "chip_id"}], ctx)
    cmd = next(a for a in _argvs(tp) if "chip_id" in a)
    assert "--before" in cmd and cmd[cmd.index("--before") + 1] == "no_reset"
    assert any(
        "ESPTOOL_RESCUE_VERDICT action=chip_id ok=true chip=ESP32-S3" in m for m in emitted
    )
    entry = next(e for e in ctx.transcript if e["cmd"] == "[esptool_rescue chip_id]")
    assert entry["esptool"]["mac"] == "f4:12:fa:5a:35:b4"


async def test_esptool_rescue_erase_and_read_mac() -> None:
    tp = _esp_transport()
    emitted: list[str] = []
    ctx = _ctx(tp, device={"solenoid_channel": 4}, emit=emitted.append)
    await run_stages(
        [
            {"type": "esptool_rescue", "action": "read_mac"},
            {"type": "esptool_rescue", "action": "erase_flash", "enter_download": False},
        ],
        ctx,
    )
    assert any("read_mac" in a for a in _argvs(tp))
    assert any("erase_flash" in a for a in _argvs(tp))
    assert any("ESPTOOL_RESCUE_VERDICT action=erase_flash ok=true" in m for m in emitted)


async def test_esptool_rescue_write_uses_address() -> None:
    tp = _esp_transport()
    ctx = _ctx(tp, device={"solenoid_channel": 4})
    await run_stages(
        [{"type": "esptool_rescue", "action": "write", "address": "0x10000"}], ctx
    )
    cmd = next(a for a in _argvs(tp) if "write_flash" in a)
    assert cmd[cmd.index("write_flash") + 1] == "0x10000"


async def test_esptool_rescue_rejects_unknown_action() -> None:
    ctx = _ctx(_transport())
    with pytest.raises(StageError, match="unknown action"):
        await run_stages([{"type": "esptool_rescue", "action": "detonate"}], ctx)


# --------------------------------------------------------------------------- #
# bare param (rescue job ergonomics)                                          #
# --------------------------------------------------------------------------- #


def _bench_adapter(params: dict) -> object:
    from hil_controller.adapters.firmware_bench import FirmwareBenchAdapter

    return FirmwareBenchAdapter(
        controller_transport=MagicMock(),
        dut_transport=MagicMock(),
        hub_transport=MagicMock(),
        job_id="job-1",
        device={"id": "dut-1"},
        params=params,
        secrets={},
    )


def test_bare_param_suppresses_launch_protomq() -> None:
    stages = [{"type": "write_secrets_msc"}, {"type": "power_cycle"}]
    bare = _bench_adapter({"stages": stages, "bare": True})._build_stages(log_port="/dev/x")
    assert all(s["type"] != "launch_protomq" for s in bare)
    # start_serial_log / print_boot_log auto-inserts survive bare mode
    assert any(s["type"] == "start_serial_log" for s in bare)
    assert any(s["type"] == "print_boot_log" for s in bare)


def test_without_bare_launch_protomq_still_inserted() -> None:
    stages = [{"type": "write_secrets_msc"}, {"type": "power_cycle"}]
    built = _bench_adapter({"stages": stages})._build_stages(log_port="/dev/x")
    assert any(s["type"] == "launch_protomq" for s in built)
