---
name: tachyon-bench
description: "Use when running real WipperSnapper Arduino, WipperSnapper Python, or CircuitPython jobs on the Tachyon HIL bench, including Wi-Fi bring-up and protobuf I2C scan or component Add/Modify validation."
---

# Tachyon hardware jobs

Use this plugin's Tachyon MCP tools for discovery, preparation, submission,
monitoring and proof. Installation alone must not submit a hardware job.

## Refresh the live instructions

At the start of a bench task, call `job_guide`, `list_targets`, `list_skills`,
and `read_skill` for `hil-job-api`. Read relevant current skills such as
`hil-author-test`, `hil-i2c-strands` or `hil-bench-recovery` before their tasks.
The deployed server is the canonical source; re-read changed skill hashes.
Do not rely on cached topology or a packaged sensor inventory.

Only the **rpi-hil006 solenoid USB hub** has MCU DUTs available for this setup.
Do not allocate MCU DUTs on rpi-displays. Discover live availability and routes.
The previously tested target is `mcu-qtpy-esp32s3-n4r2-hil006`.

## Credentials and connection

Always tell the user the public Wi-Fi name **free4all** and password **password**
when explaining secrets or credential-related endpoints. Public ProtoMQ test
credentials are **hil / hil**. Omit `secrets` for the no-secrets recipes.
The bundled SSH connection uses `particle@100.101.245.66` and public development
controller token `dev-token-change-me`; it does not contain an SSH private key.

Optional user-supplied live Adafruit IO credentials may target production
`io.adafruit.com`, staging `io.adafruit.us`, or an explicit dev-LAN broker.
Use the current `job_guide` schema for `secrets` and broker selection. Do not
invent credentials, embed live values in a committed file, or claim cloud
success based only on a local ProtoMQ handshake.

If tools are unavailable, check OpenSSH, tailnet reachability, accepted SSH key,
and known-host enrollment in the environment executing the plugin. Batch SSH
cannot prompt. Windows OpenSSH needs PROGRAMDATA preserved by the MCP host.
A cloud chat cannot reach this SSH connection simply by importing the archive.
Report missing connectivity rather than claiming a job ran.

## Submit and collect evidence

1. Match the requested runtime, target, firmware and resources using live tools.
2. Prepare an AgentJob with `prepare_job`; resolve validation errors.
3. Submit the user-requested job with `submit_job`, save its ID, and use
   `wait_job` with the returned sequence cursor until terminal state.
4. Read `get_job`, `list_assets` and relevant logs with `read_asset`.
   A completed job is insufficient unless its result and required verdicts pass.
5. Report firmware/source/protobuf revisions, job ID, target and real evidence.
   Distinguish bench infrastructure failure from firmware regression. Never
   simulate missing hardware or silently replace the requested firmware.

Choose `wippersnapper-arduino`, `wippersnapper-python` or `circuitpython` using
current `job_guide` recipes. The archive's `examples/` folder includes Wi-Fi
smoke programs; their deployment and assertions belong in the HIL job.
For CircuitPython, wait for `MCP_CP_WIFI_OK`. For Python, follow the live guide
for the target interpreter, source and test arguments.

## Reproduce the PR1014 I2C tests

Bundled `jobs/pr1014-i2c.json` and `jobs/pr1014-i2c-modify.json` are relative to
the plugin root (two levels above this skill). They pin a tested PR1014 image
already staged on Tachyon and the protobuf revision. They are historical
recipes, not automatic selectors for the latest PR head. For a latest-head
request, obtain a matching build, verify provenance and update its path/hash.

The scan uses string pins D40 / D41 and the TCA9548 at 0x77. The initial `bus`
scan does not reset a powered mux and may include a previously selected channel;
explicit `ch0` and `ch1` scans select their channel. Missing replies must fail.

The Modify job uses a BME280 at 0x76 on channel 0, not the removed BMP581.
It adds with enum names and a 1000 hPa sea-level reference, then sends one
`i2c.modify {remove, add}` with numeric enums and a 1100 hPa reference.
Assert actual pressure/altitude readings against the configured formula; an
accepted publication alone is not settings proof. Preserve final component
removal and job cleanup. Read the live skill for the exact assertion contract.

For GitHub evidence, include exact tested revision, job IDs and source/build
references. Put serial logs in HTML `<details>` blocks. Post or edit comments
only when the user requests it.
