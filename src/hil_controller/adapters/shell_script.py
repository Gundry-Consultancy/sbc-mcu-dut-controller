"""ShellScriptAdapter: run an inline shell script via any transport."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class ShellScriptAdapter:
    """Fulfils the DeviceAdapter protocol for inline-script jobs (no git source)."""

    def __init__(self, transport: Any, script: str) -> None:
        self.transport = transport
        self.script = script
        self._run_stdout = ""
        self._run_stderr = ""

    async def acquire(self) -> None:
        pass

    async def release(self) -> None:
        pass

    async def run(self) -> str:
        result = await self.transport.exec(["sh", "-c", self.script])
        self._run_stdout = result.stdout
        self._run_stderr = result.stderr
        log.info("shell script exit %d", result.exit_status)
        return "pass" if result.exit_status == 0 else "fail"
