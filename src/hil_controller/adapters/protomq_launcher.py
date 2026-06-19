"""Launch a per-session protomq broker on the controller — without editing it.

protomq is cloned + built fresh per session (like the arduino-ws setup), then
started with its own ``npm start``. We never modify protomq: the launcher reads
the ``... listening on port N`` lines protomq already prints on boot to learn
the MQTT/API/WS ports it actually bound, so whatever a given protomq build does
about port selection is honoured automatically.

protomq runs on the controller (the LAN address the freshly-flashed DUT reaches
the broker at), so the long-running ``npm start`` is spawned as a local
subprocess the launcher owns and can reliably ``terminate()`` on teardown —
avoiding the orphaned-node-on-port-1884 problem noted in the tachyon runbook.
Clone + build go through the controller transport's ``exec``.

Log forwarding and script activation (the pytest autoresponder/echo setup) reuse
:class:`ProtoMQObserver`; this module only owns the process + port discovery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import PurePosixPath
from typing import Any, Optional

log = logging.getLogger(__name__)


class ProtomqLaunchError(RuntimeError):
    """Clone / build / start failed, or ports never appeared."""


# protomq boot lines, e.g.
#   "MQTT listening on port 1884"
#   "HTTP frontend & API listening on port 5173"
#   "MQTT-via-WebSocket listening on port 8888"
_PORT_RES: dict[str, re.Pattern[str]] = {
    "mqtt": re.compile(r"\bMQTT listening on port\b[^\d]*(\d+)", re.IGNORECASE),
    "api": re.compile(r"\bAPI listening on port\b[^\d]*(\d+)", re.IGNORECASE),
    "ws": re.compile(r"\bWebSocket listening on port\b[^\d]*(\d+)", re.IGNORECASE),
}


def parse_listen_ports(text: str) -> dict[str, int]:
    """Extract any ``{mqtt,api,ws}`` ports announced in *text*.

    Tolerant of the exact wording ("HTTP frontend & API listening on port",
    "MQTT-via-WebSocket listening on port") — keyed on the distinctive token in
    each line. The MQTT pattern deliberately requires "MQTT listening" so the
    "MQTT-via-WebSocket listening" line is matched only as ``ws``.
    """
    out: dict[str, int] = {}
    for key, rx in _PORT_RES.items():
        m = rx.search(text or "")
        if m:
            out[key] = int(m.group(1))
    return out


def clone_argv(
    repo: str,
    ref: str,
    work_dir: str,
    *,
    pat: str | None = None,
    credential_helper: str | None = None,
) -> list[str]:
    """``git clone --depth 1 --branch <ref> <repo> <work_dir>`` with optional auth.

    Auth precedence mirrors git-source jobs: a per-repo ``pat`` embedded in the
    https URL takes priority; otherwise a ``credential.helper`` (e.g. the bench's
    ``!sudo gh auth git-credential``) is used. Public repos need neither.
    """
    url = repo
    if pat and url.startswith("https://"):
        url = url.replace("https://", f"https://{pat}@", 1)
    argv = ["git"]
    if not pat and credential_helper:
        argv += ["-c", f"credential.helper={credential_helper}"]
    argv += ["clone", "--depth", "1", "--branch", ref, url, work_dir]
    return argv


#: protobuf source repo + ref protomq's import-protos pulls the V2 protos from,
#: cloned as the sibling ``../Wippersnapper_Protobuf`` that ``.env.example.json``
#: points at. V1 protos are already bundled in protomq's displays-v2-testing.
PROTOBUF_REPO_DEFAULT = "https://github.com/adafruit/Wippersnapper_Protobuf.git"
PROTOBUF_REF_DEFAULT = "api-v2"
PROTOBUF_DIR_NAME = "Wippersnapper_Protobuf"


#: Build steps run (chained) inside the clone dir. Mirrors the known-good
#: arduino-ws setup: install deps, import protobufs, build the web bundle that
#: protomq's main.js requires before it will start.
BUILD_COMMAND = (
    "cp -f .env.example.json .env.json && npm ci && npm run import-protos && npm run build-web"
)


def start_argv(active_script: str | None = None) -> list[str]:
    """``npm start`` (optionally passing protomq's ``--active-script``)."""
    argv = ["npm", "start"]
    if active_script:
        argv += ["--", f"--active-script={active_script}"]
    return argv


LineSink = Callable[[str], Awaitable[None]] | Callable[[str], None]


class ProtomqLauncher:
    """Clone + build + start one protomq instance; discover its bound ports."""

    def __init__(
        self,
        *,
        controller_transport: Any,
        repo: str,
        ref: str,
        work_dir: PurePosixPath | str,
        active_script: str | None = None,
        on_line: LineSink | None = None,
        pat: str | None = None,
        credential_helper: str | None = None,
        proto_repo: str = PROTOBUF_REPO_DEFAULT,
        proto_ref: str = PROTOBUF_REF_DEFAULT,
    ) -> None:
        self.controller_transport = controller_transport
        self.repo = repo
        self.ref = ref
        self.work_dir = str(work_dir)
        self.active_script = active_script
        self.on_line = on_line
        self.pat = pat
        self.credential_helper = credential_helper
        self.proto_repo = proto_repo
        self.proto_ref = proto_ref

        self.mqtt_port: int | None = None
        self.api_port: int | None = None
        self.ws_port: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._drain_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # clone + build                                                       #
    # ------------------------------------------------------------------ #

    def _proto_dir(self) -> str:
        """Sibling path ``../Wippersnapper_Protobuf`` relative to the protomq dir."""
        return str(PurePosixPath(self.work_dir).parent / PROTOBUF_DIR_NAME)

    async def _clone(self, repo: str, ref: str, dest: str, what: str) -> None:
        argv = clone_argv(repo, ref, dest, pat=self.pat, credential_helper=self.credential_helper)
        res = await self.controller_transport.exec(argv)
        if getattr(res, "exit_status", 0) != 0:
            raise ProtomqLaunchError(
                f"{what} clone failed (exit {res.exit_status}): {(res.stderr or '').strip()[:300]}"
            )

    async def clone_and_build(self) -> None:
        """Clone protomq + its sibling protobuf source, then build the bundle.

        protomq's import-protos reads ``../Wippersnapper_Protobuf/proto`` (V2);
        V1 protos are already bundled in the protomq branch.
        """
        await self._clone(self.repo, self.ref, self.work_dir, "protomq")
        await self._clone(
            self.proto_repo, self.proto_ref, self._proto_dir(), "Wippersnapper_Protobuf"
        )
        build = await self.controller_transport.exec(
            ["bash", "-c", BUILD_COMMAND], cwd=self.work_dir
        )
        if getattr(build, "exit_status", 0) != 0:
            raise ProtomqLaunchError(
                f"protomq build failed (exit {build.exit_status}): {(build.stderr or '').strip()[:300]}"  # noqa: E501
            )

    # ------------------------------------------------------------------ #
    # port discovery from a line stream                                   #
    # ------------------------------------------------------------------ #

    async def _consume_until_ready(
        self, lines: AsyncIterator[str], *, ready_timeout: float
    ) -> None:
        """Read boot lines, recording ports, until MQTT+API are known.

        Each line is also forwarded to ``on_line``. Raises
        :class:`ProtomqLaunchError` if the broker doesn't announce its MQTT and
        API ports within ``ready_timeout`` seconds.
        """

        async def _pump() -> None:
            async for raw in lines:
                line = raw.rstrip("\n")
                await self._forward(line)
                found = parse_listen_ports(line)
                self.mqtt_port = found.get("mqtt", self.mqtt_port)
                self.api_port = found.get("api", self.api_port)
                self.ws_port = found.get("ws", self.ws_port)
                if self.mqtt_port and self.api_port:
                    return

        try:
            await asyncio.wait_for(_pump(), timeout=ready_timeout)
        except TimeoutError as exc:
            raise ProtomqLaunchError(
                f"protomq did not announce MQTT+API ports within {ready_timeout}s "
                f"(mqtt={self.mqtt_port}, api={self.api_port})"
            ) from exc

    async def _forward(self, line: str) -> None:
        if self.on_line is None:
            return
        try:
            res = self.on_line(line)
            if asyncio.iscoroutine(res):
                await res
        except Exception:  # noqa: BLE001 — a log sink must never break the launcher
            log.warning("protomq on_line sink raised", exc_info=True)

    # ------------------------------------------------------------------ #
    # start / stop                                                        #
    # ------------------------------------------------------------------ #

    async def start(self, *, ready_timeout: float = 90.0) -> None:
        """Spawn ``npm start`` locally, block until ports are known, keep draining.

        Assumes protomq runs on the controller (a local subprocess) so teardown
        can kill it deterministically.
        """
        self._proc = await asyncio.create_subprocess_exec(
            *start_argv(self.active_script),
            cwd=self.work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # New session → own process group, so teardown can signal the whole
            # tree. ``npm start`` forks ``sh -c node main.js`` → ``node main.js``;
            # terminating only npm orphans node on the MQTT port (the bug this
            # avoids — see the tachyon orphaned-node note in the module docstring).
            start_new_session=True,
        )
        assert self._proc.stdout is not None

        async def _line_iter() -> AsyncIterator[str]:
            assert self._proc is not None and self._proc.stdout is not None
            async for b in self._proc.stdout:
                yield b.decode("utf-8", errors="replace")

        gen = _line_iter()
        await self._consume_until_ready(gen, ready_timeout=ready_timeout)
        # Keep draining stdout for the rest of the session so logs keep flowing
        # and the pipe never fills (which would stall node).
        self._drain_task = asyncio.create_task(self._drain(gen), name="protomq-drain")

    async def _drain(self, gen: AsyncIterator[str]) -> None:
        try:
            async for line in gen:
                await self._forward(line.rstrip("\n"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("protomq drain ended: %s", exc)

    def _signal_tree(self, sig: int) -> None:
        """Signal the broker's whole process group, falling back to the proc.

        ``npm start`` spawns ``node main.js`` as a child; signalling only the
        ``npm`` process leaves ``node`` holding the MQTT port. The process was
        started with ``start_new_session=True`` so its PID is the group leader —
        ``killpg`` reaches npm + node + any helpers in one shot. ``getpgid`` /
        ``killpg`` are POSIX-only; on other platforms we degrade to the proc.
        """
        proc = self._proc
        if proc is None or proc.pid is None:
            return
        getpgid = getattr(os, "getpgid", None)
        killpg = getattr(os, "killpg", None)
        try:
            if getpgid and killpg:
                killpg(getpgid(proc.pid), sig)
            elif sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError):
            pass

    async def stop(self) -> None:
        """Terminate the broker process *group* and stop draining."""
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._drain_task = None
        if self._proc is not None and self._proc.returncode is None:
            self._signal_tree(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except TimeoutError:
                self._signal_tree(signal.SIGKILL)
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except TimeoutError:
                    log.warning("protomq process did not exit after SIGKILL")
        self._proc = None
