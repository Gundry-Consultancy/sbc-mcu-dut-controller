"""TinyUF2 release fetcher — resolve + download Adafruit's tinyuf2 builds.

ESP32-S2/S3 (and to a lesser extent plain ESP32 / ESP32-C3) ship a UF2
bootloader from `adafruit/tinyuf2 <https://github.com/adafruit/tinyuf2>`_.
To install it, you erase the chip and write the matching ``combined.bin``
at offset 0x0 with esptool. This module finds the right release asset for
a given board (with chip-family fallback) and extracts ``combined.bin``
into a local cache.

The fetcher is pure data — it does not invoke esptool. Wire it together
with :class:`EsptoolFlasher` in a job builder:

    fetcher = TinyUf2Fetcher()
    fetched = await fetcher.fetch(board_name="feather_esp32s3_reverse_tft",
                                  fallback_board="feather_esp32s3")
    # Now ship fetched.path to the flash host and call:
    await esptool.erase()
    await esptool.flash(Artifact(path=str(remote_path),
                                  kind="combined_bin", offset=0,
                                  label=f"tinyuf2 {fetched.tag}"))
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

GITHUB_RELEASES_API = "https://api.github.com/repos/adafruit/tinyuf2/releases"


# --------------------------------------------------------------------------- #
# Dataclasses                                                                 #
# --------------------------------------------------------------------------- #


@dataclass
class TinyUf2Release:
    """One specific (tag, asset) pair resolved from the GH releases API."""

    tag: str
    asset_name: str
    asset_url: str
    asset_size: int


@dataclass
class TinyUf2Fetched:
    """The downloaded + extracted tinyuf2 ``combined.bin``."""

    path: Path
    tag: str
    asset_name: str
    digest_sha256: str
    raw_size: int


# --------------------------------------------------------------------------- #
# Pure parsers (unit-testable without network)                                #
# --------------------------------------------------------------------------- #


def asset_name_prefix(board_name: str) -> str:
    """Prefix every tinyuf2 asset zip for *board_name* shares.

    Releases are named ``tinyuf2-<board>-<version>.zip``, so any asset
    starting with ``tinyuf2-<board>-`` and ending in ``.zip`` belongs to
    that board.
    """
    return f"tinyuf2-{board_name}-"


def find_asset(
    assets: list[dict[str, Any]],
    board_name: str,
    *,
    fallback_board: str | None = None,
) -> dict[str, Any] | None:
    """Pick the best matching asset dict from a GH releases response.

    Match order: exact ``board_name`` zip, then ``fallback_board`` zip.
    Returns ``None`` when neither matches.
    """
    primary = asset_name_prefix(board_name)
    for asset in assets:
        name = asset.get("name", "")
        if name.startswith(primary) and name.endswith(".zip"):
            return asset
    if fallback_board:
        secondary = asset_name_prefix(fallback_board)
        for asset in assets:
            name = asset.get("name", "")
            if name.startswith(secondary) and name.endswith(".zip"):
                return asset
    return None


def extract_combined_bin(zip_bytes: bytes) -> bytes:
    """Pull ``combined.bin`` out of a tinyuf2 release zip.

    Releases nest the file under a per-board directory; we match either
    a top-level ``combined.bin`` or any path ending in ``/combined.bin``
    (case-insensitive).
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            lower = name.lower()
            if lower == "combined.bin" or lower.endswith("/combined.bin"):
                return zf.read(name)
    raise FileNotFoundError(
        "combined.bin not found in tinyuf2 release zip "
        f"(entries sample: {zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()[:5]})"
    )


# --------------------------------------------------------------------------- #
# Async fetcher                                                               #
# --------------------------------------------------------------------------- #


class TinyUf2Fetcher:
    """Resolve a tinyuf2 release for a board and extract its ``combined.bin``.

    The fetcher caches the extracted bin under ``cache_dir`` keyed by
    ``(board, tag)``. Pass an existing :class:`httpx.AsyncClient` to share
    auth headers / proxies / TLS config with other HTTP calls; default is
    a fresh transient client per request.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        cache_dir: Path | None = None,
        github_api_base: str = GITHUB_RELEASES_API,
    ) -> None:
        self._client = http_client
        self.cache_dir = cache_dir or Path("/tmp/hil/tinyuf2-cache")
        self._api_base = github_api_base

    async def _http_get(self, url: str) -> httpx.Response:
        if self._client is not None:
            resp = await self._client.get(url, follow_redirects=True)
        else:
            async with httpx.AsyncClient() as c:
                resp = await c.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp

    async def resolve(
        self,
        *,
        board_name: str,
        tag: str = "latest",
        fallback_board: str | None = None,
    ) -> TinyUf2Release:
        """Resolve the GH release + asset for *board_name* without downloading."""
        if tag == "latest":
            url = f"{self._api_base}/latest"
        else:
            url = f"{self._api_base}/tags/{tag}"
        data = (await self._http_get(url)).json()
        asset = find_asset(
            data.get("assets", []),
            board_name,
            fallback_board=fallback_board,
        )
        if asset is None:
            tried = [board_name] + ([fallback_board] if fallback_board else [])
            raise FileNotFoundError(
                f"No tinyuf2 release asset matches {tried!r} in tag={data.get('tag_name')!r}"
            )
        return TinyUf2Release(
            tag=data["tag_name"],
            asset_name=asset["name"],
            asset_url=asset["browser_download_url"],
            asset_size=int(asset.get("size", 0)),
        )

    async def fetch(
        self,
        *,
        board_name: str,
        tag: str = "latest",
        fallback_board: str | None = None,
    ) -> TinyUf2Fetched:
        """Download, extract, cache, and return the ``combined.bin``."""
        release = await self.resolve(board_name=board_name, tag=tag, fallback_board=fallback_board)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        local_path = self.cache_dir / f"tinyuf2-{board_name}-{release.tag}-combined.bin"
        if local_path.exists():
            data = local_path.read_bytes()
        else:
            resp = await self._http_get(release.asset_url)
            data = extract_combined_bin(resp.content)
            local_path.write_bytes(data)
        return TinyUf2Fetched(
            path=local_path,
            tag=release.tag,
            asset_name=release.asset_name,
            digest_sha256=hashlib.sha256(data).hexdigest(),
            raw_size=len(data),
        )
