---
name: project-state
description: Current implementation state of the HIL controller
metadata:
  type: project
---

As of 2026-05-22, main is at commit 8ad86c3.

**What is built:**

M0–M4.5: pyproject.toml, FastAPI app, `/healthz` `/readyz`, SQLite init, `POST /v1/jobs`,
long-poll wait, cancel, asyncio scheduler+EventBus, SSHTransport, GitDeployAdapter,
RealHostRegistry, static + argon2id bearer auth, mint-token.py, topology.example.yaml,
systemd unit, hil-controller-ci.yml.

M1.5: hosts/devices/auxes/connections/audit_log DB tables; topology seeder; full topology
REST endpoints.

M2 (no OIDC): Principal auth, pool/profile/capability gating, audit log, mint-token.py
extended.

LocalTransport + PAT injection: `hosts/local.py` subprocess transport; `source.pat` injects
into HTTPS clone URL; `RealHostRegistry` routes `kind: local` hosts to LocalTransport;
`topology.example.yaml` has a localhost host + wippersnapper-python device;
`examples/wippersnapper-python/job.json` + `scripts/submit-wipper-test.sh` for demo runs.

M5 (partial — ProtoMQ observer): `adapters/protomq_observer.py` — activate script via HTTP,
subscribe MQTT `#`, forward messages as job log events (protobuf topics shown as size,
others decoded), emit completed steps on teardown. `aiomqtt>=2.0` added.

Scheduler now wired to RealHostRegistry: `main.py` creates `RealHostRegistry` when
`HIL_TOPOLOGY_FILE` is set; was always using `_FakeAdapter` before.

Log streaming: `GitDeployAdapter` stores `_run_stdout/_run_stderr/_deploy_stdout/_deploy_stderr`;
`JobWorker` emits them as `log` kind events after run/deploy phases.

ProtoMQ observer wired into worker: started as concurrent asyncio Task during run phase,
cancelled after test, completed steps emitted. Configured via `params.protomq.{broker_host,
mqtt_port, api_port, script}`.

**181 tests pass, 0 failures.** (as of 2026-05-25, commit bf04757)

**HTMX web UI (done 2026-05-23):**
`/ui/` — Jinja2/HTMX admin interface served from `src/hil_controller/web/`.
- Login/logout (cookie `hil_token`, same auth as Bearer API)
- Dashboard with counts + recent jobs
- Hosts CRUD: add/edit/delete inline
- Devices CRUD: add/edit/delete, kind filter (microcontroller/sbc)
- Hardware/Aux CRUD with connection management (which Adafruit product is on which device)
- Cameras CRUD (new cameras table; streams_json; host_id for v4l2; notes)
- ProtoMQ script browser (set `HIL_SCRIPTS_DIR` to vendor/protomq/scripts/)
- Static files at /ui/static/app.css; HTMX from CDN unpkg

**Camera integration (done 2026-05-25, commit bf04757):**
`adapters/camera/` — standalone library ported from tyeth/protomq PR#1.
- calibration.py: compute_scale(), transform_roi() (pure math, no cv2)
- frame_extractor.py: Frame dataclass, extract_distinct_frames(), _classify_change()
- qr_locator.py: BoundingBox, scan_qr_codes(), segment_board_roi() (GrabCut→Otsu→padding)
- recorder.py: VideoRecorder (background thread, cv2 VideoWriter)
- report.py: generate_report() HTML
- sources.py: CameraSource protocol, IPCamera (HTTP), V4L2Camera (Phase 2 stub)
- monitor.py: ROI dataclass, CameraMonitor (thread-safe frame→crop loop)
- capture.py: CameraCapture, ROIStore protocol, CameraArtifacts

DB schema additions: cameras table, camera_rois table, camera_id+qr_identifier on devices.
Migration: existing auxes kind=camera copied to cameras table.
API: GET/list cameras, snapshot, GET/PUT/DELETE ROI per device, POST calibrate+save.
Devices form: camera_id + qr_identifier fields + live camera panel with snapshot thumbnail.
47 new tests. opencv-python-headless + pyzbar + numpy as optional [camera] deps.

