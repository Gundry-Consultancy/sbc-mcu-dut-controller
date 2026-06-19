# firmware-bench logging — verbosity & the live allow-list

A `firmware-bench` job produces two kinds of logs:

| sink | what | verbosity |
|---|---|---|
| **live job stream** (the events CI tails via `GET /v1/jobs/{id}/wait`, and the inline PR-comment excerpt) | each command's argv + output, streamed as it runs | controlled by `log_level` |
| **`hil-assets`** (`flash.log` / `serial.log` / `protomq.log`, downloadable) | the complete transcript — every stdout **and** stderr line, per-command UTC-ms timestamped | **always full**, regardless of `log_level` |

## The `log_level` job param

Set in the job request `params`:

```jsonc
{
  "script": "firmware-bench",
  "params": {
    "log_level": "all",        // default — stream EVERYTHING (every stdout+stderr line)
    // "log_level": "filtered" // low-verbosity — stream only the allow-list + a summary
    "stages": [ ... ]
  }
}
```

- **`all`** (default): the live stream carries every line of every command — erase
  progress (`Flash memory erased successfully in 17.7 seconds`), `Writing at 0x…`,
  esptool warnings (incl. the `erase_flash`→`erase-flash` / `no_reset` deprecation
  notices, which are on **stderr**), and any errors. One UTC-ms-stamped event per
  command.
- **`filtered`**: the live stream carries only lines matching the aggressive
  allow-list (plus the failing stderr tail on a non-zero exit). Use it when the
  full stream is too noisy to watch live. **You lose nothing** — `flash.log` in the
  `hil-assets` artifact is still the complete transcript.

## Updating the allow-list (filtered view)

The allow-list is a single constant — **`NOTABLE_LIVE_LINES`** in
[`src/hil_controller/adapters/bench_stages.py`](../src/hil_controller/adapters/bench_stages.py).
A command-output line is surfaced in `filtered` mode if it contains **any** of the
substrings in that tuple. To change what `filtered` shows, edit that tuple — it is
deliberately the only place to touch, and pruning/extending it can never drop data
(the asset transcript is always complete).

Current entries cover chip identity (`Chip is`, `MAC:`, `Features:`), flash/erase
milestones (`Erasing`, `erased successfully`, `Writing at`, `Wrote `, `Compressed`),
and verify/reset (`Hash of data verified`, `Verifying`, `Leaving`, `Hard resetting`).
