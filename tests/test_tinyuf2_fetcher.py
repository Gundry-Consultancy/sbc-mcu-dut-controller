"""TinyUF2 release fetcher tests (M3.5).

Pure-parser tests run without any network. Async fetcher tests use respx
to stub the GitHub Releases API and the asset download.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from hil_controller.adapters.tinyuf2_fetcher import (
    GITHUB_RELEASES_API,
    TinyUf2Fetcher,
    asset_name_prefix,
    extract_combined_bin,
    find_asset,
)

# --------------------------------------------------------------------------- #
# Pure parsers                                                                #
# --------------------------------------------------------------------------- #


def test_asset_name_prefix() -> None:
    assert asset_name_prefix("feather_esp32s3_reverse_tft") == (
        "tinyuf2-feather_esp32s3_reverse_tft-"
    )


def test_find_asset_exact_match() -> None:
    assets = [
        {"name": "tinyuf2-feather_esp32s3-0.21.0.zip", "browser_download_url": "x"},
        {"name": "tinyuf2-metro_esp32s2-0.21.0.zip", "browser_download_url": "y"},
    ]
    a = find_asset(assets, "metro_esp32s2")
    assert a is not None and a["name"].startswith("tinyuf2-metro_esp32s2-")


def test_find_asset_returns_none_when_no_match() -> None:
    assets = [{"name": "tinyuf2-feather_esp32s3-0.21.0.zip"}]
    assert find_asset(assets, "nrf52840_dk") is None


def test_find_asset_falls_back_to_chip_family_board() -> None:
    assets = [
        {"name": "tinyuf2-feather_esp32s3-0.21.0.zip"},  # generic ESP32-S3 build
        {"name": "tinyuf2-metro_esp32s2-0.21.0.zip"},
    ]
    # exact "feather_esp32s3_reverse_tft" not present → fall back to "feather_esp32s3"
    a = find_asset(
        assets,
        "feather_esp32s3_reverse_tft",
        fallback_board="feather_esp32s3",
    )
    assert a is not None
    assert a["name"] == "tinyuf2-feather_esp32s3-0.21.0.zip"


def test_find_asset_ignores_non_zip_assets() -> None:
    assets = [
        {"name": "tinyuf2-feather_esp32s3-0.21.0.tar.gz"},
        {"name": "tinyuf2-feather_esp32s3-0.21.0.zip"},
    ]
    a = find_asset(assets, "feather_esp32s3")
    assert a is not None and a["name"].endswith(".zip")


def _make_zip(contents: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in contents.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_combined_bin_top_level() -> None:
    zip_bytes = _make_zip({"combined.bin": b"\xde\xad\xbe\xef"})
    assert extract_combined_bin(zip_bytes) == b"\xde\xad\xbe\xef"


def test_extract_combined_bin_nested_under_board_dir() -> None:
    zip_bytes = _make_zip(
        {
            "feather_esp32s3/README.md": b"# tinyuf2",
            "feather_esp32s3/combined.bin": b"\x01\x02\x03\x04",
        }
    )
    assert extract_combined_bin(zip_bytes) == b"\x01\x02\x03\x04"


def test_extract_combined_bin_raises_when_missing() -> None:
    zip_bytes = _make_zip({"README.md": b"no combined here"})
    with pytest.raises(FileNotFoundError, match="combined.bin"):
        extract_combined_bin(zip_bytes)


# --------------------------------------------------------------------------- #
# Async fetcher (respx-stubbed)                                               #
# --------------------------------------------------------------------------- #


def _release_json(tag: str, assets: list[dict[str, object]]) -> dict[str, object]:
    return {"tag_name": tag, "assets": assets}


@pytest.mark.asyncio
async def test_resolve_latest_picks_matching_board(tmp_path: Path) -> None:
    fetcher = TinyUf2Fetcher(cache_dir=tmp_path)
    with respx.mock(base_url="https://api.github.com") as r:
        r.get("/repos/adafruit/tinyuf2/releases/latest").mock(
            return_value=httpx.Response(
                200,
                json=_release_json(
                    "0.22.0",
                    [
                        {
                            "name": "tinyuf2-feather_esp32s3-0.22.0.zip",
                            "browser_download_url": "https://example.com/a.zip",
                            "size": 1234,
                        },
                        {
                            "name": "tinyuf2-metro_esp32s2-0.22.0.zip",
                            "browser_download_url": "https://example.com/b.zip",
                            "size": 5678,
                        },
                    ],
                ),
            )
        )
        release = await fetcher.resolve(board_name="metro_esp32s2")
    assert release.tag == "0.22.0"
    assert release.asset_name == "tinyuf2-metro_esp32s2-0.22.0.zip"
    assert release.asset_url == "https://example.com/b.zip"
    assert release.asset_size == 5678


@pytest.mark.asyncio
async def test_resolve_by_explicit_tag(tmp_path: Path) -> None:
    fetcher = TinyUf2Fetcher(cache_dir=tmp_path)
    with respx.mock(base_url="https://api.github.com") as r:
        r.get("/repos/adafruit/tinyuf2/releases/tags/0.20.0").mock(
            return_value=httpx.Response(
                200,
                json=_release_json(
                    "0.20.0",
                    [
                        {
                            "name": "tinyuf2-feather_esp32s3-0.20.0.zip",
                            "browser_download_url": "https://example.com/old.zip",
                            "size": 9999,
                        }
                    ],
                ),
            )
        )
        release = await fetcher.resolve(board_name="feather_esp32s3", tag="0.20.0")
    assert release.tag == "0.20.0"
    assert release.asset_url == "https://example.com/old.zip"


@pytest.mark.asyncio
async def test_resolve_raises_when_no_match_after_fallback(tmp_path: Path) -> None:
    fetcher = TinyUf2Fetcher(cache_dir=tmp_path)
    with respx.mock(base_url="https://api.github.com") as r:
        r.get("/repos/adafruit/tinyuf2/releases/latest").mock(
            return_value=httpx.Response(
                200,
                json=_release_json(
                    "0.22.0",
                    [{"name": "tinyuf2-metro_esp32s2-0.22.0.zip"}],
                ),
            )
        )
        with pytest.raises(FileNotFoundError, match="No tinyuf2 release asset"):
            await fetcher.resolve(board_name="nrf52840_dk", fallback_board="nrf52840")


@pytest.mark.asyncio
async def test_fetch_downloads_extracts_and_caches(tmp_path: Path) -> None:
    fetcher = TinyUf2Fetcher(cache_dir=tmp_path)
    zip_bytes = _make_zip({"feather_esp32s3/combined.bin": b"\x55" * 64})

    with (
        respx.mock(base_url="https://api.github.com") as r1,
        respx.mock(base_url="https://example.com") as r2,
    ):
        r1.get("/repos/adafruit/tinyuf2/releases/latest").mock(
            return_value=httpx.Response(
                200,
                json=_release_json(
                    "0.22.0",
                    [
                        {
                            "name": "tinyuf2-feather_esp32s3-0.22.0.zip",
                            "browser_download_url": "https://example.com/a.zip",
                            "size": len(zip_bytes),
                        }
                    ],
                ),
            )
        )
        r2.get("/a.zip").mock(return_value=httpx.Response(200, content=zip_bytes))
        fetched = await fetcher.fetch(board_name="feather_esp32s3")

    assert fetched.tag == "0.22.0"
    assert fetched.asset_name == "tinyuf2-feather_esp32s3-0.22.0.zip"
    assert fetched.raw_size == 64
    # Digest is the SHA-256 of 64 bytes of 0x55.
    expected_path = tmp_path / "tinyuf2-feather_esp32s3-0.22.0-combined.bin"
    assert fetched.path == expected_path
    assert fetched.path.read_bytes() == b"\x55" * 64


@pytest.mark.asyncio
async def test_fetch_uses_cache_on_second_call(tmp_path: Path) -> None:
    fetcher = TinyUf2Fetcher(cache_dir=tmp_path)
    # Pre-seed the cache so the fetch never has to download.
    cached_path = tmp_path / "tinyuf2-feather_esp32s3-0.22.0-combined.bin"
    cached_path.write_bytes(b"\xaa" * 32)

    with respx.mock(base_url="https://api.github.com") as r1:
        r1.get("/repos/adafruit/tinyuf2/releases/latest").mock(
            return_value=httpx.Response(
                200,
                json=_release_json(
                    "0.22.0",
                    [
                        {
                            "name": "tinyuf2-feather_esp32s3-0.22.0.zip",
                            "browser_download_url": "https://example.com/never-called.zip",
                            "size": 0,
                        }
                    ],
                ),
            )
        )
        # No respx mock for the asset URL — would error if hit.
        fetched = await fetcher.fetch(board_name="feather_esp32s3")

    assert fetched.path == cached_path
    assert fetched.raw_size == 32


def test_github_releases_constant_points_to_adafruit_tinyuf2() -> None:
    # Sanity: nobody quietly retargeted the URL.
    assert "adafruit/tinyuf2" in GITHUB_RELEASES_API
    assert GITHUB_RELEASES_API.startswith("https://api.github.com")
