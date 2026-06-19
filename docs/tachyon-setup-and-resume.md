# Tachyon HIL Setup — State & Resume Guide

## Machine

| Field | Value |
|-------|-------|
| Host | `192.168.1.169` |
| User | `particle` (sudo, gpio, i2c, plugdev, dialout groups) |
| OS | Ubuntu 24.04, aarch64, kernel `6.8.0-1058-particle` |
| Board | Particle Tachyon (Quectel QCM6490) |
| Attached DUT hardware | Adafruit EYESPI Pi Beret + ILI9341 2.2" 240×320 SPI TFT |
| SSH key | `~/.ssh/id_ed25519` (must be in agent: `ssh-add ~/.ssh/id_ed25519`) |

---

## Current State (as of 2026-05-22)

### Test results

| Suite | Command | Result |
|---|---|---|
| Non-hardware (connection/integration) | `-m 'not hardware'` | **66 passed, 2 failed, 19 skipped** |
| ILI9341 real hardware | `WS_REAL_DISPLAY_TEST=1 … tachyon_webcam` | **PASSED** |

The 2 failures are pre-existing unit test bugs in `base_displayio_epd_test.py` (EPD display group assertions), unrelated to HIL or Tachyon setup.

### `.env` (Wippersnapper Python repo root)

```
PROTOMQ_PATH=/home/particle/dev-projects/python/Adafruit_Wippersnapper_Python/tools/protomq
PROTOMQ_RUN_EXTERNALLY=False
PROTOMQ_HOST=localhost
PROTOMQ_PORT=1884
BLINKA_OS_AGNOSTIC=1
```

`BLINKA_OS_AGNOSTIC=1` is required for non-hardware tests — it makes Blinka use mock pin objects so `I2CBus.configure_bus()` doesn't crash when the test suite runs without real I2C devices connected. Remove it (or override with `BLINKA_OS_AGNOSTIC=0`) when running hardware tests.

### Blinka version

A patched Blinka is installed that correctly maps GPIO chip 4 on the Tachyon:

```
adafruit-blinka @ git+https://github.com/tyeth-ai-assisted/Adafruit_Blinka.git@tachyon-qcm6490-gpiochip4-fix
```

Without it, `board.SDA` returns the internal `(chip, line)` tuple representation instead of a usable `Pin` object, breaking both real I2C and Blinka's `digitalio` layer.

---

## First-Time Host Setup

Run `scripts/setup-hil-host.sh` as root on the target machine — see that script for full details. In short it:

1. Adds the HIL user to `gpio`, `i2c`, `plugdev`, `dialout`, `video` groups
2. Installs an SSH authorized key so the controller can connect

```bash
# On the HIL host, as root:
bash /path/to/usbip-hil-controller/scripts/setup-hil-host.sh particle ~/.ssh/id_ed25519.pub
```

The group change takes effect on the next login. Log out and back in (or `newgrp`) before running hardware tests.

---

## Controller-side per-host SSH keys

`setup-hil-host.sh` (above) installs the *public* key on a remote HIL host. The
complementary half lives on the **controller**: each `hosts:` entry in
`run/topology.yaml` names an `ssh_key_path`, and the controller hands that exact
path to asyncssh as `client_keys` (`hosts/ssh.py`). The service does **not** read
`~/.ssh` or use an ssh-agent — it only opens the literal file at `ssh_key_path`.

Consequences:

- "I can `ssh` into the host from my shell" does **not** mean the service can. Your
  personal `~/.ssh/...` key is irrelevant to the controller process.
- A missing file shows up as a job failing in `flashing`/`preparing` with
  `[Errno 2] No such file or directory: '/etc/hil/keys/<host>'` — this happens
  *before* any clone/build/flash runs.

The controller service runs as **`particle`**, so keys must be readable by that
user (the generic `hil` user in ARCHITECTURE.md §12 maps to `particle` on this
bench). Provision a key — reusing one you already have authorized on the host:

```bash
# On the Tachyon controller, as particle:
sudo install -m 0400 -o particle -g particle \
  ~/.ssh/<key-that-logs-into-the-host> /etc/hil/keys/<host-id>

# verify as the service user (not yourself):
sudo -u particle ssh -i /etc/hil/keys/<host-id> <ssh_user>@<addr> 'echo ok'
```

