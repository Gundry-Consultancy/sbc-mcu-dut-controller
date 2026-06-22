"""HostTransport protocol and ExecResult dataclass."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable


@dataclass
class ExecResult:
    exit_status: int
    stdout: str
    stderr: str


@runtime_checkable
class HostTransport(Protocol):
    async def exec(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        cwd: str | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> ExecResult: ...

    async def stream(self, argv: list[str]) -> AsyncIterator[bytes]: ...

    async def copy_to(self, local: Path, remote: PurePosixPath) -> None: ...

    async def copy_from(self, remote: PurePosixPath, local: Path) -> None: ...

    async def healthcheck(self) -> bool: ...
