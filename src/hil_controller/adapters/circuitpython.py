"""CircuitPython file deployment and bounded, fresh serial assertions."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from hil_controller.adapters.msc_secrets import _mount_msc, resolve_msc_device

if TYPE_CHECKING:
    from hil_controller.adapters.bench_stages import BenchContext


def validate_files(files: dict[str, str]) -> None:
    """Accept only relative FAT paths, with code.py written last."""
    if not files or "code.py" not in files:
        raise ValueError("CircuitPython files must include code.py")
    for name, content in files.items():
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or any(p in {".", "..", ""} for p in name.split("/"))
            or any(c in name for c in "\\:\x00\r\n")
            or name.casefold() in {"settings.toml", "boot.py"}
        ):
            raise ValueError("Unsafe or reserved CircuitPython file path")
        if not isinstance(content, str):
            raise ValueError("CircuitPython files must contain UTF-8 text")
    if sum(len(v.encode()) for v in files.values()) > 1024 * 1024:
        raise ValueError("CircuitPython text files exceed 1 MiB")


async def write_circuitpython(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Write settings.toml and test files on the selected board, then unmount."""
    files = dict(stage.get("files") or {})
    validate_files(files)
    host = stage.get("io_url") or ctx.protomq_host
    port = stage.get("io_port") or ctx.protomq_port
    if not host or not port:
        raise ValueError("write_circuitpython needs a running protoMQ or explicit io_url/io_port")
    settings = {
        "CIRCUITPY_WIFI_SSID": ctx.secrets.get("WIFI_SSID", ""),
        "CIRCUITPY_WIFI_PASSWORD": ctx.secrets.get("WIFI_PASSWORD", ""),
        "ADAFRUIT_IO_USERNAME": ctx.secrets.get("IO_USERNAME", ""),
        "ADAFRUIT_IO_KEY": ctx.secrets.get("IO_KEY", ""),
        "MQTT_HOST": host,
        "MQTT_PORT": int(port),
    }
    # JSON scalar strings/integers are valid TOML values; escape newlines and quotes.
    files["settings.toml"] = "".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in settings.items()
    )
    dev = await resolve_msc_device(ctx.dut_transport, stage.get("msc_filter") or ctx.msc_filter)
    _, mount, unmount = await _mount_msc(ctx.dut_transport, dev, read_only=False)
    try:
        # Board firmware/library compatibility stays the test author's responsibility.
        # Do not send contents through tee (which echoes credentials into transcripts).
        for name in sorted(files, key=lambda n: (n == "code.py", n)):
            dest = str(PurePosixPath(mount) / name)
            result = await ctx.dut_transport.exec(
                [
                    "python3",
                    "-c",
                    "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                    "p.parent.mkdir(parents=True,exist_ok=True); "
                    "p.write_bytes(sys.stdin.buffer.read())",
                    dest,
                ],
                stdin=files[name].encode(),
            )
            if result.exit_status != 0:
                raise RuntimeError(
                    "CircuitPython write failed; check host write access to CIRCUITPY"
                )
        result = await ctx.dut_transport.exec(["sync"])
        if result.exit_status != 0:
            raise RuntimeError("CircuitPython filesystem sync failed")
    finally:
        await ctx.dut_transport.exec(unmount)
    ctx.log_line("CIRCUITPYTHON_DEPLOY_VERDICT written=true")


async def assert_serial(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Assert a literal marker in this job's serial log, bounded by timeout_s."""
    marker = stage.get("contains")
    timeout = float(stage.get("timeout_s", 60))
    if not isinstance(marker, str) or not marker or not 0 < timeout <= 600:
        raise ValueError("assert_serial requires contains and timeout_s in (0, 600]")
    if not ctx.serial_log_path:
        raise ValueError("assert_serial requires serial capture")
    path = Path(ctx.serial_log_path)
    deadline = time.monotonic() + timeout
    # Logs belong to one job; read incrementally to bound memory on noisy boards.
    offset = ctx.serial_assert_offset
    tail = ""
    while True:
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as log:
                log.seek(offset)
                chunk = log.read(65536)
                offset = log.tell()
            if marker in tail + chunk:
                ctx.log_line("SERIAL_ASSERT_VERDICT ok=true")
                return
            tail = (tail + chunk)[-len(marker) :]
            if chunk and time.monotonic() < deadline:
                continue
        if time.monotonic() >= deadline:
            ctx.log_line("SERIAL_ASSERT_VERDICT ok=false")
            raise RuntimeError("Serial success marker was not observed before the deadline")
        await asyncio.sleep(0.1)


async def serial_checkpoint(stage: dict[str, Any], ctx: BenchContext) -> None:
    """Only subsequent serial bytes count as proof for an assert_serial stage."""
    path = Path(ctx.serial_log_path) if ctx.serial_log_path else None
    ctx.serial_assert_offset = path.stat().st_size if path and path.exists() else 0
