"""PioUploadFlasher — wraps ``pio run --target upload`` behind FlasherProtocol.

Lets the existing arduino-ws flow route through the same uniform call site
as raw-`.bin` flashing: ``flasher.flash(artifact)``. The artifact is largely
informational here — PlatformIO already knows what to upload from
``platformio.ini``; the flasher knows ``cwd`` and ``--upload-port``.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from hil_controller.adapters.flashers.base import (
    Artifact,
    CliFlasher,
    FlasherUnsupported,
    FlashResult,
)


class PioUploadFlasher(CliFlasher):
    """Wraps ``pio run -e <env> --target upload --upload-port <port>``.

    Constructed with the workspace dir (the PlatformIO project root with
    ``platformio.ini``) and the env name. ``flash()`` invokes ``pio run``
    in that directory; ``erase()`` calls ``pio run --target erase``;
    ``probe()`` and ``reset()`` are :class:`FlasherUnsupported` (use an
    EsptoolFlasher / PicotoolFlasher for those — pio is upload-only).
    """

    name = "pio-upload"
    tool = "pio"

    def __init__(
        self,
        *,
        transport: Any,
        port: str,
        workspace_dir: str,
        pio_env: str,
        sudo: bool = False,
    ) -> None:
        super().__init__(transport=transport, port=port, sudo=sudo)
        self.workspace_dir = workspace_dir
        self.pio_env = pio_env

    def _shell(self, target: str) -> list[str]:
        # bash -c keeps the venv-activation pattern from the existing
        # ArduinoWsExecAdapter intact. Callers that don't need a venv
        # can override by subclassing.
        cmd = (
            f". .venv/bin/activate 2>/dev/null; "
            f"pio run -e {self.pio_env} --target {target} "
            f"--upload-port {self.port}"
        )
        return ["bash", "-c", cmd]

    async def flash(self, artifact: Artifact) -> FlashResult:
        t0 = time.monotonic()
        result = await self._run(self._shell("upload"), cwd=self.workspace_dir)
        elapsed = time.monotonic() - t0
        # pio doesn't report bytes-written in a stable, parseable form; leave 0
        # and let callers fall back to artifact size if they care.
        return FlashResult(
            bytes_written=0,
            elapsed_s=elapsed,
            raw_stdout=result.stdout or "",
            raw_stderr=result.stderr or "",
        )

    async def erase(self) -> None:
        await self._run(self._shell("erase"), cwd=self.workspace_dir)

    async def reset(self, *, into: Literal["bootloader", "application"]) -> None:
        # PlatformIO drives the platform's own bootloader-entry sequence
        # as part of upload (e.g. esptool's DTR/RTS for ESP32, picotool's
        # BOOTSEL for RP). No standalone reset verb; use EsptoolFlasher /
        # PicotoolFlasher / SolenoidHubAdapter directly when an explicit
        # reset is needed.
        raise FlasherUnsupported(
            "pio-upload.reset(): use an EsptoolFlasher / PicotoolFlasher / "
            "SolenoidHubAdapter for explicit resets; PlatformIO upload drives "
            "bootloader entry as part of the upload itself."
        )
