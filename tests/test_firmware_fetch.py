"""Tests for firmware resolution (path / url / upload)."""

from __future__ import annotations

import hashlib

import pytest

import hil_controller.adapters.firmware_fetch as mod
from hil_controller.adapters.firmware_fetch import (
    FirmwareFetchError,
    download_to,
    resolve_firmware_local,
    store_uploaded_firmware,
)

FW = b"\x1a\x09combined-firmware-bytes"
FW_SHA = hashlib.sha256(FW).hexdigest()


class _FakeResp:
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, n=0):
        for c in self._chunks:
            yield c


class _FakeStreamCtx:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return _FakeResp(self._chunks)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None):
        return _FakeStreamCtx([FW])


def test_store_uploaded_firmware(tmp_path):
    rec = store_uploaded_firmware(FW, store_dir=tmp_path / "fw", filename="x.combined.bin")
    assert rec["size_bytes"] == len(FW)
    assert rec["sha256"] == FW_SHA
    assert rec["filename"] == "x.combined.bin"
    from pathlib import Path

    assert Path(rec["path"]).read_bytes() == FW


def test_store_uploaded_firmware_sanitizes_filename(tmp_path):
    rec = store_uploaded_firmware(FW, store_dir=tmp_path / "fw", filename="../../etc/evil")
    assert "/" not in rec["filename"] and "\\" not in rec["filename"]


@pytest.mark.asyncio
async def test_download_to_verifies_sha256(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)
    dest = tmp_path / "fw.bin"
    out = await download_to("https://x/fw.bin", dest, sha256=FW_SHA)
    from pathlib import Path

    assert Path(out).read_bytes() == FW


@pytest.mark.asyncio
async def test_download_to_sha256_mismatch_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)
    with pytest.raises(FirmwareFetchError, match="sha256 mismatch"):
        await download_to("https://x/fw.bin", tmp_path / "fw.bin", sha256="deadbeef")


@pytest.mark.asyncio
async def test_resolve_path_exists(tmp_path):
    p = tmp_path / "local.bin"
    p.write_bytes(FW)
    assert await resolve_firmware_local({"path": str(p)}, dest_dir=tmp_path) == str(p)


@pytest.mark.asyncio
async def test_resolve_path_missing_raises(tmp_path):
    with pytest.raises(FirmwareFetchError, match="does not exist"):
        await resolve_firmware_local({"path": str(tmp_path / "nope.bin")}, dest_dir=tmp_path)


@pytest.mark.asyncio
async def test_resolve_url_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)
    out = await resolve_firmware_local(
        {"url": "https://x/wippersnapper.qtpy_esp32s3_n4r2.fatfs.combined.bin", "sha256": FW_SHA},
        dest_dir=tmp_path,
    )
    from pathlib import Path

    assert Path(out).read_bytes() == FW


@pytest.mark.asyncio
async def test_resolve_neither_raises(tmp_path):
    with pytest.raises(FirmwareFetchError, match="path or .url"):
        await resolve_firmware_local({}, dest_dir=tmp_path)
