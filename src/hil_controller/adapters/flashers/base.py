"""Flasher Protocol + CliFlasher base class.

Four-verb contract every concrete flasher satisfies::

    probe()   -> ChipInfo
    erase()   -> None
    flash(artifact) -> FlashResult
    reset(*, into="bootloader"|"application") -> None

CliFlasher provides the common scaffolding for flashers that drive an
external CLI tool (esptool, picotool, bossac, dfu-util, ...). Subclasses
override ``name``/``tool`` and the per-verb argv builders + parsers.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #


class FlasherError(Exception):
    """Base for flasher errors."""


class FlasherToolMissing(FlasherError):
    """The CLI tool was not found on the transport's PATH."""


class FlasherToolFailed(FlasherError):
    """The wrapped CLI tool exited non-zero."""

    def __init__(
        self,
        *,
        tool: str,
        argv: list[str],
        exit_status: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.tool = tool
        self.argv = argv
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr
        summary = (stderr or stdout or "").strip().splitlines()[:3]
        super().__init__(f"{tool} failed (exit {exit_status}): " + " | ".join(summary))


class FlasherUnsupported(FlasherError):
    """The flasher doesn't implement this verb for this device."""


class FlasherNeedsExternalReset(FlasherError):
    """Bootloader entry needs an external adapter (solenoid / GPIO / serial sentinel)."""


# --------------------------------------------------------------------------- #
# Dataclasses                                                                 #
# --------------------------------------------------------------------------- #


@dataclass
class Artifact:
    """A firmware artifact + how to write it.

    ``path`` is a path on the transport that hosts the flash (so it's
    accessible to the CLI tool). ``kind`` is a hint for the flasher
    (``bin`` / ``uf2`` / ``elf`` / ``combined_bin``). ``offset`` is the
    flash address for tools that need one (esptool); ``None`` means the
    tool's default is fine. ``label`` is shown in job events.
    """

    path: str
    kind: str = "bin"
    offset: int | None = None
    label: str | None = None


@dataclass
class ChipInfo:
    """Identity information discovered by ``probe()``."""

    family: str
    mac: str | None = None
    flash_bytes: int | None = None
    unique_id: str | None = None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class FlashResult:
    """Outcome of ``flash()``."""

    bytes_written: int
    elapsed_s: float
    raw_stdout: str = ""
    raw_stderr: str = ""


# --------------------------------------------------------------------------- #
# Protocol                                                                    #
# --------------------------------------------------------------------------- #


@runtime_checkable
class FlasherProtocol(Protocol):
    """Uniform four-verb interface every flasher implements."""

    name: str

    async def probe(self) -> ChipInfo: ...

    async def erase(self) -> None: ...

    async def flash(self, artifact: Artifact) -> FlashResult: ...

    async def reset(self, *, into: Literal["bootloader", "application"]) -> None: ...


# --------------------------------------------------------------------------- #
# CLI-tool base class                                                         #
# --------------------------------------------------------------------------- #


class CliFlasher:
    """Base for flashers that drive an external CLI tool over a transport.

    Provides ``_locate`` (PATH probe + cache), ``_run`` (subprocess
    invocation through the transport with structured error wrapping),
    optional ``sudo`` prefixing, and defaults for the four verbs that
    raise :class:`FlasherUnsupported` so subclasses can implement
    them incrementally.
    """

    #: Human-readable flasher name (used in job events, error messages).
    name: str = "cli-flasher"

    #: The CLI binary as it appears on PATH (overridden by subclasses).
    tool: str = ""

    def __init__(
        self,
        *,
        transport: Any,
        port: str,
        sudo: bool = False,
    ) -> None:
        self.transport = transport
        self.port = port
        self._sudo = sudo
        self._tool_path: str | None = None
        #: Optional sink called after every CLI invocation with
        #: ``(argv, result)`` — lets callers capture a full command + stdout/
        #: stderr transcript (e.g. a verifiable flash.log) without each verb
        #: having to thread its output back. Set the attribute after construction.
        self.on_output: Any | None = None

    # ------------------------------------------------------------------ #
    # Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _locate(self) -> str:
        """Resolve ``self.tool`` on the transport's PATH (cached).

        Raises :class:`FlasherToolMissing` when ``command -v`` returns
        nothing. The first successful lookup is cached on the instance.
        """
        if self._tool_path is not None:
            return self._tool_path
        if not self.tool:
            raise FlasherToolMissing(
                f"{self.name}: no `tool` set on subclass — cannot locate CLI binary"
            )
        result = await self.transport.exec(
            ["bash", "-c", f"command -v {shlex.quote(self.tool)} || true"]
        )
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            raise FlasherToolMissing(
                f"{self.tool!r} not found on transport PATH "
                f"(install it on the host, or override `tool` on the subclass)"
            )
        self._tool_path = lines[0]
        return self._tool_path

    def _argv_prefix(self) -> list[str]:
        return ["sudo"] if self._sudo else []

    async def _run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> Any:
        """Invoke a CLI argv list through the transport, wrapping errors.

        ``check=False`` returns the :class:`ExecResult` even on non-zero
        exit (useful for diagnostic commands).
        """
        full = self._argv_prefix() + argv
        coro = self.transport.exec(full, cwd=cwd, env=env)
        if timeout is not None:
            result = await asyncio.wait_for(coro, timeout=timeout)
        else:
            result = await coro
        if self.on_output is not None:
            try:
                self.on_output(full, result)
            except Exception:  # noqa: BLE001 — a transcript sink must never break a flash
                pass
        if check and result.exit_status != 0:
            raise FlasherToolFailed(
                tool=self.name,
                argv=full,
                exit_status=result.exit_status,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )
        return result

    # ------------------------------------------------------------------ #
    # Default verb implementations                                        #
    # ------------------------------------------------------------------ #

    async def probe(self) -> ChipInfo:
        raise FlasherUnsupported(f"{self.name}.probe() not implemented")

    async def erase(self) -> None:
        raise FlasherUnsupported(f"{self.name}.erase() not implemented")

    async def flash(self, artifact: Artifact) -> FlashResult:
        raise FlasherUnsupported(f"{self.name}.flash() not implemented")

    async def reset(self, *, into: Literal["bootloader", "application"]) -> None:
        raise FlasherUnsupported(f"{self.name}.reset(into={into!r}) not implemented")
