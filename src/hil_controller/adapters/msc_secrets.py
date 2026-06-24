"""Write ``secrets.json`` onto a DUT's USB mass-storage (MSC) volume.

After a WipperSnapper ``.fatfs`` image is flashed and the board reboots, it
exposes a FAT volume over USB MSC; dropping ``secrets.json`` on it points the
firmware at a broker. This module does that on the host physically holding the
DUT:

1. resolve the block device under ``/dev/disk/by-id`` via a caller-supplied
   filter (job override, or the DUT profile assigned from the usbip page —
   matched on iSerial / by-id / label, never VID per project policy);
2. mount it read-write with ``udisksctl`` (rootless on Pi OS; it mounts as the
   login user so a plain ``tee`` can write, and it reports the mountpoint);
3. render + ``tee`` ``secrets.json`` (reusing the stdin-to-``tee`` pattern from
   :class:`GitDeployAdapter`);
4. ``sync`` and ``udisksctl unmount``.

It is intentionally a small, self-contained unit so it can be swapped for a
different secrets-delivery mechanism later without touching the stage pipeline.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import shlex
from pathlib import PurePosixPath
from typing import Any, Optional

log = logging.getLogger(__name__)


class MscError(RuntimeError):
    """MSC resolve / mount / write failed."""


# --------------------------------------------------------------------------- #
# Pure helpers (top-level, unit-testable)                                     #
# --------------------------------------------------------------------------- #

# udisksctl prints e.g. "Mounted /dev/sda at /media/pi/WIPPER." (trailing dot
# varies by version); capture the path after " at ".
_MOUNTED_AT_RE = re.compile(r"\bat\s+(\S+?)\.?\s*$")


def parse_udisks_mountpoint(text: str) -> str | None:
    """Pull the mountpoint out of ``udisksctl mount`` stdout, or ``None``."""
    for line in (text or "").splitlines():
        m = _MOUNTED_AT_RE.search(line.strip())
        if m:
            return m.group(1)
    return None


def select_block_device(by_id_names: list[str], msc_filter: str) -> str | None:
    """Pick the ``/dev/disk/by-id`` entry matching *msc_filter*.

    The filter is matched case-insensitively as a glob when it contains glob
    metacharacters, otherwise as a substring. Whole-disk entries are preferred
    over ``*-partN`` partitions when both match (FAT MSC volumes are usually a
    bare disk). Returns the matched name, or ``None`` if nothing matches.
    """
    if not msc_filter:
        return None
    needle = msc_filter.lower()
    is_glob = any(c in msc_filter for c in "*?[")
    matches = [
        n
        for n in by_id_names
        if (fnmatch.fnmatch(n.lower(), needle) if is_glob else needle in n.lower())
    ]
    if not matches:
        return None
    # Prefer non-partition entries, then shortest name (most specific disk id).
    matches.sort(key=lambda n: ("-part" in n, len(n)))
    return matches[0]


def render_secrets_json(
    *,
    io_url: str,
    io_port: int,
    io_username: str = "",
    io_key: str = "",
    wifi_ssid: str = "",
    wifi_password: str = "",
) -> str:
    """Render the WipperSnapper-Arduino ``secrets.json`` body.

    Field names match ``examples/wippersnapper-arduino/secrets.example.json``
    (``io_url`` / ``io_port`` are the broker overrides). ``io_port`` is emitted
    as a JSON number. Only the WiFi block is included when an SSID is given.
    """
    body: dict[str, Any] = {
        "io_username": io_username,
        "io_key": io_key,
        "io_url": io_url,
        "io_port": int(io_port),
    }
    if wifi_ssid:
        body["network_type_wifi"] = {
            "network_ssid": wifi_ssid,
            "network_password": wifi_password,
        }
    return json.dumps(body, indent=2) + "\n"


# --------------------------------------------------------------------------- #
# Transport-driven flow                                                       #
# --------------------------------------------------------------------------- #


# Search by-path FIRST: a by-path name encodes the physical USB port
# (…usb-0:1.2:1.2-scsi-…), so it stays valid across re-enumeration / mode
# changes — unlike by-id, whose label can shift. by-id is kept as a fallback.
_DISK_DIRS = ("/dev/disk/by-path", "/dev/disk/by-id")


async def resolve_msc_device(
    transport: Any, msc_filter: str, *, dirs: tuple[str, ...] = _DISK_DIRS
) -> str:
    """Return the full ``/dev/disk/<by-path|by-id>/<name>`` path matching *msc_filter*.

    Searches by-path before by-id (stable across re-enumeration). Raises
    :class:`MscError` when the filter is empty or nothing matches (e.g. the
    volume hasn't enumerated yet — the FAT only appears once the app is running).
    """
    if not msc_filter:
        raise MscError(
            "no MSC filter set — assign one to the DUT on the usbip page or "
            "pass an msc_filter on the job"
        )
    seen: list[str] = []
    for d in dirs:
        res = await transport.exec(["ls", "-1", d])
        if getattr(res, "exit_status", 0) != 0:
            continue  # dir may not exist on a host with no matching devices
        names = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        seen += [f"{d}/{n}" for n in names]
        matched = select_block_device(names, msc_filter)
        if matched:
            return f"{d}/{matched}"
    raise MscError(
        f"no /dev/disk entry matched MSC filter {msc_filter!r} (seen: {', '.join(seen) or 'none'})"
    )


async def _mount_msc(transport: Any, dev: str, *, read_only: bool) -> tuple[str, str, list[str]]:
    """Mount the MSC volume, returning ``(device_used, mountpoint, unmount_argv)``.

    Tries ``udisksctl`` first (rootless where a polkit session exists), and falls
    back to ``sudo mount`` when it isn't authorized — the common case on Pi OS
    over SSH, where there's no login session so udisksctl returns
    ``NotAuthorized``. The sudo path resolves the symlink to the real
    ``/dev/sdX`` and mounts to a deterministic per-device mountpoint; read-write
    mounts are owned by the calling user so a plain ``tee`` can write.
    """
    # udisksctl only does rw mounts cleanly; skip it for a read-only mount.
    if not read_only:
        mount_res = await transport.exec(["udisksctl", "mount", "-b", dev])
        if getattr(mount_res, "exit_status", 0) == 0:
            mp = parse_udisks_mountpoint(mount_res.stdout or "")
            if mp:
                return dev, mp, ["udisksctl", "unmount", "-b", dev]
        udisks_err = (getattr(mount_res, "stderr", "") or "").strip()[:120]
    else:
        udisks_err = "skipped (read-only)"

    rl = await transport.exec(["readlink", "-f", dev])
    realdev = (getattr(rl, "stdout", "") or "").strip() or dev
    mnt = f"/tmp/hil-msc-{PurePosixPath(realdev).name}"
    await transport.exec(["bash", "-c", f"sudo mkdir -p {shlex.quote(mnt)}"])
    opts = "ro" if read_only else "rw,uid=$(id -u),gid=$(id -g)"
    mount_cmd = f"sudo mount -t vfat -o {opts} {shlex.quote(realdev)} {shlex.quote(mnt)}"
    m = await transport.exec(["bash", "-c", mount_cmd])
    if getattr(m, "exit_status", 0) != 0:
        raise MscError(
            f"mounting {realdev} failed (udisksctl: {udisks_err!r}; "
            f"sudo mount: {(m.stderr or '').strip()[:120]!r})"
        )
    return realdev, mnt, ["bash", "-c", f"sudo umount {shlex.quote(mnt)}"]


async def write_secrets_to_msc(
    transport: Any,
    *,
    msc_filter: str,
    secrets_json: str,
    filename: str = "secrets.json",
) -> tuple[str, str]:
    """Mount the matched MSC volume, write *secrets_json*, sync, unmount.

    Returns ``(device_path, mountpoint)``. Always unmounts in a ``finally`` so a
    mid-write failure never leaves the volume mounted.
    """
    dev = await resolve_msc_device(transport, msc_filter)
    device_used, mountpoint, unmount = await _mount_msc(transport, dev, read_only=False)
    try:
        dest = f"{mountpoint.rstrip('/')}/{filename}"
        write_res = await transport.exec(["tee", dest], stdin=secrets_json.encode())
        if getattr(write_res, "exit_status", 0) != 0:
            raise MscError(f"writing {dest} failed: {(write_res.stderr or '').strip()[:200]}")
        await transport.exec(["sync"])
        # Flush the DUT's TinyUSB MSC RAM cache to flash via a SCSI SYNCHRONIZE
        # CACHE (blockdev --flushbufs) — a plain unmount writes the data sectors
        # but doesn't fire the device's flush callback, so a following hard
        # power-cycle loses the write (board boots on the default secrets
        # template). A full SCSI eject/STOP flushes too but leaves the FATFS
        # unwritable on the next boot, so use the gentler cache-sync here.
        rl = await transport.exec(["readlink", "-f", dev])
        realdev = (getattr(rl, "stdout", "") or dev).strip() or dev
        await transport.exec(
            ["bash", "-c", f"sudo blockdev --flushbufs {shlex.quote(realdev)} && sync"],
            check=False,
        )
    finally:
        await transport.exec(unmount)
    log.info("wrote %s to MSC volume %s (%s)", filename, device_used, mountpoint)
    return device_used, mountpoint


async def read_msc_files(
    transport: Any,
    *,
    msc_filter: str,
    globs: tuple[str, ...] = ("*boot_out.txt",),
) -> dict[str, str]:
    """Mount the MSC volume read-only, return ``{path: contents}`` for *globs*.

    Used to surface a board's boot log — WipperSnapper's ``wipper_boot_out.txt``
    or CircuitPython's ``boot_out.txt`` (the default glob catches both, and the
    volume label varies: WIPPER / CIRCUITPY). Read-only so it never disturbs the
    running app's own view of the volume. Always unmounts in a ``finally``.
    """
    dev = await resolve_msc_device(transport, msc_filter)
    _, mnt, unmount = await _mount_msc(transport, dev, read_only=True)
    out: dict[str, str] = {}
    try:
        pattern = " ".join(f"{shlex.quote(mnt)}/{g}" for g in globs)
        # Print each matching file with a header; nullglob so a no-match is quiet.
        script = (
            f'shopt -s nullglob; for f in {pattern}; do echo "@@@FILE@@@ $f"; cat "$f"; echo; done'
        )
        res = await transport.exec(["bash", "-c", script])
        text = getattr(res, "stdout", "") or ""
        cur: str | None = None
        buf: list[str] = []
        for line in text.splitlines():
            if line.startswith("@@@FILE@@@ "):
                if cur is not None:
                    out[cur] = "\n".join(buf).strip()
                cur = line[len("@@@FILE@@@ ") :].strip()
                buf = []
            elif cur is not None:
                buf.append(line)
        if cur is not None:
            out[cur] = "\n".join(buf).strip()
    finally:
        await transport.exec(unmount)
    return out
