"""Resolve a firmware image to a controller-local path for firmware-bench.

firmware-bench copies ``params.firmware.path`` (a path ON THE CONTROLLER) to the
bench. CI can supply that file three ways, all resolving to a local path here:

* ``path``  — already on the controller (legacy / pre-staged).
* ``url``   — the controller downloads it (public release assets; optional
              ``sha256`` verification, optional bearer token for private URLs).
* uploaded  — via ``POST /v1/firmware`` (see api/firmware.py), which stores the
              bytes through :func:`store_uploaded_firmware` and returns a path
              the job then passes as ``params.firmware.path``.

Kept dependency-light (httpx, hashlib) and side-effect-narrow so it's testable.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

#: Default glob for the firmware member inside a zip when ``firmware.member`` is
#: unset. A combined image flashes cleanly at 0x0; override ``member`` + ``offset``
#: for app-only bins (e.g. ``*qtpy_esp32s3_n4r2*.bin`` flashed at 0x10000).
DEFAULT_ZIP_MEMBER = "*combined.bin"


class FirmwareFetchError(RuntimeError):
    """Firmware could not be resolved to a local file."""


def _sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def download_to(
    url: str,
    dest: str | Path,
    *,
    sha256: str | None = None,
    token: str | None = None,
    timeout: float = 120.0,
) -> str:
    """Download ``url`` to ``dest`` (streaming), optionally verifying ``sha256``.

    A ``token`` is sent as ``Authorization: Bearer`` for private asset URLs.
    Raises :class:`FirmwareFetchError` on HTTP error or digest mismatch.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            async with http.stream("GET", url, headers=headers) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
    except httpx.HTTPError as exc:
        raise FirmwareFetchError(f"download failed for {url}: {exc}") from exc
    if sha256:
        got = _sha256_file(dest)
        if got.lower() != sha256.lower():
            raise FirmwareFetchError(f"sha256 mismatch for {url}: expected {sha256}, got {got}")
    return str(dest)


def _is_zip_source(url: str, fw: dict) -> bool:
    """True if ``fw.url`` should be treated as a zip we extract a member from.

    A ``member`` key forces zip handling; otherwise the URL shape decides
    (``...zip`` or a GitHub Actions artifact ``.../artifacts/<id>/zip``)."""
    if fw.get("member"):
        return True
    u = url.rstrip("/").lower()
    return u.endswith(".zip") or u.endswith("/zip")


async def _extract_zip_member(
    url: str,
    *,
    dest_dir: str | Path,
    member: str,
    token: str | None,
    sha256: str | None,
    log_fn: Callable[[str], None],
) -> str:
    """Download a zip (``url``) and extract the single member matching *member*.

    Everything is logged via *log_fn* (no hidden steps): the URL, the downloaded
    zip's own sha256 (informational — GitHub repackages artifact zips so this is
    NOT stable across downloads), the full member list, the chosen member, and
    its size + sha256. ``sha256`` (when given) verifies the EXTRACTED MEMBER (the
    firmware bytes — stable), not the zip."""
    zip_dest = Path(dest_dir) / f"artifact-{uuid.uuid4().hex}.zip"
    log_fn(f"firmware: downloading zip {url} (token={'yes' if token else 'no'})")
    await download_to(url, zip_dest, token=token)
    zip_sha = _sha256_file(zip_dest)
    log_fn(f"firmware: downloaded zip {zip_dest.name} ({zip_dest.stat().st_size} bytes) sha256={zip_sha}")
    with zipfile.ZipFile(zip_dest) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        log_fn(f"firmware: zip contains {len(names)} member(s); selecting with glob {member!r}")
        matches = sorted(
            n
            for n in names
            if fnmatch.fnmatch(n, member) or fnmatch.fnmatch(os.path.basename(n), member)
        )
        if not matches:
            raise FirmwareFetchError(
                f"no zip member matching {member!r} in {url}; members={names}"
            )
        if len(matches) > 1:
            log_fn(f"firmware: WARNING {len(matches)} members match {member!r}: {matches}; using {matches[0]}")
        chosen = matches[0]
        data = zf.read(chosen)
    out = Path(dest_dir) / os.path.basename(chosen)
    out.write_bytes(data)
    member_sha = hashlib.sha256(data).hexdigest()
    log_fn(f"firmware: extracted member {chosen!r} → {out.name} ({len(data)} bytes) sha256={member_sha}")
    if sha256:
        if member_sha.lower() != sha256.lower():
            raise FirmwareFetchError(
                f"sha256 mismatch for member {chosen!r}: expected {sha256}, got {member_sha}"
            )
        log_fn(f"firmware: member sha256 verified == expected {sha256}")
    else:
        log_fn("firmware: no expected sha256 supplied — member hash logged but NOT verified")
    try:
        zip_dest.unlink()
    except OSError:
        pass
    return str(out)


