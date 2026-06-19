"""NoOpFlasher — succeeds for every verb without touching hardware.

Used for pre-provisioned devices, for SBC jobs that only run the test
phase, and as a default in tests that need a Flasher-shaped object but
don't care about the work.
"""

from __future__ import annotations

from typing import Any, Literal

from hil_controller.adapters.flashers.base import (
    Artifact,
    ChipInfo,
    FlashResult,
)


class NoOpFlasher:
    """Implements :class:`FlasherProtocol`; every verb is a successful no-op."""

    name = "noop"

    def __init__(
        self,
        *,
        transport: Any = None,
        port: str = "",
        family: str = "unknown",
    ) -> None:
        # transport/port are accepted for parity with other flashers but
        # never used. ``family`` lets the caller pin the ChipInfo reply.
        self.transport = transport
        self.port = port
        self.family = family

    async def probe(self) -> ChipInfo:
        return ChipInfo(family=self.family, raw={"noop": "true"})

    async def erase(self) -> None:
        return None

    async def flash(self, artifact: Artifact) -> FlashResult:
        return FlashResult(bytes_written=0, elapsed_s=0.0)

    async def reset(self, *, into: Literal["bootloader", "application"]) -> None:
        return None