Keys are read per-connection, so no `systemctl restart` is needed after adding one.
Several hosts share `/etc/hil/keys/rpi-hil-fleet`; `rpi-displays` uses its own
`/etc/hil/keys/rpi-displays`.

---

## Resuming After Reboot / Fresh Session

### 1. Ensure SSH agent has the key

```bash
ssh-add ~/.ssh/id_ed25519
ssh particle@192.168.1.169 "echo ok"
```

### 2. Start the HIL controller

```bash
ssh particle@192.168.1.169 "
  cd ~/dev-projects/python/usbip-hil-controller &&
  set -a && source run/controller.env && set +a &&
  nohup .venv/bin/hil-controller > /tmp/hil.log 2>&1 &
  sleep 4 && curl -s http://localhost:8080/healthz
"
```

Check topology loaded correctly:

```bash
ssh particle@192.168.1.169 "curl -s -H 'Authorization: Bearer dev-token-change-me' \
  http://localhost:8080/v1/topology | python3 -m json.tool"
```

### 3. Run the Wippersnapper Python test suite

```bash
ssh particle@192.168.1.169 "ss -tlnp | grep -E '1884|5173' || echo ports-free"
# kill any stale ProtoMQ if needed:
ssh particle@192.168.1.169 "fuser -k 1884/tcp 2>/dev/null; fuser -k 5173/tcp 2>/dev/null"

ssh particle@192.168.1.169 "
  cd ~/dev-projects/python/Adafruit_Wippersnapper_Python &&
  timeout 300 .venv/bin/python -m pytest test/ -m 'not hardware' -q --tb=short
"
```

Expected: **66 passed, 2 failed, 19 skipped** (the 2 failures are EPD unit tests, not regressions).

### 4. Run the ILI9341 hardware test

Requires the display physically connected and `BLINKA_OS_AGNOSTIC` unset:

```bash
ssh particle@192.168.1.169 "
  cd ~/dev-projects/python/Adafruit_Wippersnapper_Python &&
  BLINKA_OS_AGNOSTIC=0 WS_REAL_DISPLAY_TEST=1 \
  timeout 120 .venv/bin/python -m pytest \
    test/integration/display_test.py::test_display_real_ili9341_240x320_tachyon_webcam -v
"
```

Snapshots go to `WS_DISPLAY_SNAPSHOT_DIR` if set, otherwise pytest's `tmp_path`.

---

## HIL Controller Topology

| Resource | ID | Type | Notes |
|---|---|---|---|
| Host (runner) | `localhost` | `kind: local` | Tachyon itself |
| Host (broker) | `tachyon-protomq` | `role: protomq-broker` | ProtoMQ at `127.0.0.1:1884` |
| Device | `tachyon-runner-a` | `sbc / particle-tachyon` | pool: wippersnapper-python |
| Aux | `tachyon-ili9341` | `display / ili9341-240x320` | SPI via EYESPI Beret |

---

## Known Issues / Next Steps

### ProtoMQ async `stop()` — potential upstream PR

`ProtoMQService.stop()` is `async def` but is registered with `atexit.register(self.stop)`. At Python exit, atexit calls it synchronously, producing an unawaited coroutine — the node process is never killed, leaving port 1884 occupied for the next run. This only bites when the test process exits abnormally (e.g. timeout, SIGKILL). Normal fixture teardown (`await protomq.stop()`) works correctly.

Workaround: `fuser -k 1884/tcp` before running tests after any abrupt exit.

Fix (not yet upstreamed): register a sync wrapper with atexit; `async def stop()` delegates to it.

### ProtoMQ web build — required after fresh clone

`tools/protomq/main.js` exits early without `dist/index.html`. Run once:

```bash
cd ~/dev-projects/python/Adafruit_Wippersnapper_Python/tools/protomq
npm run build-web
```

### HIL controller token

`dev-token-change-me` is the static bootstrap token. Replace for any non-local use:

```bash
ssh particle@192.168.1.169 "
  cd ~/dev-projects/python/usbip-hil-controller &&
  set -a && source run/controller.env && set +a &&
  .venv/bin/python scripts/mint-token.py
"
```

### Submitting a Wippersnapper git-source job

```bash
GH_PAT=ghp_xxx \
HIL_API_TOKEN=dev-token-change-me \
HIL_API_BASE=http://192.168.1.169:8080 \
MQTT_HOST=127.0.0.1 \
WIPPER_REF=displays-v2 \
bash scripts/submit-wipper-test.sh
```
