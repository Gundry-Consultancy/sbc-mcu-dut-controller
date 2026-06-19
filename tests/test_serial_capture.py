"""SerialCaptureAdapter tests (M3.5).

Pure helpers (split_lines / parse_boot_marker / socat_argv) are
unit-tested without any I/O. Adapter behavior uses a fake transport
whose ``stream()`` is an async generator that yields predetermined
byte chunks.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from hil_controller.adapters.serial_capture import (
    SerialCaptureAdapter,
    parse_boot_marker,
    socat_argv,
    split_lines,
)

# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


def test_split_lines_basic_lf() -> None:
    lines, rem = split_lines(b"hello\nworld\n")
    assert lines == ["hello", "world"]
    assert rem == b""


def test_split_lines_crlf() -> None:
    lines, rem = split_lines(b"first\r\nsecond\r\n")
    assert lines == ["first", "second"]
    assert rem == b""


def test_split_lines_keeps_partial_remainder() -> None:
    lines, rem = split_lines(b"complete\npartial")
    assert lines == ["complete"]
    assert rem == b"partial"


def test_split_lines_empty_input() -> None:
    assert split_lines(b"") == ([], b"")


def test_split_lines_replaces_bad_utf8() -> None:
    # \xff is not valid UTF-8; should not raise.
    lines, rem = split_lines(b"ok\xff\nnext\n")
    assert len(lines) == 2
    assert "next" == lines[1]


def test_parse_boot_marker_matches_esp_boot() -> None:
    assert parse_boot_marker("rst:0x1 (POWERON_RESET),boot:0x8")
    assert parse_boot_marker("entry 0x40000000")
    assert parse_boot_marker("load:0x40080000,len:1234")


def test_parse_boot_marker_matches_circuitpython() -> None:
    assert parse_boot_marker("CIRCUITPY mounted")


def test_parse_boot_marker_no_match() -> None:
    assert not parse_boot_marker("some random log line")
    assert not parse_boot_marker("")


def test_socat_argv() -> None:
    assert socat_argv("/dev/serial/by-id/usb-Adafruit_xxx", 115200) == [
        "socat",
        "-u",
        "OPEN:/dev/serial/by-id/usb-Adafruit_xxx,b115200,raw,clocal=1",
        "-",
    ]


def test_socat_argv_escapes_colon_bearing_by_path() -> None:
    # by-path names contain colons, which socat would otherwise treat as
    # address separators ("wrong number of parameters").
    argv = socat_argv("/dev/serial/by-path/platform-3f980000.usb-usb-0:1.2:1.0", 115200)
    assert argv == [
        "socat",
        "-u",
        "OPEN:/dev/serial/by-path/platform-3f980000.usb-usb-0\\:1.2\\:1.0,b115200,raw,clocal=1",
        "-",
    ]


# --------------------------------------------------------------------------- #
# Fake transport that yields predetermined chunks                              #
# --------------------------------------------------------------------------- #


class _FakeStreamingTransport:
    """Yields ``chunks`` from ``stream()``, exposes the recorded argv list."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.stream_argvs: list[list[str]] = []
        self._block_event = asyncio.Event()  # set this to release any block

    async def stream(self, argv: list[str]) -> AsyncIterator[bytes]:
        self.stream_argvs.append(list(argv))
        for chunk in self.chunks:
            yield chunk
            # tiny await so other tasks can run; lets read_until race the producer
            await asyncio.sleep(0)
        # After chunks exhaust, block forever so the reader task only ends
        # when its outer Task is cancelled.
        await self._block_event.wait()


