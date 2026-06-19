---
name: project-deployment
description: Controller runs on Tachyon at 192.168.1.169 (particle user); live DB state confirmed 2026-05-25
metadata:
  type: project
---

**Controller host:** Particle Tachyon at 192.168.1.169, user `particle`. Ubuntu, SSH on port 22.
- SSH works using Windows OpenSSH (`C:\Windows\System32\OpenSSH\ssh.exe`) with the ed25519 key loaded in the Windows SSH agent. Git Bash SSH cannot reach the agent due to device/pipe mismatch.
- DB at `/home/particle/dev-projects/python/usbip-hil-controller/run/jobs.db`
- ProtoMQ broker running locally (`tachyon-protomq` host, `127.0.0.1`)

**Live DB state (2026-05-25):**
- Hosts: `localhost` (sbc-fleet, LocalTransport), `tachyon-protomq` (protomq-broker, 127.0.0.1), `rpi-hil001`–`rpi-hil006`, `rpi-displays`
- Devices: `tachyon-runner-a` (sbc, particle-tachyon, host=localhost)
- Auxes: `tachyon-ili9341` (display, ili9341-240x320), `android-note9` (camera, IP Webcam Android)
- Connections: `tachyon-runner-a` → `tachyon-ili9341`
- Camera `android-note9`: `http://192.168.1.249:8080/shot.jpg` (snapshot interface, available)

**HIL hosts:**
- `rpi-displays` — microcontroller DUT fleet, MCP23017 solenoid hub, Genesys USB hub. CSI camera attached (not yet in DB as aux).
- `rpi-hil001`–`rpi-hil006` — SBC DUT fleet
- ProtoMQ broker local on Tachyon (not pi5-protomq as in original architecture)

**Camera topology:**
- `android-note9` — IP Webcam app on Android Note 9, `http://192.168.1.249:8080/shot.jpg`, covers rpi-hil00x displays and Tachyon. Snapshot URL (single JPEG), not MJPEG stream. Already registered in DB.
- CSI camera on rpi-displays — not yet in DB; covers microcontroller-fleet DUT displays.
- Future: additional cameras splitting DUTs per camera.

**Why:** Confirmed by querying live DB and user context.
**How to apply:** Use Windows OpenSSH for Tachyon access. Camera IP is 192.168.1.249:8080. Only one device (`tachyon-runner-a`) currently registered, connected to display but not camera aux yet.
