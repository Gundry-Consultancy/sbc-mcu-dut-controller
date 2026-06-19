"""TinyUf2Installer — orchestrates fetch + usbip attach + esptool erase/flash.

Bridges the four M3.5 building blocks into the one operator action
"install TinyUF2 bootloader on this DUT":

1. :class:`TinyUf2Fetcher` resolves the GitHub release for the board
   (with chip-family fallback) and extracts ``combined.bin`` into the
   controller's local cache.
2. :class:`UsbipBridge` binds the busid on the hub host and attaches
   it onto the controller, yielding the freshly-enumerated serial port.
3. :class:`EsptoolFlasher` runs on the controller against that port:
   ``erase_flash`` then ``write_flash 0x0 combined.bin``.
4. The bridge context manager tears down (detach + unbind) in a
   ``finally`` so a mid-install crash never leaves the busid bound.

Returns a :class:`TinyUf2InstallResult` dict the route handler renders
back to the UI; the resolved tag + asset name + sha-256 land in there
so the operator has a reproducibility footprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hil_controller.adapters.flashers import Artifact
from hil_controller.adapters.flashers.esptool import EsptoolFlasher
from hil_controller.adapters.tinyuf2_fetcher import TinyUf2Fetched, TinyUf2Fetcher
from hil_controller.adapters.usbip_bridge import UsbipBridge


@dataclass
class TinyUf2InstallResult:
    """Outcome of a TinyUF2 install — what's surfaced to the UI."""

    board_name: str
    tag: str
    asset_name: str
    digest_sha256: str
    serial_port: str
    bytes_written: int
    elapsed_s: float
    erase_stdout: str = ""
    flash_stdout: str = ""


class TinyUf2InstallError(RuntimeError):
    """Setup failed before we got far enough to start the flasher."""


class TinyUf2Installer:
    """Compose fetch + usbip + erase + flash for a single TinyUF2 install."""

    def __init__(
        self,
        *,
        controller_transport: Any,
        dut_transport: Any,
        server_addr: str,
        busid: str,
        board_name: str,
        fetcher: TinyUf2Fetcher | None = None,
        esptool_chip: str = "auto",
        esptool_baud: int = 921600,
        settle_s: float = 2.0,
    ) -> None:
        self.controller_transport = controller_transport
        self.dut_transport = dut_transport
        self.server_addr = server_addr
        self.busid = busid
        self.board_name = board_name
        self.fetcher = fetcher or TinyUf2Fetcher()
        self.esptool_chip = esptool_chip
        self.esptool_baud = esptool_baud
        self.settle_s = settle_s

    async def install(
        self,
        *,
        tag: str = "latest",
        fallback_board: str | None = None,
    ) -> TinyUf2InstallResult:
        """Run the full pipeline. Raises on any failure."""
        fetched: TinyUf2Fetched = await self.fetcher.fetch(
            board_name=self.board_name,
            tag=tag,
            fallback_board=fallback_board,
        )
        bridge = UsbipBridge(
            server_tp=self.dut_transport,
            client_tp=self.controller_transport,
            server_addr=self.server_addr,
            busid=self.busid,
            settle_s=self.settle_s,
        )
        async with bridge.attached() as port:
            if not port:
                raise TinyUf2InstallError(
                    f"usbip attach of busid {self.busid} from {self.server_addr} "
                    f"completed but no new serial port appeared on the controller; "
                    f"check `usbip port` and dmesg there"
                )
            flasher = EsptoolFlasher(
                transport=self.controller_transport,
                port=port,
                chip=self.esptool_chip,
                baud=self.esptool_baud,
            )
            erase_res = await flasher._run(flasher._base_argv() + ["erase_flash"])
            artifact = Artifact(
                path=str(fetched.path),
                kind="combined_bin",
                offset=0,
                label=f"tinyuf2 {fetched.tag}",
            )
            flash_res = await flasher.flash(artifact)
        return TinyUf2InstallResult(
            board_name=self.board_name,
            tag=fetched.tag,
            asset_name=fetched.asset_name,
            digest_sha256=fetched.digest_sha256,
            serial_port=port,
            bytes_written=flash_res.bytes_written,
            elapsed_s=flash_res.elapsed_s,
            erase_stdout=erase_res.stdout or "",
            flash_stdout=flash_res.raw_stdout,
        )