**Arduino WipperSnapper Test job (done 2026-05-25):**
- New `GET /ui/jobs/new-arduino-ws` + `POST /ui/jobs/arduino-ws` routes in router.py
- Template `job_new_arduino_ws.html` — WipperSnapper Arduino ref, protoMQ ref + play-script
  selector, MCU device picker, MQTT settings, extra setup, test command, timeouts.
- The job clones WipperSnapper Arduino at `wippersnapper_ref`, prepends
  `git clone … protomq {protomq_ref}` to setup, runs pytest against it.
- Config: `HIL_WIPPERSNAPPER_ARDUINO_REPO`, `HIL_PROTOMQ_REPO`, `HIL_PROTOMQ_DEFAULT_REF`
  (all default sensibly). Empty ref falls back to "main" / config default.
- jobs.html: "+ Arduino WS Test" button alongside existing "+ Python" / "+ Arduino Flash".
- 3 new tests (42 web UI tests pass, 187 total).

**M6 — USB identity (done 2026-05-26):**
- `device_usb_ids` (surrogate PK; UNIQUE expression index over
  `COALESCE(iserial,'')`). Roles: runtime|bootloader|dfu|msc|cdc|unknown.
- `devices` gained `hub_host_id`, `hub_port_path`, `solenoid_channel`,
  `usb_serial`. Topology YAML `usb_ids:` list seeded; legacy `usb:` still
  works; migration backfills from `usb_json`.
- REST: `/v1/devices/{id}/usb-ids` GET/POST/DELETE,
  `/v1/devices/lookup-by-usb`, `/v1/devices/{id}/learn-usb`.
- HTMX UI: list editor + Learn USB IDs button (optional reset cycle).
- `device_leases` + `queue/leases.py` (BEGIN IMMEDIATE, exclusive_device
  vs exclusive_hub conflict matrix). `/v1/leases` GET/POST/DELETE.
  Startup sweep releases crashed-job leases. Scheduler acquires per job.
  Important boundary: leases decide ownership/exclusivity only; they do
  not themselves expose a public usbip bind/attach/detach API.
- `adapters/usb_scan.py` (parse_usbip_list, learn_once, passive_learn_loop)
  runs as a background task during exclusive leases; auto-records
  unseen VID/PIDs with source='passive'.
- `adapters/usb_fingerprint.py` (UsbFingerprintAdapter.learn): acquires
  exclusive_hub, depowers via solenoid, repowers target port, captures
  VID/PIDs; optional reset cycle splits bootloader vs runtime.
- Current public usbip state: read-only inventory (`GET
  /v1/hosts/{id}/usbip/exportable`) is implemented; public mutation
  routes for bind/unbind/attach/detach remain future M4 work. Internal
  controller-managed flows use `adapters/usbip_bridge.py`.
- 56 new tests across PR1–PR5; **275 total pass**.

**Job matching fix (2026-05-27):** Arduino-WS jobs silently "passed" doing nothing
because the builder pinned `pool: wippersnapper-arduino` but all MCU devices are in
`pool: public`, so `find_device_for_job` fell back to `_FakeAdapter` (no deploy/run).
Now: explicit `device.id` selection bypasses pool/kind/cap gates; capability subset
matching implemented; builder no longer pins a pool and auto-selector requests
`["wippersnapper"]`; no-match returns `_UnmatchedAdapter` whose `acquire()` raises so
the worker emits `state=error` + a diagnostic log instead of a silent pass.

**Not yet done:**
- M2 remainder: GitHub OIDC verifier, policy file
- M2.5: secret profiles YAML, per-job secrets materialisation, artifact sanitisation
- M3.5: MCU adapter chain (serial capture, esptool, MCP23017)
- M4: USB-IP, solenoid-hub reset, uf2-msc / picotool flashers
- M5 camera remainder: V4L2Camera SSH wiring, VideoRecorder in CameraCapture.stop(),
  artifact storage, frame extraction at job end, Prometheus metrics
- topology/importers/ (protomq_scripts.py, hardware_md.py)
- ProtoMQ protobuf decoding (Python proto definitions not yet compiled)
- GET /v1/jobs/{id}/logs non-blocking endpoint
