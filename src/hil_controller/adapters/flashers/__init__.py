"""Flasher chain for the MCU adapter layer.

Each MCU family (ESP32, RP2040, RP2350, SAMD51, ...) has a concrete
:class:`FlasherProtocol` implementation that drives the appropriate
CLI tool (or the MSC-mount flow for UF2 bootloaders) through a
:class:`hil_controller.hosts.base.HostTransport`.

See ``docs/ARCHITECTURE.md`` section 16 (M3.5 / M4) for the design.
"""

from hil_controller.adapters.flashers.base import (
    Artifact,
    ChipInfo,
    CliFlasher,
    FlasherError,
    FlasherNeedsExternalReset,
    FlasherProtocol,
    FlasherToolFailed,
    FlasherToolMissing,
    FlasherUnsupported,
    FlashResult,
)
from hil_controller.adapters.flashers.bossac import BossacFlasher
from hil_controller.adapters.flashers.esptool import EsptoolFlasher
from hil_controller.adapters.flashers.noop import NoOpFlasher
from hil_controller.adapters.flashers.pio_upload import PioUploadFlasher
from hil_controller.adapters.flashers.uf2_msc import Uf2MscFlasher

__all__ = [
    "Artifact",
    "BossacFlasher",
    "ChipInfo",
    "CliFlasher",
    "EsptoolFlasher",
    "Uf2MscFlasher",
    "FlashResult",
    "FlasherError",
    "FlasherNeedsExternalReset",
    "FlasherProtocol",
    "FlasherToolFailed",
    "FlasherToolMissing",
    "FlasherUnsupported",
    "NoOpFlasher",
    "PioUploadFlasher",
]
