#!/usr/bin/env python3
"""Convert an ESP32-S3 UF2 (full image, targetAddr from 0x0) to a flat .bin.

The WipperSnapper v2 CI artifact ships a .uf2 (bootloader@0x0 + partitions@0x8000
+ boot_app0@0xe000 + app@0x10000) but no combined .bin. esptool can't flash a
.uf2, so convert it to a contiguous bin starting at the lowest targetAddr (0x0)
with inter-region gaps padded 0xFF -- flashable at 0x0.

Usage: uf2_to_bin.py in.uf2 out.bin
"""
import struct
import sys

UF2_MAGIC0 = 0x0A324655
UF2_MAGIC1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
BLOCK = 512


def convert(src: str, dst: str) -> None:
    data = open(src, "rb").read()
    if len(data) % BLOCK != 0:
        raise SystemExit(f"not a UF2 (size {len(data)} not a multiple of 512)")
    chunks = []  # (addr, bytes)
    for off in range(0, len(data), BLOCK):
        blk = data[off : off + BLOCK]
        m0, m1, flags, addr, size, blkno, numblk, fam = struct.unpack("<IIIIIIII", blk[:32])
        if m0 != UF2_MAGIC0 or m1 != UF2_MAGIC1:
            raise SystemExit(f"bad UF2 magic at block {off // BLOCK}")
        if struct.unpack("<I", blk[508:512])[0] != UF2_MAGIC_END:
            raise SystemExit(f"bad UF2 end magic at block {off // BLOCK}")
        chunks.append((addr, blk[32 : 32 + size]))
    base = min(a for a, _ in chunks)
    end = max(a + len(b) for a, b in chunks)
    out = bytearray(b"\xff" * (end - base))
    for addr, payload in chunks:
        out[addr - base : addr - base + len(payload)] = payload
    open(dst, "wb").write(out)
    print(f"base=0x{base:x} end=0x{end:x} size={len(out)} -> {dst}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    convert(sys.argv[1], sys.argv[2])
