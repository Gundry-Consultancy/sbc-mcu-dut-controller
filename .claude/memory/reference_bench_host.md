---
name: reference-bench-host
description: Bench repo layout + deploy workflow for the live HIL controller (host/SSH basics in project-deployment)
metadata:
  type: reference
---

Deploy/layout details for the live controller. For host + SSH-client basics see
[[project-deployment]] and [[ssh-openssh]] (Windows OpenSSH at
`/c/Windows/System32/OpenSSH/ssh.exe`).

- **Repo:** `/home/particle/dev-projects/python/usbip-hil-controller`
- **Service:** systemd `hil-controller.service`, `EnvironmentFile=run/controller.env`
  (sets `HIL_DB_PATH=run/jobs.db`, `HIL_TOPOLOGY_FILE=run/topology.yaml`,
  `HIL_STATIC_TOKEN`, host/port).
- **DB query:** `sqlite3` CLI is NOT installed — use `.venv/bin/python3` + `sqlite3` module.
- **Topology:** `run/topology.yaml` is **git-tracked** (NOT git-ignored — an old note here
  was wrong). It is live-edited on the bench, but per standing policy (2026-06-19) **every
  topology change must be committed and pushed upstream** — commit the live file on the bench
  (credential helper `!sudo gh auth git-credential`) and `git push origin HEAD:main`, so git ==
  live and there is no untracked drift. All MCU devices are `pool: public` with capabilities
  `[arduino, wippersnapper, ...]`. The controller host itself is host id `localhost`
  (`transport: local`, sbc-fleet python-snapper runner, device `tachyon-runner-a`); the protoMQ
  broker is `tachyon-protomq` (`transport: none`) — both now in topology, not DB-only.

**Deploy is always via git** ([[feedback-commit-and-push]]): push to origin, then on the bench
`git pull && sudo systemctl restart hil-controller`. Never scp/rsync code. M6 migrations are
additive (new columns/tables) so pulling forward is safe.

**Deploy auth + remote FIXED 2026-06-12 (was briefly "blocked"):**
- Bench `origin` now = `https://github.com/Gundry-Consultancy/usbip-hil-controller.git`
  (tokenless — the old `tyeth-ai-assisted` URL with an embedded `github_pat_...` was scrubbed;
  that PAT leaked into a transcript and **should still be rotated**). Auth uses
  `git config credential.helper "!sudo gh auth git-credential"` — root's `gh` is logged in as
  `tyeth-ai-assisted` (passwordless sudo works for `particle`, so `sudo gh` is non-interactive).
  `git fetch` over HTTPS now succeeds without any embedded token.
- The "divergent history" scare was a FALSE ALARM: the bench's `origin/main` was just stale
  (hadn't fetched). After fetching, `origin/main..HEAD` was empty — bench tip `03ca213` was a
  plain **ancestor**, so it was a clean fast-forward (`03ca213..6568b6e`), not divergence.
- **FF gotcha:** the merge aborts if an untracked file collides with one now tracked upstream
  (hit `.claude/settings.local.json` — origin started tracking it). Move the bench's copy aside
  (`mv .claude/settings.local.json /tmp/...`) then FF.
- **topology preservation:** `run/topology.yaml` is tracked AND live-edited; snapshot it
  (`cp run/topology.yaml /tmp/...` + a `run/topology.yaml.predeploy-<ts>` backup), `git checkout
  --` it to clean the tree, FF, then `cp` the snapshot back. Never `git reset --hard`. (This
  time origin's committed topology already equalled the live file, so nothing changed.)
- **DON'T pipe `git merge --ff-only ... | tail`** — the pipe masks the merge's exit code and the
  `&&` chain runs on even when the FF aborted. Capture `$?` instead.

**Service:** runs as user `particle` (MainPID via `systemctl show hil-controller -p MainPID`).
HTTP on **port 8080**; web token = `HIL_STATIC_TOKEN` in `run/controller.env` (cookie `hil_token`).

**Gotcha — `git pull` "insufficient permission for adding an object to .git/objects":**
a prior `sudo git` left root-owned objects under `.git`. Fix:
`sudo chown -R particle:particle .git` then pull. `run/topology.yaml` is actually **tracked**
(shows as modified after local edits), not gitignored as previously noted — `git pull --ff-only`
is safe (no upstream topology changes), but never `git reset --hard` (wipes the live topology).

**SSH from this Windows box — use the Windows OpenSSH binary, not git-bash's ssh.**
The Bash tool's git-bash ssh-agent can die (`Error connecting to agent: Device or
resource busy` / `Connection refused`), and `~/.ssh/id_ed25519` is passphrase-protected,
so `ssh particle@192.168.1.169` then fails `Permission denied (publickey,password)`.
Fall back to `/c/Windows/System32/OpenSSH/ssh.exe particle@192.168.1.169 "..."`, which
uses the Windows ssh-agent **service** (holds the unlocked key). Note `~/.ssh/config` maps
`Host 192.168.1.169` to `User root` — always pass `particle@` explicitly.

**Stale-vs-race lease (`device ... blocked by lease #N (exclusive_device)`):** before
treating a lease as stuck, check its `released_at` — a freshly *cancelled* job can still be
mid-release when the next job reaches its flash phase (saw lease #10 release 4 s after the
blocked acquire). `select id,job_id from device_leases where released_at is null` shows the
truly-held ones; if that's empty, the device is free and you just rerun. Rerun cleanly via
`POST /ui/jobs/{id}/rerun` (cookie `hil_token`=`HIL_STATIC_TOKEN`) — it reuses the stored
`request_json`, so an embedded PAT is preserved without re-handling the secret.

**usbip flashing (per-phase exec, [[project-exec-location-feature]]):** `usbipd` on rpi-displays
must be **running and listening on :3240** or the controller's `usbip attach` fails with
`usbip: error: tcp connect` (build can succeed, flash still never reaches the DUT). Provisioned
2026-06-07 via `bash scripts/setup-hil-host.sh pi <pubkey>` run under `sudo` on the bench:
installs `hil-usbipd.service` (Debian/RPi ship the `usbip` binary but no service unit), the
`/etc/sudoers.d/hil-usbip` passwordless drop-in, and persists `vhci-hcd`+`usbip-host` modules
via `/etc/modules-load.d/hil-usbip.conf`. The service is now **enabled + active** and survives
reboot — no more manual `sudo usbipd -D`. `vhci_hcd` loads on tachyon via `sudo modprobe
vhci-hcd` (the bridge does this at flash time; passwordless sudo for usbip/modprobe already
works for `particle`). busid for the revtft Feather = `1-1.1.1.4` (VID/PID 239a:8123 in app
mode); bind→`usbip list -r 127.0.0.1`→unbind cycle confirmed working. Build-on-controller works
once `~/.platformio` cache is clean (see [[project-exec-location-feature]]).

**`usbip` binary not on `pi`'s PATH** — it lives in `/usr/sbin/usbip`, so invoke as
`sudo -n /usr/sbin/usbip ...` from controller-side helpers (or rely on the sudoers drop-in
which uses the absolute path).

**Submodule clone on the bench needs the credential helper passed explicitly
(2026-06-12):** `git submodule update --init` fails with `could not read Username for
https://github.com` because the `!sudo gh auth git-credential` helper is set on the
*parent* repo's local config and a fresh submodule clone (new git process/dir) doesn't
inherit it. Fix: `git -c credential.helper="!sudo gh auth git-credential" submodule
update --init vendor/<name>`. (The `vendor/hil-detection` submodule is reference-only —
the running controller doesn't import it — so a missing clone doesn't break the service,
but this is how to get the bench fully in sync.)