async def resolve_firmware_local(
    fw: dict,
    *,
    dest_dir: str | Path,
    token: str | None = None,
    on_line: Callable[[str], None] | None = None,
) -> str:
    """Return a controller-local path for the ``firmware`` param dict.

    ``fw`` is ``params.firmware``. Accepted shapes (logged via *on_line*):

    * ``{"path": ...}`` — already on the controller (returned as-is).
    * ``{"url": ..., "sha256"?: ...}`` — a direct file download (``sha256``
      verifies the downloaded file).
    * ``{"url": <zip>, "member"?: <glob>, "sha256"?: ...}`` — a zip (a GitHub
      Actions ``.../artifacts/<id>/zip`` or any ``*.zip``): the zip is downloaded
      (with ``token`` as ``Authorization: Bearer`` for private/artifact URLs),
      the member matching ``member`` (default ``*combined.bin``) is extracted, and
      ``sha256`` (when given) verifies the EXTRACTED member. The flash ``offset``
      lives alongside (``firmware.offset``, default 0x0) and is honoured by the
      flash stage.

    Every download/extract/verify step is logged through *on_line* so nothing is
    hidden. Raises :class:`FirmwareFetchError` if nothing is usable."""
    log_fn = on_line or (lambda _m: None)
    path = fw.get("path")
    if path:
        if not os.path.isfile(path):
            raise FirmwareFetchError(f"firmware path does not exist on controller: {path}")
        log_fn(f"firmware: using pre-staged path {path}")
        return str(path)
    url = fw.get("url")
    if url:
        if _is_zip_source(url, fw):
            member = fw.get("member") or DEFAULT_ZIP_MEMBER
            if not fw.get("member"):
                log_fn(f"firmware: no member glob set, defaulting to {DEFAULT_ZIP_MEMBER!r}")
            return await _extract_zip_member(
                url,
                dest_dir=dest_dir,
                member=member,
                token=token,
                sha256=fw.get("sha256"),
                log_fn=log_fn,
            )
        name = url.rstrip("/").rsplit("/", 1)[-1] or f"firmware-{uuid.uuid4().hex}.bin"
        dest = Path(dest_dir) / name
        log_fn(f"firmware: downloading {url} → {name} (token={'yes' if token else 'no'})")
        out = await download_to(url, dest, sha256=fw.get("sha256"), token=token)
        log_fn(
            f"firmware: downloaded {name} ({Path(out).stat().st_size} bytes) "
            f"sha256={_sha256_file(out)}"
            + (" (verified)" if fw.get("sha256") else " (not verified)")
        )
        return out
    raise FirmwareFetchError(
        "firmware-bench: provide params.firmware.path or .url (or upload via POST /v1/firmware)"
    )


def store_uploaded_firmware(
    data: bytes, *, store_dir: str | Path, filename: str | None = None
) -> dict:
    """Persist uploaded firmware bytes; return an asset dict (id/filename/path/size_bytes/sha256).

    Backs ``POST /v1/firmware`` — the returned ``path`` is what a job passes as
    ``params.firmware.path``.
    """
    store = Path(store_dir)
    store.mkdir(parents=True, exist_ok=True)
    fid = uuid.uuid4().hex
    safe = (os.path.basename(filename) if filename else "") or "firmware.bin"
    dest = store / f"{fid}-{safe}"
    dest.write_bytes(data)
    return {
        "id": fid,
        "filename": safe,
        "path": str(dest),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
