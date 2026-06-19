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

import hashlib
import logging
import os
import uuid
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


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


async def resolve_firmware_local(
    fw: dict, *, dest_dir: str | Path, token: str | None = None
) -> str:
    """Return a controller-local path for the ``firmware`` param dict.

    ``fw`` is ``params.firmware`` — accepts ``{"path": ...}`` (returned as-is) or
    ``{"url": ..., "sha256"?: ...}`` (downloaded into ``dest_dir``). Raises if
    neither is usable.
    """
    path = fw.get("path")
    if path:
        if not os.path.isfile(path):
            raise FirmwareFetchError(f"firmware path does not exist on controller: {path}")
        return str(path)
    url = fw.get("url")
    if url:
        name = url.rstrip("/").rsplit("/", 1)[-1] or f"firmware-{uuid.uuid4().hex}.bin"
        dest = Path(dest_dir) / name
        return await download_to(url, dest, sha256=fw.get("sha256"), token=token)
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
