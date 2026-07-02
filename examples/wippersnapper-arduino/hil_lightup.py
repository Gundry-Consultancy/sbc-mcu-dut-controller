#!/usr/bin/env python3
"""Light the LilyGo panel via a no-flash HIL job and hold it lit for tuning.

Powers the (already-flashed) board, waits for a v2 check-in, injects the i8080
display Add + a text Write over /api/echo, then holds the board powered for
``--window-minutes`` so the camera can be swept live. No erase/flash/secrets.
"""
import argparse, json, os, sys, time, urllib.request

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7; out.append(b | (0x80 if n else 0))
        if not n: return bytes(out)

def _lenfield(field, payload):
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload

def display_write_signal(name, message):
    write = _lenfield(1, name.encode()) + _lenfield(3, message.encode())
    return _lenfield(36, _lenfield(3, write))

def _req(method, url, token, body=None, ctype="application/json", timeout=60.0):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None: req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="mcu-lilygo-tdisplay-s3-hil006")
    ap.add_argument("--uid", default="lilygo-t-display-s321621634")
    ap.add_argument("--message", default="LilyGo T-Display-S3\nWipperSnapper v2\ni8080 ST7789\nHIL camera proof OK")
    ap.add_argument("--window-minutes", type=int, default=25)
    ap.add_argument("--deadline-s", type=int, default=420)
    ap.add_argument("--no-power-cycle", action="store_true")
    args = ap.parse_args()

    base = (os.environ.get("HIL_API_BASE") or "http://192.168.1.169:8080").rstrip("/")
    token = os.environ.get("HIL_API_TOKEN") or "dev-token-change-me"

    display_params = {
        "name": "tft-19-i8080", "driver": "ST7789",
        "data_pins": ["D39","D40","D41","D42","D45","D46","D47","D48"],
        "cs": "D6", "dc": "D7", "rst": "D5",
        "width": 320, "height": 170, "rotation": 1, "text_size": 2, "status_bar": True,
    }
    inject = {"type": "inject_protobuf", "kind": "display_add_i8080", "settle_s": 5,
              "params": display_params, "uid": args.uid}
    write = {"type": "inject_protobuf", "settle_s": 5, "uid": args.uid,
             "payload_hex": display_write_signal("tft-19-i8080", args.message).hex()}
    stages = []
    if not args.no_power_cycle:
        stages.append({"type": "power_cycle"})
    stages += [
        {"type": "verify_checkin", "checkin_timeout_s": 150, "proto": "auto", "soft": True},
        inject, write,
    ]
    secrets = {k: os.environ.get(k, "hil") for k in ("IO_USERNAME", "IO_KEY")}
    secrets["WIFI_SSID"] = os.environ.get("WIFI_SSID", "bench-wifi")
    secrets["WIFI_PASSWORD"] = os.environ.get("WIFI_PASSWORD", "changeme")
    job = {"target": {"device": {"id": args.device}, "pool": "public"},
           "script": "firmware-bench",
           "params": {"window_minutes": args.window_minutes, "stages": stages},
           "secrets": secrets}
    st, body = _req("POST", f"{base}/v1/jobs", token, json.dumps(job).encode())
    job_id = json.loads(body)["id"]
    print(f"submitted job {job_id} (window {args.window_minutes}m)")

    since, deadline, state = 0, time.time() + args.deadline_s, "pending"
    verdicts = {}
    while time.time() < deadline:
        st, body = _req("GET", f"{base}/v1/jobs/{job_id}/wait?since={since}&timeout=20", token, timeout=40)
        d = json.loads(body); since = d.get("next_since", since)
        for e in d.get("events", []):
            p = e.get("payload", {})
            if p.get("stream") == "serial": continue
            m = str(p.get("msg", "") or "")
            for key in ("CHECKIN_VERDICT", "INJECT_VERDICT"):
                if key in m:
                    verdicts[key] = m.strip(); print(f"  {m.strip()[:180]}")
        state = d.get("state", state)
        # once both injects are seen, the panel is lit; keep job alive (window holds power)
        if state in ("finished", "failed", "cancelled", "error", "timeout"):
            break
        if verdicts.get("INJECT_VERDICT") and "CHECKIN_VERDICT" in verdicts:
            print(f"  panel lit; job {job_id} holding power for window."); break
    print(f"JOB_ID={job_id} state={state}")
    print(f"checkin_ok={'ok=true' in verdicts.get('CHECKIN_VERDICT','')}")

if __name__ == "__main__":
    sys.exit(main())
