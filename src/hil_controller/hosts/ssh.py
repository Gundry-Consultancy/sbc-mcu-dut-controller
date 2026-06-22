"""asyncssh-backed HostTransport implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from hil_controller.hosts.base import ExecResult

log = logging.getLogger(__name__)


class SSHTransport:
    def __init__(
        self,
        host: str,
        user: str = "pi",
        key_path: Path | None = None,
        known_hosts: str | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.user = user
        self.key_path = key_path
        self.known_hosts = known_hosts  # path to known_hosts file, or None to disable checking
        self.connect_timeout = connect_timeout

    def _connect_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "host": self.host,
            "username": self.user,
            "connect_timeout": self.connect_timeout,
        }
        if self.key_path:
            kw["client_keys"] = [str(self.key_path)]
        if self.known_hosts is None:
            kw["known_hosts"] = None
        else:
            kw["known_hosts"] = self.known_hosts
        return kw

    async def exec(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        cwd: str | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> ExecResult:
        import asyncssh

        cmd = " ".join(_shell_quote(a) for a in argv)
        if cwd:
            cmd = f"cd {_shell_quote(cwd)} && {cmd}"

        # asyncssh runs channels in str/UTF-8 mode: input must be str (bytes
        # raise "utf_8_encode() argument 1 must be str, not bytes"), and output
        # is decoded strictly unless errors="replace" is set. Our stdin is always
        # text we encoded ourselves, so decoding back is lossless.
        ssh_input = stdin.decode("utf-8") if isinstance(stdin, (bytes, bytearray)) else stdin

        kw = self._connect_kwargs()
        if on_line is None:
            async with asyncssh.connect(**kw) as conn:
                result = await conn.run(
                    cmd, env=env, input=ssh_input, encoding="utf-8", errors="replace"
                )
            return ExecResult(
                exit_status=result.exit_status or 0,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )

        # Streaming path: fire on_line per stdout line as it arrives (HIL capture
        # reacts to in-test WS_HIL_CAPTURE markers) while accumulating the full
        # output and stderr for the run.log asset.
        out_parts: list[str] = []
        err_parts: list[str] = []
        async with asyncssh.connect(**kw) as conn:
            async with conn.create_process(
                cmd, env=env, encoding="utf-8", errors="replace"
            ) as proc:
                if ssh_input is not None:
                    proc.stdin.write(ssh_input)
                    proc.stdin.write_eof()

                async def _pump_out() -> None:
                    async for line in proc.stdout:
                        out_parts.append(line)
                        try:
                            on_line(line.rstrip("\n"))
                        except Exception:  # pragma: no cover - must not kill the run
                            log.exception("on_line callback raised")

                async def _pump_err() -> None:
                    async for line in proc.stderr:
                        err_parts.append(line)

                await asyncio.gather(_pump_out(), _pump_err())
                await proc.wait()
                rc = proc.exit_status or 0

        return ExecResult(exit_status=rc, stdout="".join(out_parts), stderr="".join(err_parts))

    async def stream(self, argv: list[str]) -> AsyncIterator[bytes]:
        import asyncssh

        cmd = " ".join(_shell_quote(a) for a in argv)
        kw = self._connect_kwargs()

        async with asyncssh.connect(**kw) as conn:
            async with conn.create_process(cmd, encoding="utf-8", errors="replace") as proc:
                async for line in proc.stdout:
                    yield line.encode() if isinstance(line, str) else line

    async def copy_to(self, local: Path, remote: PurePosixPath) -> None:
        import asyncssh

        kw = self._connect_kwargs()
        async with asyncssh.connect(**kw) as conn:
            await asyncssh.scp(str(local), (conn, str(remote)))

    async def copy_from(self, remote: PurePosixPath, local: Path) -> None:
        import asyncssh

        kw = self._connect_kwargs()
        async with asyncssh.connect(**kw) as conn:
            await asyncssh.scp((conn, str(remote)), str(local))

    async def healthcheck(self) -> bool:
        try:
            result = await self.exec(["true"])
            return result.exit_status == 0
        except Exception as exc:
            log.debug("SSH healthcheck failed for %s: %s", self.host, exc)
            return False


def _shell_quote(s: str) -> str:
    """Minimal shell quoting — wrap in single quotes, escape embedded singles."""
    return "'" + s.replace("'", "'\"'\"'") + "'"
