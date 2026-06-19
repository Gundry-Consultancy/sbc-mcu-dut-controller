"""LocalTransport: run commands on the local machine via asyncio subprocesses."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import AsyncIterator
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
        stdout_b, stderr_b = await proc.communicate(input=stdin)
        rc = proc.returncode if proc.returncode is not None else 0
        log.debug("local exec %s → exit %d", argv[0], rc)
        return ExecResult(
            exit_status=rc,
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
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
