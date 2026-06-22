"""LocalTransport: run commands on the local machine via asyncio subprocesses."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path, PurePosixPath

from hil_controller.hosts.base import ExecResult

log = logging.getLogger(__name__)


class LocalTransport:
    """HostTransport implementation that runs commands on the local machine."""

    async def exec(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        cwd: str | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> ExecResult:
        merged_env = {**os.environ, **(env or {})}
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            cwd=cwd,
            env=merged_env,
        )
        if on_line is None:
            stdout_b, stderr_b = await proc.communicate(input=stdin)
            rc = proc.returncode if proc.returncode is not None else 0
            log.debug("local exec %s → exit %d", argv[0], rc)
            return ExecResult(
                exit_status=rc,
                stdout=stdout_b.decode(errors="replace"),
                stderr=stderr_b.decode(errors="replace"),
            )

        # Streaming path: accumulate the full output but also fire on_line per
        # stdout line as it arrives (used by HIL capture to react to in-test
        # WS_HIL_CAPTURE stage markers while the run is still going).
        out_parts: list[str] = []
        err_parts: list[str] = []
        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin)
            proc.stdin.close()

        async def _pump_stdout() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace")
                out_parts.append(line)
                try:
                    on_line(line.rstrip("\n"))
                except Exception:  # pragma: no cover - callback must not kill the run
                    log.exception("on_line callback raised")

        async def _pump_stderr() -> None:
            assert proc.stderr is not None
            async for raw in proc.stderr:
                err_parts.append(raw.decode(errors="replace"))

        await asyncio.gather(_pump_stdout(), _pump_stderr())
        rc = await proc.wait()
        log.debug("local exec %s (streamed) → exit %d", argv[0], rc)
        return ExecResult(
            exit_status=rc, stdout="".join(out_parts), stderr="".join(err_parts)
        )

    async def stream(self, argv: list[str]) -> AsyncIterator[bytes]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        async for line in proc.stdout:
            yield line

    async def copy_to(self, local: Path, remote: PurePosixPath) -> None:
        shutil.copy2(str(local), str(remote))

    async def copy_from(self, remote: PurePosixPath, local: Path) -> None:
        shutil.copy2(str(remote), str(local))

    async def healthcheck(self) -> bool:
        try:
            result = await self.exec(["true"])
            return result.exit_status == 0
        except Exception as exc:
            log.debug("local healthcheck failed: %s", exc)
            return False