class _ReconnectTransport:
    """First stream() ends immediately with no data (device not ready yet);
    later attempts yield a line then block — models post-power-cycle boot."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, argv: list[str]) -> AsyncIterator[bytes]:
        self.calls += 1
        if self.calls == 1:
            return  # empty async generator — simulates port not present yet
            yield b""  # unreachable; present to make this an async generator
        yield b"hello after reconnect\n"
        await asyncio.sleep(0)
        await asyncio.Event().wait()  # block so we don't reconnect-spam


# --------------------------------------------------------------------------- #
# Adapter                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reader_reconnects_after_stream_ends() -> None:
    tp = _ReconnectTransport()
    cap = SerialCaptureAdapter(
        transport=tp,
        serial_path="/dev/x",
        reconnect_s=0.01,
        on_line=None,
    )
    await cap.start()
    await asyncio.sleep(0.1)  # allow the first (empty) attempt + a reconnect
    await cap.stop()
    assert tp.calls >= 2
    assert any("hello after reconnect" in ln for ln in cap.lines)


@pytest.mark.asyncio
async def test_start_launches_reader_with_socat_argv(tmp_path: Path) -> None:
    tp = _FakeStreamingTransport([b"boot ok\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/serial/by-id/usb-X")
    await cap.start()
    await asyncio.sleep(0.01)  # let the reader pick up the chunk
    await cap.stop()
    assert tp.stream_argvs == [
        ["socat", "-u", "OPEN:/dev/serial/by-id/usb-X,b115200,raw,clocal=1", "-"]
    ]


@pytest.mark.asyncio
async def test_lines_collects_stream_output_into_history() -> None:
    tp = _FakeStreamingTransport([b"line one\nline two\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x")
    async with cap:
        await asyncio.sleep(0.05)
    assert "line one" in cap.lines
    assert "line two" in cap.lines


@pytest.mark.asyncio
async def test_partial_chunk_then_completion_yields_one_line() -> None:
    tp = _FakeStreamingTransport([b"partial-", b"complete\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x")
    async with cap:
        await asyncio.sleep(0.05)
    assert cap.lines == ["partial-complete"]


@pytest.mark.asyncio
async def test_artifact_path_records_each_line(tmp_path: Path) -> None:
    artifact = tmp_path / "serial-boot.log"
    tp = _FakeStreamingTransport([b"alpha\nbeta\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x", artifact_path=artifact)
    async with cap:
        await asyncio.sleep(0.05)
    # Each artifact line is timestamped (UTC ISO, ms) then the raw line; the
    # in-memory/on_line path stays raw (asserted elsewhere).
    lines = artifact.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("  alpha") and "T" in lines[0]
    assert lines[1].endswith("  beta")


@pytest.mark.asyncio
async def test_on_line_callback_invoked_per_line() -> None:
    received: list[str] = []
    tp = _FakeStreamingTransport([b"a\nb\nc\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x", on_line=received.append)
    async with cap:
        await asyncio.sleep(0.05)
    assert received == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_on_line_callback_exception_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    def boom(line: str) -> None:
        raise RuntimeError("callback bug")

    tp = _FakeStreamingTransport([b"keep going\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x", on_line=boom)
    async with cap:
        await asyncio.sleep(0.05)
    assert "keep going" in cap.lines  # capture survives a bad callback


@pytest.mark.asyncio
async def test_read_until_matches_already_received_line() -> None:
    tp = _FakeStreamingTransport([b"boot\nPASS\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x")
    async with cap:
        await asyncio.sleep(0.05)
        line = await cap.read_until("PASS", timeout=1.0)
    assert line == "PASS"


@pytest.mark.asyncio
async def test_read_until_waits_for_future_line() -> None:
    tp = _FakeStreamingTransport([b"warming up\n", b"AOK signal\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x")
    async with cap:
        line = await cap.read_until("AOK", timeout=1.0)
    assert "AOK" in line


@pytest.mark.asyncio
async def test_read_until_times_out_when_token_never_arrives() -> None:
    tp = _FakeStreamingTransport([b"first\nsecond\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x")
    async with cap:
        with pytest.raises(TimeoutError, match="not seen"):
            await cap.read_until("never-arrives", timeout=0.1)


@pytest.mark.asyncio
async def test_port_lock_blocks_other_acquirers_while_running() -> None:
    shared = asyncio.Lock()
    tp = _FakeStreamingTransport([b"running\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x", port_lock=shared)
    await cap.start()
    try:
        # Another would-be acquirer should NOT be able to take the lock.
        assert shared.locked()
    finally:
        await cap.stop()
    # After stop, the lock is released.
    assert not shared.locked()


@pytest.mark.asyncio
async def test_pause_releases_lock_then_resume_takes_it_again() -> None:
    shared = asyncio.Lock()
    tp = _FakeStreamingTransport([b"line\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x", port_lock=shared)
    await cap.start()
    assert shared.locked()
    await cap.pause()
    assert not shared.locked()
    # While paused, a flasher could acquire the same lock:
    await shared.acquire()
    shared.release()
    await cap.resume()
    assert shared.locked()
    await cap.stop()
    assert not shared.locked()


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    tp = _FakeStreamingTransport([b"x\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x")
    await cap.start()
    await cap.stop()
    await cap.stop()  # second stop is a no-op, no exception


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    tp = _FakeStreamingTransport([b"x\n"])
    cap = SerialCaptureAdapter(transport=tp, serial_path="/dev/x")
    await cap.start()
    await cap.start()  # second start is a no-op (already running)
    await cap.stop()
