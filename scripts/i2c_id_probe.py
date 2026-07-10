#!/usr/bin/env python3
"""Read I2C device ID / PART_ID registers over a CircuitPython REPL, to
disambiguate parts that a bare address scan can't tell apart.

This is a **manual bring-up probe**, promoted from bench scratch. It drives a
DUT running CircuitPython over its USB serial: it pushes a code block into the
raw REPL, reads each part's identity register, and prints the result to a
``PROBE_DONE`` sentinel. Useful when standing up a new sensor string and you
need to confirm which part sits at an ambiguous address (e.g. TMP117 vs TMP119,
ENS160 vs ENS161).

The ``JOBS`` table below is an **example** for one specific QT Py ESP32-S3
strand (a TCA9548A @ 0x77 with a known sensor layout); edit it for your parts.
It doubles as a small reference of id-register recipes (register, byte count,
16-bit "cmd2" addressing, expected value).

Note on muxing: the on-DUT ``TCA9548A`` here is that board's *own* sensor mux.
For sensors **shared between DUTs**, the platform routes a whole strand to one
DUT at a time via the controller's **ADG729 analog strand-mux** (see the
``hil-i2c-strands`` skill and the ``select_i2c_strand`` bench stage), and you'd
normally read the sensors through the ``inject_i2c_probe`` / ``inject_i2c_settings``
stages rather than this REPL probe. Use this when poking a board by hand.

Usage: i2c_id_probe.py [/dev/ttyACM0]
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"

PROBE = r'''
import board, time
i2c = board.STEMMA_I2C()
MUX = 0x77
def lock():
    t=0
    while not i2c.try_lock():
        time.sleep(0.01); t+=1
        if t>300: return False
    return True
def sel(ch):
    if not lock(): return False
    try:
        i2c.writeto(MUX, bytes([1<<ch] if ch is not None else [0]))
    finally:
        i2c.unlock()
    time.sleep(0.01); return True
def rd(addr, reg, n, cmd2=False):
    if not lock(): return "lockfail"
    try:
        try:
            buf = bytearray(n)
            if cmd2:
                i2c.writeto(addr, bytes([reg>>8, reg&0xFF]))
                time.sleep(0.03)
                i2c.readfrom_into(addr, buf)
            else:
                i2c.writeto_then_readfrom(addr, bytes([reg]), buf)
            return " ".join("%02x"%b for b in buf)
        except Exception as e:
            return "ERR %r"%e
    finally:
        i2c.unlock()
# (channel, addr, label, [(reg, n, cmd2, note)...])  -- edit for your parts
JOBS = [
 (None, 0x12, "direct 0x12", []),                         # PMSA003I (no simple id)
 (None, 0x58, "direct 0x58", [(0x3682,3,True,"SGP30 serial")]),
 (0, 0x52, "ch0 0x52", [(0x00,2,False,"ENS160 PART_ID=60 01? else APDS9999")]),
 (0, 0x59, "ch0 0x59", [(0x202F,3,True,"Sensirion featureset SGP40/41")]),
 (0, 0x61, "ch0 0x61", [(0xD100,3,True,"SCD30 fw ver")]),
 (1, 0x48, "ch1 0x48", [(0x0F,2,False,"TMP117=01 17 / TMP119=21 17"),(0x0B,1,False,"ADT7410 id=cb")]),
 (1, 0x53, "ch1 0x53", [(0x00,2,False,"ENS161 PART_ID=61 01?"),(0x06,1,False,"LTR390 PART_ID=b2?")]),
]
for ch, addr, label, regs in JOBS:
    sel(ch)
    print("PROBE", label, "ch=%s"%ch)
    if not regs:
        print("   (no id register; address-based guess only)")
    for reg, n, cmd2, note in regs:
        v = rd(addr, reg, n, cmd2)
        print("   reg=0x%02x -> [%s]  (%s)" % (reg, v, note))
sel(None)
print("PROBE_DONE")
'''


def main():
    s = serial.Serial(PORT, 115200, timeout=0.3)
    time.sleep(0.4)
    s.write(b"\x03\x03"); time.sleep(0.4); s.reset_input_buffer()  # Ctrl-C x2: interrupt
    s.write(b"\x01"); time.sleep(0.3); s.read(4096)                # Ctrl-A: raw REPL
    s.write(PROBE.encode("utf-8") + b"\x04")                       # code + Ctrl-D: run
    buf = b""; t0 = time.time()
    while time.time() - t0 < 35:
        d = s.read(4096)
        if d:
            buf += d
        if b"PROBE_DONE" in buf and buf.count(b"\x04") >= 2:
            break
    s.write(b"\x02"); s.close()                                    # Ctrl-B: friendly REPL
    print(buf.decode("utf-8", "replace").replace("\x04", "<EOT>"))


if __name__ == "__main__":
    main()
