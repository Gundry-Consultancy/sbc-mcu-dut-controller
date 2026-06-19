"""SerialCaptureAdapter — stream a DUT's serial port into job log events.

Opens a streaming channel against the host's stable
``/dev/serial/by-id/...`` (never ``ttyACM*`` numbering — that name jumps
across re-enumeration). On hosts using usbip, the channel opens on the
*attached* side (the controller), with the path resolved at start time
from :meth:`UsbipBridge.list_serial_ports`.

Two phases:

* **boot capture** — from :meth:`acquire`/:meth:`start` until either a
  caller-supplied ``boot_marker`` is seen or a quiet window elapses.
* **run capture** — concurrent with the run phase, terminated by
  :meth:`stop`/:meth:`release`.

Each received line is tee'd to:

1. an optional ``on_line`` callback (e.g. emit a `JobEvent` log event), and
2. an optional ``artifact_path`` on disk (``serial-<phase>.log``).

Tests scripts can call :meth:`read_until` to assert on a token without
losing concurrent stream output (the buffered reader and the in-memory
line history are both consulted).

A shared :class:`asyncio.Lock` (``port_lock``) lets a Flasher steal the
port for the duration of a flash/erase call. The orchestrator calls
:meth:`pause` (releases the lock + stops the reader) before invoking
the flasher and :meth:`resume` after the flasher's ``finally`` returns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Pure helpers (top-level so they're unit-testable)                           #
# --------------------------------------------------------------------------- #


def split_lines(buffer: bytes) -> tuple[list[str], bytes]:
    """Split ``buffer`` on LF / CRLF; return complete lines + carry-over bytes.

    Lines are decoded with utf-8/replace so binary noise doesn't crash the
    pipeline. The carry-over is the trailing fragment that didn't end in
    a newline; the caller should prepend it to the next chunk.
    """
    if not buffer:
        return [], b""
    text = buffer.decode("utf-8", errors="replace")
    parts = text.split("\n")
    lines = [p.rstrip("\r") for p in parts[:-1]]
    remainder = parts[-1].encode("utf-8", errors="replace")
    return lines, remainder


_BOOT_MARKER_NEEDLES = (
    "rst:",
    "boot:",
    "configsip:",
    "load:",
    "entry ",
    "Boot ",
    "CIRCUITPY",
    "MicroPython",
)


def parse_boot_marker(line: str) -> bool:
    """Heuristic: does *line* look like a boot/banner emission worth keeping?

    Used to decide when the "boot capture" phase has gathered enough
    output to hand off to "run capture". Not a hard contract — callers
    can override the marker by passing an explicit ``boot_marker``
    substring to :meth:`SerialCaptureAdapter.start`.
    """
    return any(needle in line for needle in _BOOT_MARKER_NEEDLES)


def _socat_escape(path: str) -> str:
    """Escape characters socat treats as address separators within ``OPEN:``.

    socat splits an address on ``:`` and ``,``; a ``/dev/serial/by-path/...``
    name contains colons (``…usb-0:1.2:1.0``), so an unescaped path makes socat
    fail with ``OPEN: wrong number of parameters``. Backslash-escape ``\\``,
    ``:`` and ``,`` so the literal path reaches ``open()``.
    """
    return path.replace("\\", "\\\\").replace(":", "\\:").replace(",", "\\,")


def socat_argv(serial_path: str, baud: int) -> list[str]:
    """Build the argv that streams the serial port as raw bytes to stdout.

    Uses ``socat -u`` (unidirectional read) so it cleanly closes when we
    cancel the streaming task. ``b<baud>`` + ``raw`` give a binary-clean byte
    stream; ``clocal=1`` makes it a passive logger that ignores modem-control
    lines, so opening the port (and the 1s reconnect loop) doesn't lean on
    DTR/RTS and nudge a native-USB CDC app. The path is escaped so colon-bearing
    ``by-path`` names parse correctly (see :func:`_socat_escape`).
    """
    return ["socat", "-u", f"OPEN:{_socat_escape(serial_path)},b{baud},raw,clocal=1", "-"]


# --------------------------------------------------------------------------- #
# Adapter                                                                     #
# --------------------------------------------------------------------------- #


class SerialCaptureAdapter:
    """Long-running serial stream with pause/resume + read_until."""

    def __init__(
        self,
        *,
        transport: Any,
        serial_path: str,
        baud: int = 115200,
        artifact_path: Path | None = None,
        on_line: Callable[[str], None] | None = None,
        port_lock: asyncio.Lock | None = None,
        max_queue: int = 4096,
        reconnect_s: float = 1.0,
    ) -> None:
        self.transport = transport
        self.serial_path = serial_path
        self.baud = baud
        self.artifact_path = artifact_path
        self.on_line = on_line
        self.port_lock = port_lock or asyncio.Lock()
        self.reconnect_s = reconnect_s

        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue)
        self._all_lines: list[str] = []
        self._buffer = b""
        self._task: asyncio.Task | None = None
        self._running = False
        self._lock_held = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Acquire the port lock and spawn the background reader task."""
        if self._running:
            return
        await self.port_lock.acquire()
        self._lock_held = True
        self._running = True
        self._task = asyncio.create_task(self._reader(), name="serial-capture")

    async def stop(self) -> None:
        """Cancel the reader, flush trailing buffer, release the port lock."""
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                # Bound the wait: cancelling a socat-over-SSH reader can block in
                # asyncssh's channel teardown if the remote process doesn't die
                # promptly. Don't let that hang the caller's teardown.
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        # flush trailing buffer as a single line if it has any content
        if self._buffer:
            self._handle_line(self._buffer.decode("utf-8", errors="replace"))
            self._buffer = b""
        if self._lock_held:
            try:
                self.port_lock.release()
            except RuntimeError:  # already released
                pass
            self._lock_held = False

    async def pause(self) -> None:
        """Release the port for a Flasher operation; stops the reader."""
        await self.stop()

    async def resume(self) -> None:
        """Re-take the port after a Flasher operation; restarts the reader."""
        await self.start()

    async def __aenter__(self) -> SerialCaptureAdapter:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------ #
    # Reader internals                                                   #
    # ------------------------------------------------------------------ #

    async def _reader(self) -> None:
        """Stream the port, reconnecting while running.

        A single ``socat`` open dies if the device isn't enumerated yet (e.g.
        the seconds right after a power-cycle, before its USB CDC re-appears) or
        if it re-enumerates mid-session. Rather than give up on the first
        failure/EOF, retry the open every ``reconnect_s`` for as long as the
        capture is running — so logging survives reboots and boot delays.
        """
        argv = socat_argv(self.serial_path, self.baud)
        while self._running:
            try:
                async for chunk in self.transport.stream(argv):
                    if chunk is None:
                        continue
                    self._buffer += chunk
                    lines, self._buffer = split_lines(self._buffer)
                    for line in lines:
                        self._handle_line(line)
                # stream ended cleanly (EOF) — port likely went away; retry.
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("serial capture stream attempt failed (will retry): %s", exc)
            if not self._running:
                break
            await asyncio.sleep(self.reconnect_s)

    def _handle_line(self, line: str) -> None:
        self._all_lines.append(line)
        if self.artifact_path is not None:
            try:
                # Timestamp the on-disk record (UTC, ms) so serial.log lines up
                # with flash.log/protomq.log; the in-memory line stays raw so
                # token matching / boot-marker / reboot-banner scans are unaffected.
                ts = datetime.now(UTC).isoformat(timespec="milliseconds")
                with self.artifact_path.open("a", encoding="utf-8") as f:
                    f.write(f"{ts}  {line}\n")
            except OSError as exc:
                log.warning("serial capture artifact write failed: %s", exc)
        if self.on_line is not None:
            try:
                self.on_line(line)
            except Exception as exc:  # noqa: BLE001
                log.warning("serial capture on_line callback raised: %s", exc)
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            # Drop oldest queued line to make room — read_until callers
            # are best-effort, the durable record lives in self._all_lines.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(line)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    # ------------------------------------------------------------------ #
    # Public consumer API                                                #
    # ------------------------------------------------------------------ #

    @property
    def lines(self) -> list[str]:
        """All lines captured so far (snapshot copy)."""
        return list(self._all_lines)

    async def read_until(self, token: str, *, timeout: float = 30.0) -> str:
        """Block until a line containing *token* arrives; return that line.

        Already-received lines are checked first so a caller racing the
        producer doesn't miss the match. Raises :class:`TimeoutError` if
        *token* doesn't appear within ``timeout`` seconds.
        """
        # Drain history first.
        for line in self._all_lines:
            if token in line:
                return line
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"serial: token {token!r} not seen within {timeout}s")
            try:
                line = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except TimeoutError as exc:
                raise TimeoutError(f"serial: token {token!r} not seen within {timeout}s") from exc
            if token in line:
                return line
