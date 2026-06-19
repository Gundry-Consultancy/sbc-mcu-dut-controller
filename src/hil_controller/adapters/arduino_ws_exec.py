"""Phase-aware arduino-ws adapter: route each job phase to a chosen host.

WipperSnapper arduino-ws jobs used to run every phase on the DUT's host. That
host (rpi-displays) is too weak to compile WipperSnapper, so builds now route
to the controller. This adapter holds two transports — ``controller_transport``
(the controller, typically ``LocalTransport``) and ``dut_transport`` (the host
physically holding the DUT, over SSH) — and routes per an ``exec`` plan::

    exec = {
      "build_host":   "controller" | "dut-host",          # where `pio run` compiles
      "flash_mode":   "usbip" | "ship-artifacts",          # how firmware reaches the DUT
      "test_host":    "controller" | "dut-host" | "none",  # where the run command runs
      "protomq_host": "controller" | "dut-host" | "off",
      "pio_env":      "<platformio env>",
    }

Clone+build+secrets+run are delegated to an inner :class:`GitDeployAdapter`
bound to the run/build transport; this adapter adds the **flash** phase as a
distinct step (it is no longer fused into the build chain):

* ``usbip`` — bind the DUT's busid on its host, attach it onto the controller,
  upload from the controller against the freshly-enumerated serial port. Wrapped
  in an ``exclusive_device`` lease and torn down in a ``finally``.
* ``ship-artifacts`` — copy the built ``.pio/build/<env>/`` set to the DUT host
  and run esptool there.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from hil_controller.adapters.git_deploy import GitDeployAdapter
from hil_controller.adapters.usbip_bridge import UsbipBridge

log = logging.getLogger(__name__)

# esptool flash layout for ESP32-S3 (offsets match the Arduino/PlatformIO build).
_ESP32S3_ARTIFACTS = [
    ("0x0", "bootloader.bin"),
    ("0x8000", "partitions.bin"),
    ("0xe000", "boot_app0.bin"),
    ("0x10000", "firmware.bin"),
]


class ArduinoWsExecAdapter:
    def __init__(
        self,
        *,
        controller_transport: Any,
        dut_transport: Any,
        job_id: str,
        source: dict[str, Any],
        params: dict[str, Any],
        exec_plan: dict[str, Any],
        device: dict[str, Any],
        server_addr: str,
        secrets: dict[str, str] | None = None,
        secrets_format: str = "dotenv",
        work_dir: PurePosixPath | None = None,
    ) -> None:
        self.controller_transport = controller_transport
        self.dut_transport = dut_transport
        self.job_id = job_id
        self.source = source
        self.params = params
        self.exec_plan = exec_plan
        self.device = device
        self.server_addr = server_addr
        self.work_dir = work_dir or PurePosixPath(f"/tmp/hil/{job_id}")
        self._pio_env = exec_plan.get("pio_env", "")
        self._settle_s = 2.0

        self._build_host = exec_plan.get("build_host", "controller")
        self._flash_mode = exec_plan.get("flash_mode", "usbip")
        self._run_host = self._resolve_run_host()

        # Inner adapter handles clone + setup (compile-only) + secrets + run on
        # the build/run transport. Build and run must share a host (see deploy()).
        self._inner = GitDeployAdapter(
            transport=self._transport_for(self._build_host),
            job_id=job_id,
            source=source,
            params=params,
            secrets=secrets,
            secrets_format=secrets_format,
            work_dir=self.work_dir,
        )

        self._deploy_stdout = ""
        self._deploy_stderr = ""
        self._run_stdout = ""
        self._run_stderr = ""

    # ------------------------------------------------------------------ #
    # plan helpers                                                        #
    # ------------------------------------------------------------------ #

    def _resolve_run_host(self) -> str:
        test_host = self.exec_plan.get("test_host", "none")
        if test_host and test_host != "none":
            return test_host
        protomq_host = self.exec_plan.get("protomq_host", "off")
        if protomq_host and protomq_host != "off":
            return protomq_host
        return self._build_host

    def _transport_for(self, host: str) -> Any:
        if host == "dut-host":
            return self.dut_transport
        return self.controller_transport

    # ------------------------------------------------------------------ #
    # DeviceAdapter protocol                                              #
    # ------------------------------------------------------------------ #

    async def acquire(self) -> None:
        pass

    async def reset(self) -> None:
        pass

    async def flash(self, artifact: dict) -> None:
        await self.deploy()

    async def open_serial(self):
        return iter([])

    async def release(self) -> None:
        pass

    async def cleanup(self) -> None:
        await self._inner.cleanup()

    # ------------------------------------------------------------------ #
    # phases                                                              #
    # ------------------------------------------------------------------ #

    async def deploy(self) -> None:
        if self._run_host != self._build_host:
            raise NotImplementedError(
                f"build_host={self._build_host!r} and run_host={self._run_host!r} "
                "differ; cross-host build+run is not supported yet (the cloned "
                "repo + protomq only exist on the build host). Run build and "
                "test/protomq on the same host."
            )

        await self._inner.deploy()
        self._deploy_stdout = self._inner._deploy_stdout
        self._deploy_stderr = self._inner._deploy_stderr

        await self._flash()

    async def run(self) -> str:
        result = await self._inner.run()
        self._run_stdout = self._inner._run_stdout
        self._run_stderr = self._inner._run_stderr
        return result

    # ------------------------------------------------------------------ #
    # flash                                                               #
    # ------------------------------------------------------------------ #

    async def _flash(self) -> None:
        if self._flash_mode == "ship-artifacts":
            await self._flash_ship_artifacts()
        else:
            await self._flash_usbip()

    async def _flash_usbip(self) -> None:
        # The job's exclusive_device lease is held by the scheduler for the
        # whole job lifetime (queue/scheduler.py), covering deploy+flash+run.
        # This phase must NOT take a second lease — that conflicts with the
        # job's own lease and self-deadlocks the flash. The usbip bridge
        # context manager handles its own bind/attach/detach/unbind teardown.
        busid = self.device["hub_port_path"]
        bridge = UsbipBridge(
            server_tp=self.dut_transport,
            client_tp=self.controller_transport,
            server_addr=self.server_addr,
            busid=busid,
            settle_s=self._settle_s,
        )
        async with bridge.attached() as port:
            if not port:
                raise RuntimeError(
                    f"usbip: no serial port appeared on the controller after "
                    f"attaching busid {busid} from {self.server_addr}"
                )
            self._deploy_stdout += f"\n$ usbip-attached {busid} → {port}\n"
            upload = [
                "bash",
                "-c",
                f". .venv/bin/activate && pio run -e {self._pio_env} "
                f"--target upload --upload-port {port}",
            ]
            res = await self.controller_transport.exec(upload, cwd=str(self.work_dir))
            self._deploy_stdout += res.stdout
            self._deploy_stderr += res.stderr
            if res.exit_status != 0:
                raise RuntimeError(
                    f"flash (pio upload) failed (exit {res.exit_status}): {res.stderr}"
                )

    async def _flash_ship_artifacts(self) -> None:
        build_dir = self.work_dir / ".pio" / "build" / self._pio_env
        remote_dir = PurePosixPath("/tmp/hil") / f"{self.job_id}-fw"
        await self.dut_transport.exec(["mkdir", "-p", str(remote_dir)])

        with tempfile.TemporaryDirectory() as td:
            for _, fname in _ESP32S3_ARTIFACTS:
                local = Path(td) / fname
                await self.controller_transport.copy_from(build_dir / fname, local)
                await self.dut_transport.copy_to(local, remote_dir / fname)

        port = self.device.get("serial_port") or "/dev/ttyACM0"
        write_args = " ".join(f"{off} {name}" for off, name in _ESP32S3_ARTIFACTS)
        cmd = [
            "bash",
            "-c",
            f"esptool.py --chip esp32s3 --port {port} write_flash {write_args}",
        ]
        res = await self.dut_transport.exec(cmd, cwd=str(remote_dir))
        self._deploy_stdout += res.stdout
        self._deploy_stderr += res.stderr
        if res.exit_status != 0:
            raise RuntimeError(
                f"flash (esptool ship-artifacts) failed (exit {res.exit_status}): {res.stderr}"
            )
