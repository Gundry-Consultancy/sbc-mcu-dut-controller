"""WipperSnapper version-bisection engine.

Find the first WipperSnapper-Arduino **release** where a given board broke, by
binary-searching the releases between a known-good ("working") and known-bad
("broken") ref, flashing + testing each candidate on real HIL hardware through
the controller's ``firmware-bench`` API.

Design (per the project spec):

* **Releases only** (initially). Candidates are the published releases between the
  two refs that ship a flashable asset for the board (``asset_glob``); releases
  without the asset are skipped (logged). Direction is inferred — "working" may be
  older or newer than "broken".
* **Oracle validation first.** Flash+test BOTH endpoints before bisecting. The
  working ref must PASS and the broken ref must FAIL. If the broken ref also
  PASSES → the job fails with "test criteria were not specific enough, both
  versions passed" (+ the logs). If the working ref fails → the oracle is invalid.
* **Verdict per version:**
    - ``PASS``  — flashed, booted, and checked in (``CHECKIN_VERDICT ok=true``).
    - ``FAIL``  — flashed but **broken**: booted-but-no-checkin *or* didn't come up
      (``CHECKIN_VERDICT ok=false``; job finished). This IS a valid verdict — a
      version that flashes but fails to connect is "broken", move on.
    - ``INFRA`` — couldn't flash / host wedged (job errored before the verdict
      line). NOT a firmware verdict → recover + retry.
* **Verify twice.** Each version is tested up to ``verify_times`` and the
  PASS/FAIL results must agree, guarding against a false detection.

The pure helpers (:func:`version_key`, :func:`parse_releases`,
:func:`select_window`, :func:`bisect`) are network-free and unit-tested; the
:class:`BisectRunner` wires them to the controller + GitHub HTTP APIs.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

WS_REPO = "adafruit/Adafruit_Wippersnapper_Arduino"

#: An Adafruit IO **cloud** host needs a REAL account (anonymous / job-id creds
#: don't authenticate). The local protomq broker is anonymous, so anything else
#: is treated as local.
_CLOUD_IO_RE = re.compile(r"(^|\.)adafruit\.(com|us)$", re.IGNORECASE)
#: Credential values that are config *placeholders*, not real accounts — so a
#: misconfigured controller fails loudly instead of flashing a board that
#: reboot-loops on an MQTT auth reject.
_PLACEHOLDER_IO_KEYS = {"", "placeholder", "your_aio_key_here", "your_io_key_here"}


def is_cloud_broker(io_url: str) -> bool:
    """True if ``io_url`` is an Adafruit IO cloud host (needs a real account)."""
    host = (io_url or "").strip().lower()
    host = host.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return bool(_CLOUD_IO_RE.search(host))


def is_real_io_key(key: str) -> bool:
    """True if ``key`` looks like a real Adafruit IO key (not a placeholder)."""
    return (key or "").strip().lower() not in _PLACEHOLDER_IO_KEYS


# --------------------------------------------------------------------------- #
# Verdict                                                                     #
# --------------------------------------------------------------------------- #


class Verdict(StrEnum):
    PASS = "pass"  # flashed + booted + checked in
    FAIL = "fail"  # flashed but broken (no checkin / didn't come up)
    INFRA = "infra"  # couldn't flash / host problem — retry, not a verdict


# --------------------------------------------------------------------------- #
# Release model + enumeration (pure)                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Release:
    tag: str
    key: tuple[int, int, int, int]
    asset_url: str
    asset_name: str


_VER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-(?:offline-)?beta\.(\d+))?", re.IGNORECASE)
#: A final (non-beta) release sorts ABOVE every beta of the same x.y.z.
_FINAL = 1_000_000


def version_key(tag: str) -> tuple[int, int, int, int] | None:
    """Parse a release tag into a sortable ``(x, y, z, beta)`` key, or None.

    ``1.0.0-beta.78`` → ``(1, 0, 0, 78)``; ``1.0.0`` → ``(1, 0, 0, 1_000_000)``
    (a final release ranks above its betas). Unparseable tags return None.
    """
    m = _VER_RE.search(tag or "")
    if not m:
        return None
    x, y, z = int(m.group(1)), int(m.group(2)), int(m.group(3))
    beta = int(m.group(4)) if m.group(4) is not None else _FINAL
    return (x, y, z, beta)


def parse_releases(gh_json: list[dict[str, Any]], asset_glob: str) -> list[Release]:
    """Build the sorted (ascending) list of releases that ship a matching asset.

    ``gh_json`` is the GitHub ``/releases`` array. A release is included only if
    one of its assets' names matches ``asset_glob`` (fnmatch) — that asset's
    download URL is the firmware. Releases with no matching asset (or an
    unparseable tag) are dropped.
    """
    out: list[Release] = []
    for rel in gh_json:
        tag = rel.get("tag_name") or rel.get("name") or ""
        key = version_key(tag)
        if key is None:
            continue
        asset = next(
            (a for a in rel.get("assets", []) if fnmatch.fnmatch(a.get("name", ""), asset_glob)),
            None,
        )
        if asset is None:
            continue
        out.append(
            Release(
                tag=tag,
                key=key,
                asset_url=asset["browser_download_url"],
                asset_name=asset["name"],
            )
        )
    out.sort(key=lambda r: r.key)
    return out


def select_window(
    releases: list[Release], working_ref: str, broken_ref: str
) -> tuple[list[Release], int, int]:
    """Restrict to the inclusive version window between the two refs.

    Returns ``(window, working_idx, broken_idx)`` where ``window`` is the
    ascending candidate list bounded by the two refs (inclusive) and the indices
    locate the working/broken endpoints within it. Raises ``ValueError`` if
    either ref isn't among the asset-bearing releases.
    """
    by_tag = {r.tag: i for i, r in enumerate(releases)}
    wk = version_key(working_ref)
    bk = version_key(broken_ref)

    def _find(ref: str, key: tuple[int, int, int, int] | None) -> int:
        if ref in by_tag:
            return by_tag[ref]
        if key is not None:
            for i, r in enumerate(releases):
                if r.key == key:
                    return i
        raise ValueError(
            f"ref {ref!r} is not a release with a matching asset "
            f"(have {len(releases)} candidates {releases[0].tag}..{releases[-1].tag})"
        )

    wi, bi = _find(working_ref, wk), _find(broken_ref, bk)
    lo, hi = (wi, bi) if wi <= bi else (bi, wi)
    window = releases[lo : hi + 1]
    return window, wi - lo, bi - lo


def bisect(
    window: list[Release],
    working_idx: int,
    broken_idx: int,
    test_fn: Callable[[Release], Verdict],
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> dict[str, Any]:
    """Binary-search ``window`` for the boundary between working and broken.

    Assumes the endpoints are already validated (``working_idx`` PASSes,
    ``broken_idx`` FAILs). ``test_fn`` returns PASS/FAIL for an interior release
    (it must retry/raise on INFRA itself — bisect treats only PASS/FAIL).
    Returns ``{first_broken, last_good, tested}`` where ``first_broken`` is the
    earliest release on the broken side adjacent to the last good one. Works in
    either direction (good/bad indices converge regardless of which is higher).
    """
    good, bad = working_idx, broken_idx
    tested: list[dict[str, str]] = []
    step = 1 if bad > good else -1
    while abs(bad - good) > 1:
        mid = (good + bad) // 2
        rel = window[mid]
        v = test_fn(rel)
        tested.append({"tag": rel.tag, "verdict": v.value})
        log(f"bisect: {rel.tag} -> {v.value}  (good={window[good].tag} bad={window[bad].tag})")
        if v == Verdict.PASS:
            good = mid
        else:
            bad = mid
    return {
        "first_broken": window[bad].tag,
        "last_good": window[good].tag,
        "tested": tested,
        "direction": "forward" if step > 0 else "backward",
    }


# --------------------------------------------------------------------------- #
# Runner (HTTP I/O)                                                           #
# --------------------------------------------------------------------------- #


#: Connectivity-test pipeline for a SAM/UF2 board: enter the UF2 bootloader (tight
#: 1200-touch hammer), **erase the app region** (so a no-op/failed flash can't
#: leave STALE firmware booting → a false PASS — the bug that made a v128 job
#: "pass" while still running the previous image), copy the .uf2, boot, write
#: secrets onto the WIPPER drive pointing at ``io_url``, reboot, and assert a
#: checkin. Default target is the **io.adafruit.com cloud** over TLS (the AirLift's
#: MQTT CONNECT is rejected by the strict local protomq), so the checkin is
#: verified from the **serial** log (``via: serial`` → the WS "Registration and
#: configuration complete" banner) — soft, so a no-checkin is a FAIL verdict, not a
#: job error. ``start_serial_log`` / ``print_boot_log`` are auto-injected (the
#: boot log carries the WS "Firmware Version:" line for the operator to eyeball);
#: ``launch_protomq`` is skipped because the write_secrets stage carries an
#: explicit (external) ``io_url``.
def default_stages(
    flasher: str = "uf2-msc",
    *,
    io_url: str = "io.adafruit.com",
    io_port: int = 8883,
    checkin_timeout_s: int = 240,
) -> list[dict[str, Any]]:
    return [
        {"type": "enter_bootloader", "flasher": flasher},
        {"type": "erase", "flasher": flasher},
        {"type": "flash", "flasher": flasher},
        {"type": "power_cycle"},
        {"type": "write_secrets_msc", "io_url": io_url, "io_port": io_port},
        {"type": "power_cycle"},
        {
            "type": "verify_checkin",
            "via": "serial",
            "soft": True,
            "checkin_timeout_s": checkin_timeout_s,
        },
    ]


@dataclass
class BisectConfig:
    device_id: str
    working_ref: str
    broken_ref: str
    asset_glob: str
    base_url: str
    token: str
    repo: str = WS_REPO
    flasher: str = "uf2-msc"
    secrets: dict[str, str] = field(default_factory=dict)
    stages: list[dict[str, Any]] | None = None
    #: Broker the DUT checks in to. Default is the io.adafruit.com cloud (the
    #: AirLift's MQTT CONNECT is rejected by the strict local protomq); set to
    #: an empty io_url to use the per-session local broker (anonymous creds).
    io_url: str = "io.adafruit.com"
    io_port: int = 8883
    checkin_timeout_s: int = 240
    gh_token: str = ""
    verify_times: int = 2
    infra_retries: int = 2
    job_timeout_s: float = 900.0
    window_minutes: int = 2


class BisectError(RuntimeError):
    """A bisection precondition failed (bad oracle / unflashable target / etc.)."""


class BisectRunner:
    """Drive a release bisection over the controller + GitHub HTTP APIs."""

    def __init__(self, cfg: BisectConfig, *, log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.log = log
        self._stages = cfg.stages or default_stages(
            cfg.flasher,
            io_url=cfg.io_url,
            io_port=cfg.io_port,
            checkin_timeout_s=cfg.checkin_timeout_s,
        )

    # -- HTTP helpers ------------------------------------------------------- #

    def _gh_headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json"}
        if self.cfg.gh_token:
            h["Authorization"] = f"Bearer {self.cfg.gh_token}"
        return h

    def _ctrl_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.token}"}

    def fetch_releases(self) -> list[Release]:
        url = f"https://api.github.com/repos/{self.cfg.repo}/releases?per_page=100"
        rels: list[dict[str, Any]] = []
        with httpx.Client(timeout=30.0, follow_redirects=True) as c:
            page = 1
            while True:
                r = c.get(f"{url}&page={page}", headers=self._gh_headers())
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                rels.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        return parse_releases(rels, self.cfg.asset_glob)

    def _submit(self, client: httpx.Client, asset_url: str) -> str:
        body = {
            "target": {"device": {"id": self.cfg.device_id}, "pool": "public"},
            "script": "firmware-bench",
            "params": {
                "firmware": {"url": asset_url},
                "window_minutes": self.cfg.window_minutes,
                "stages": self._stages,
            },
            "secrets": self.cfg.secrets,
        }
        r = client.post(
            f"{self.cfg.base_url}/v1/jobs", headers=self._ctrl_headers(), json=body, timeout=30.0
        )
        r.raise_for_status()
        return r.json()["id"]

    @staticmethod
    def _event_msg(ev: dict[str, Any]) -> str:
        """Extract the human line from one job event row.

        Bench events carry ``payload_json`` like ``{"stream":"bench","msg":"…"}``;
        the verdict (``CHECKIN_VERDICT ok=…``) is one of those ``msg`` lines —
        which is why we read the EVENT stream, not the command-output assets.
        """
        payload = ev.get("payload_json") or ev.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                return payload
        if isinstance(payload, dict):
            return str(payload.get("msg") or payload.get("line") or "")
        return ""

    def _collect(self, client: httpx.Client, job_id: str) -> tuple[str, str]:
        """Long-poll ``/wait`` (paging by ``since``) until terminal; return (state, log).

        Accumulates every event ``msg`` so :meth:`classify` can read the
        ``CHECKIN_VERDICT`` line. Bounded by ``job_timeout_s`` (a flaky-port
        recovery round can take minutes — don't shorten it).
        """
        since = 0
        msgs: list[str] = []
        state = "pending"
        deadline = time.monotonic() + self.cfg.job_timeout_s
        while time.monotonic() < deadline:
            try:
                r = client.get(
                    f"{self.cfg.base_url}/v1/jobs/{job_id}/wait",
                    headers=self._ctrl_headers(),
                    params={"since": since, "timeout": 30},
                    timeout=45.0,
                )
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPError:
                time.sleep(3)
                continue
            for ev in data.get("events", []):
                m = self._event_msg(ev)
                if m:
                    msgs.append(m)
            since = data.get("next_since", since)
            state = data.get("state", state)
            if state in ("finished", "error", "timeout", "cancelled"):
                break
        return state, "\n".join(msgs)

    @staticmethod
    def classify(state: str, log: str) -> Verdict:
        """Map a terminal job (state, event-log) to a Verdict.

        ``CHECKIN_VERDICT ok=true`` → PASS; ``ok=false`` → FAIL (broken but
        flashed/booted); a finished job with NO verdict, or error/timeout → INFRA
        (flash/host problem, retry).
        """
        if "CHECKIN_VERDICT ok=true" in log:
            return Verdict.PASS
        if "CHECKIN_VERDICT ok=false" in log:
            return Verdict.FAIL
        return Verdict.INFRA

    def _run_once(self, release: Release) -> Verdict:
        with httpx.Client() as client:
            job_id = self._submit(client, release.asset_url)
            self.log(f"  job {job_id} ({release.tag}) submitted; waiting…")
            state, log_text = self._collect(client, job_id)
            v = self.classify(state, log_text)
            self.log(f"  {release.tag}: job {state} -> {v.value}")
            return v

    def test_version(self, release: Release) -> Verdict:
        """Test one release, with INFRA retries and the verify-twice agreement guard.

        Runs the pipeline up to ``verify_times`` times; INFRA results are retried
        (up to ``infra_retries`` extra attempts) since they aren't firmware
        verdicts. The PASS/FAIL results across the ``verify_times`` runs must
        agree; a disagreement raises (a genuinely flaky version the operator must
        look at). Raises :class:`BisectError` if it never gets a clean verdict.
        """
        results: list[Verdict] = []
        infra_left = self.cfg.infra_retries
        while len(results) < self.cfg.verify_times:
            v = self._run_once(release)
            if v == Verdict.INFRA:
                if infra_left <= 0:
                    raise BisectError(
                        f"{release.tag}: infrastructure failure persisted "
                        f"(could not flash/boot after {self.cfg.infra_retries} retries)"
                    )
                infra_left -= 1
                self.log(f"  {release.tag}: INFRA — recover + retry ({infra_left} left)")
                continue
            results.append(v)
        if len(set(results)) != 1:
            raise BisectError(
                f"{release.tag}: inconsistent verdicts across {self.cfg.verify_times} runs "
                f"({[r.value for r in results]}) — flaky; needs operator review"
            )
        return results[0]

    def _check_secrets(self) -> None:
        """Fail fast if a cloud-broker stage lacks a real Adafruit IO key.

        A ``write_secrets_msc`` pointing at io.adafruit.com (or .us) needs a real
        account — anonymous / placeholder creds get the MQTT CONNECT rejected, the
        DUT reboot-loops, and every version looks "broken". Catch that at submit
        time with a clear message rather than burning a full flash/test cycle.
        """
        for stage in self._stages:
            if stage.get("type") != "write_secrets_msc":
                continue
            io_url = stage.get("io_url") or ""
            if is_cloud_broker(io_url) and not is_real_io_key(self.cfg.secrets.get("IO_KEY", "")):
                raise BisectError(
                    f"cloud broker {io_url!r} needs a real Adafruit IO account, but none was "
                    "supplied (IO_USERNAME/IO_KEY are empty or placeholder). Pass real creds "
                    "in the request (CLI: IO_USERNAME/IO_KEY env; UI: the IO fields), or target "
                    "the local broker (leave io_url unset) to use anonymous per-job creds."
                )

    def run(self) -> dict[str, Any]:
        """Enumerate, validate the oracle at both endpoints, then bisect."""
        self._check_secrets()
        releases = self.fetch_releases()
        if not releases:
            raise BisectError(f"no releases in {self.cfg.repo} match asset {self.cfg.asset_glob!r}")
        window, wi, bi = select_window(releases, self.cfg.working_ref, self.cfg.broken_ref)
        self.log(
            f"window: {len(window)} releases {window[0].tag}..{window[-1].tag} "
            f"(working={self.cfg.working_ref} broken={self.cfg.broken_ref})"
        )

        self.log(f"oracle: validating working ref {self.cfg.working_ref}")
        if self.test_version(window[wi]) != Verdict.PASS:
            raise BisectError(
                f"oracle invalid: working ref {self.cfg.working_ref} did NOT pass — "
                "the 'working' build isn't actually working (or the test is too strict)"
            )
        self.log(f"oracle: validating broken ref {self.cfg.broken_ref}")
        if self.test_version(window[bi]) != Verdict.FAIL:
            raise BisectError(
                "test criteria were not specific enough, both versions passed "
                f"(broken ref {self.cfg.broken_ref} also PASSED) — see job logs"
            )

        result = bisect(window, wi, bi, self.test_version, log=self.log)
        result["window"] = [r.tag for r in window]
        result["device_id"] = self.cfg.device_id
        result["working_ref"] = self.cfg.working_ref
        result["broken_ref"] = self.cfg.broken_ref
        self.log(
            f"RESULT: first broken release = {result['first_broken']} "
            f"(last good = {result['last_good']})"
        )
        return result
