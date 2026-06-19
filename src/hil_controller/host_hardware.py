"""Auto-detect host hardware (board model, CPU, RAM, storage), live load, and a
work-speed score — so ``/v1/targets`` and the UI stop reporting every SBC as
"pi5" and operators can tell a Pi Zero W apart from a Pi 5 at a glance.

The probes run over a :class:`~hil_controller.hosts.base.HostTransport` (the same
SSH/local transport the job scheduler uses), so this works for the controller
host *and* every nested SBC DUT host in the topology. Detection is the source of
truth, but an operator can pin any field via ``hw_override_json`` — the override
always wins on read (:func:`merge_specs`). That's the "auto-detect + manual
override" contract.

Layering, deliberately:

* **pure parsers** (``parse_*``) take raw command text → dicts, so the awkward
  ``/proc`` formats are unit-tested without a bench;
* **async probes** (``probe_specs`` / ``probe_load`` / ``benchmark_speed``) run a
  command over a transport and feed its stdout to a parser;
* **DB helpers** (``store_*`` / ``load_host_hw``) persist/merge — shared by the
  background :class:`HostHardwareMonitor` and the manual UI refresh endpoints.

The **speed score** is a work-speed multiplier relative to an idle Raspberry Pi
Zero W (=1.0): a fixed CPU benchmark (``sysbench`` if present, else the
universally-available ``openssl speed``) is run, and its throughput divided by
the Zero W baseline. A 4-core Pi 5 lands around 15–25×. The benchmark loads the
box and takes seconds, so it is **never** run on the periodic tick — only on
explicit operator request.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timezone
from typing import Any

from hil_controller.db.connection import get_db, now_iso

log = logging.getLogger(__name__)

#: Markers framing the single combined spec-probe command's output sections.
_M = {
    "model": "@@MODEL@@",
    "cpuinfo": "@@CPUINFO@@",
    "meminfo": "@@MEMINFO@@",
    "maxfreq": "@@MAXFREQ@@",
    "nproc": "@@NPROC@@",
    "df": "@@DF@@",
    "compat": "@@COMPAT@@",
}

#: One read of everything static we want, framed by markers so a missing file
#: (``2>/dev/null``) just yields an empty section instead of failing the probe.
SPECS_CMD = (
    f"printf '{_M['model']}\\n'; cat /proc/device-tree/model 2>/dev/null | tr -d '\\0'; "
    f"printf '\\n{_M['cpuinfo']}\\n'; cat /proc/cpuinfo 2>/dev/null; "
    f"printf '{_M['meminfo']}\\n'; cat /proc/meminfo 2>/dev/null; "
    f"printf '{_M['maxfreq']}\\n'; cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null; "  # noqa: E501
    f"printf '\\n{_M['nproc']}\\n'; nproc 2>/dev/null; "
    f"printf '{_M['df']}\\n'; df -k --output=size / 2>/dev/null | tail -1; "
    # device-tree compatible (nul-separated) — SoC fallback for cpu_model on
    # aarch64 boards whose /proc/cpuinfo carries no "model name"/"Hardware".
    f"printf '\\n{_M['compat']}\\n'; cat /proc/device-tree/compatible 2>/dev/null | tr '\\0' '\\n'"
)

#: loadavg + SoC temperature in one read.
LOAD_CMD = (
    "cat /proc/loadavg 2>/dev/null; printf '@@TEMP@@\\n'; "
    "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null"
)

#: Default Zero-W baselines (the work-speed denominator). The openssl figure is
#: measured: a Raspberry Pi Zero W Rev 1.1 sustains ~29800 k/s on
#: ``openssl speed -evp sha256`` at the largest block (single core). The sysbench
#: figure is a placeholder — calibrate it against a real idle Zero W if you use
#: sysbench (it is not installed on the reference bench). Override both via config.
DEFAULT_BASELINE_OPENSSL = 29800.0  # k bytes/sec, sha256, largest block, 1 core
DEFAULT_BASELINE_SYSBENCH = 50.0  # events/sec, cpu --cpu-max-prime=20000, 1 thread

#: Keys a spec dict carries. Kept explicit so merge/override only touch real fields.
SPEC_KEYS = (
    "model",
    "cpu_model",
    "cpu_cores",
    "cpu_mhz",
    "mem_total_kb",
    "storage_total_kb",
)


# --------------------------------------------------------------------------- #
# Pure parsers                                                                 #
# --------------------------------------------------------------------------- #


def _section(text: str, marker: str, end_markers: list[str]) -> str:
    """Return the text between ``marker`` and the next of ``end_markers`` (or EOF)."""
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = len(text)
    for em in end_markers:
        idx = text.find(em, start)
        if 0 <= idx < end:
            end = idx
    return text[start:end].strip("\n")


def _cpuinfo_field(cpuinfo: str, key: str) -> str | None:
    """First ``key : value`` line in /proc/cpuinfo (case-insensitive key)."""
    for line in cpuinfo.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        if k.strip().lower() == key.lower():
            v = v.strip()
            if v:
                return v
    return None


def parse_specs(text: str) -> dict[str, Any]:
    """Parse the combined :data:`SPECS_CMD` output into a specs dict.

    Tolerant: any section that came back empty (file absent) just leaves its
    field ``None`` rather than failing — a half-probed host still records what
    it could read.
    """
    order = ["model", "cpuinfo", "meminfo", "maxfreq", "nproc", "df", "compat"]
    markers = {k: _M[k] for k in order}

    def sect(name: str) -> str:
        rest = [markers[n] for n in order[order.index(name) + 1 :]]
        return _section(text, markers[name], rest)

    cpuinfo = sect("cpuinfo")
    meminfo = sect("meminfo")

    model = sect("model").strip() or _cpuinfo_field(cpuinfo, "Model")
    cpu_model = _cpuinfo_field(cpuinfo, "model name") or _cpuinfo_field(cpuinfo, "Hardware")
    if not cpu_model:
        # aarch64 SoCs (e.g. the Particle Tachyon) carry no model name/Hardware
        # line; fall back to the device-tree `compatible` list. By convention it
        # runs most-specific-first — entry 0 is the board (duplicates `model`),
        # entry 1 is the SoC: ["particle,tachyon", "qcom,qcm6490", ...] /
        # ["raspberrypi,model-zero-w", "brcm,bcm2835"]. So prefer entry 1, falling
        # back to entry 0 for a single-entry list. An operator override still wins.
        compat = [e.strip() for e in sect("compat").splitlines() if e.strip()]
        if compat:
            cpu_model = compat[1] if len(compat) > 1 else compat[0]

    # Core count: prefer nproc, fall back to counting "processor" lines.
    cores: int | None = None
    nproc_raw = sect("nproc").strip()
    if nproc_raw.isdigit():
        cores = int(nproc_raw)
    else:
        n = len(re.findall(r"(?m)^processor\s*:", cpuinfo))
        cores = n or None

    # Max clock: cpufreq max (kHz → MHz), else cpuinfo "cpu MHz".
    cpu_mhz: float | None = None
    maxfreq = sect("maxfreq").strip()
    if maxfreq.isdigit():
        cpu_mhz = round(int(maxfreq) / 1000.0, 1)
    else:
        cm = _cpuinfo_field(cpuinfo, "cpu MHz")
        if cm:
            try:
                cpu_mhz = round(float(cm), 1)
            except ValueError:
                pass

    mem_total_kb: int | None = None
    m = re.search(r"(?m)^MemTotal:\s*(\d+)\s*kB", meminfo)
    if m:
        mem_total_kb = int(m.group(1))

    storage_total_kb: int | None = None
    df_raw = sect("df").strip().splitlines()
    if df_raw:
        last = df_raw[-1].strip()
        if last.isdigit():
            storage_total_kb = int(last)

    return {
        "model": model or None,
        "cpu_model": cpu_model or None,
        "cpu_cores": cores,
        "cpu_mhz": cpu_mhz,
        "mem_total_kb": mem_total_kb,
        "storage_total_kb": storage_total_kb,
    }


def parse_load(text: str) -> dict[str, Any]:
    """Parse :data:`LOAD_CMD` output → {load1, load5, load15, temp_c}."""
    loadavg, _, temp_part = text.partition("@@TEMP@@")
    out: dict[str, Any] = {"load1": None, "load5": None, "load15": None, "temp_c": None}
    parts = loadavg.split()
    for key, idx in (("load1", 0), ("load5", 1), ("load15", 2)):
        if len(parts) > idx:
            try:
                out[key] = float(parts[idx])
            except ValueError:
                pass
    temp_raw = temp_part.strip()
    if temp_raw and temp_raw.lstrip("-").isdigit():
        millideg = int(temp_raw)
        # thermal_zone temps are millidegrees C; some report plain degrees.
        out["temp_c"] = round(millideg / 1000.0, 1) if abs(millideg) > 1000 else float(millideg)
    return out


def parse_openssl_speed(text: str) -> float | None:
    """Largest-block throughput (k bytes/sec) from ``openssl speed`` output.

    The result row looks like::

        sha256   1236.95k  4504.58k  12596.10k  22735.36k  29409.28k  29802.50k

    We take the last ``...k`` figure on the row carrying the most such figures
    (the cipher/digest line; ``-multi`` header lines have none). Returns the
    aggregate when ``-multi N`` summed across cores.
    """
    best_row: list[float] = []
    for line in text.splitlines():
        nums = re.findall(r"(\d+(?:\.\d+)?)k\b", line)
        if len(nums) > len(best_row):
            best_row = [float(n) for n in nums]
    return best_row[-1] if best_row else None


def parse_sysbench(text: str) -> float | None:
    """events-per-second from ``sysbench cpu ... run`` output."""
    m = re.search(r"events per second:\s*([\d.]+)", text)
    return float(m.group(1)) if m else None


def merge_specs(detected: dict | None, override: dict | None) -> dict[str, Any]:
    """Merge detected specs with operator overrides — override wins per field.

    Only non-empty override values take effect, so pinning ``model`` doesn't
    blank out an un-pinned ``mem_total_kb``. The result always carries every
    :data:`SPEC_KEYS` key (``None`` when neither source has it).
    """
    detected = detected or {}
    override = override or {}
    merged: dict[str, Any] = {k: detected.get(k) for k in SPEC_KEYS}
    for k in SPEC_KEYS:
        ov = override.get(k)
        if ov not in (None, ""):
            merged[k] = ov
    return merged


# --------------------------------------------------------------------------- #
# Async probes (run a command over a transport)                               #
# --------------------------------------------------------------------------- #


async def probe_specs(transport: Any) -> dict[str, Any]:
    """Run the spec probe on a host; returns a specs dict (fields may be None)."""
    res = await transport.exec(["bash", "-lc", SPECS_CMD])
    return parse_specs(getattr(res, "stdout", "") or "")


async def probe_load(transport: Any) -> dict[str, Any]:
    """Run the cheap load probe on a host; returns {load1,load5,load15,temp_c}."""
    res = await transport.exec(["bash", "-lc", LOAD_CMD])
    return parse_load(getattr(res, "stdout", "") or "")


async def _has_cmd(transport: Any, name: str) -> bool:
    try:
        res = await transport.exec(["bash", "-lc", f"command -v {name} >/dev/null 2>&1 && echo y"])
    except Exception:  # noqa: BLE001 — treat an exec failure as "not available"
        return False
    return "y" in (getattr(res, "stdout", "") or "")


async def benchmark_speed(
    transport: Any,
    *,
    baseline_openssl: float = DEFAULT_BASELINE_OPENSSL,
    baseline_sysbench: float = DEFAULT_BASELINE_SYSBENCH,
    seconds: int = 2,
) -> dict[str, Any]:
    """Run a CPU benchmark and return a work-speed score vs an idle Zero W.

    Prefers ``sysbench`` (multi-threaded across all cores) when installed; else
    ``openssl speed`` with ``-multi $(nproc)`` so multi-core hosts report real
    aggregate throughput. ``score`` is the chosen tool's metric divided by the
    matching Zero-W baseline (idle Zero W ≈ 1.0). Returns
    ``{score, tool, metric}`` (``score=None`` if the benchmark produced nothing).
    """
    if await _has_cmd(transport, "sysbench"):
        cmd = (
            f"sysbench cpu --cpu-max-prime=20000 --threads=$(nproc) --time={int(seconds)} run "
            "2>/dev/null"
        )
        res = await transport.exec(["bash", "-lc", cmd])
        metric = parse_sysbench(getattr(res, "stdout", "") or "")
        if metric is not None and baseline_sysbench > 0:
            return {
                "score": round(metric / baseline_sysbench, 2),
                "tool": "sysbench",
                "metric": metric,
            }
        return {"score": None, "tool": "sysbench", "metric": metric}

    cmd = f"openssl speed -evp sha256 -seconds {int(seconds)} -multi $(nproc) 2>/dev/null"
    res = await transport.exec(["bash", "-lc", cmd])
    metric = parse_openssl_speed(getattr(res, "stdout", "") or "")
    if metric is None:
        # Some builds reject -multi; retry single-process.
        res = await transport.exec(
            ["bash", "-lc", f"openssl speed -evp sha256 -seconds {int(seconds)} 2>/dev/null"]
        )
        metric = parse_openssl_speed(getattr(res, "stdout", "") or "")
    if metric is not None and baseline_openssl > 0:
        return {"score": round(metric / baseline_openssl, 2), "tool": "openssl", "metric": metric}
    return {"score": None, "tool": "openssl", "metric": metric}


# --------------------------------------------------------------------------- #
# DB persistence + read-merge                                                  #
# --------------------------------------------------------------------------- #


def _loads(value: Any) -> dict:
    if not value:
        return {}
    try:
        return json.loads(value) or {}
    except (ValueError, TypeError):
        return {}


def host_hw_view(row: dict[str, Any]) -> dict[str, Any]:
    """Build the merged hardware view for a hosts row (for APIs/UI).

    Returns ``{model, cpu_model, cpu_cores, cpu_mhz, mem_total_kb,
    storage_total_kb, load1, load5, load15, temp_c, load_updated_at,
    speed_score, speed_score_at, specs_detected_at}`` with operator overrides
    already applied over detected specs.
    """
    merged = merge_specs(_loads(row.get("hw_detected_json")), _loads(row.get("hw_override_json")))
    load = _loads(row.get("load_json"))
    merged.update(
        {
            "load1": load.get("load1"),
            "load5": load.get("load5"),
            "load15": load.get("load15"),
            "temp_c": load.get("temp_c"),
            "load_updated_at": load.get("updated_at"),
            "speed_score": row.get("speed_score"),
            "speed_score_at": row.get("speed_score_at"),
            "specs_detected_at": row.get("specs_detected_at"),
        }
    )
    return merged


async def store_specs(db_path: str, host_id: str, specs: dict[str, Any]) -> None:
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE hosts SET hw_detected_json = ?, specs_detected_at = ? WHERE id = ?",
            (json.dumps(specs), now_iso(), host_id),
        )
        await db.commit()


async def store_load(db_path: str, host_id: str, load: dict[str, Any]) -> None:
    payload = dict(load)
    payload["updated_at"] = now_iso()
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE hosts SET load_json = ?, last_seen_at = ? WHERE id = ?",
            (json.dumps(payload), payload["updated_at"], host_id),
        )
        await db.commit()


async def store_speed_score(db_path: str, host_id: str, score: float | None) -> None:
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE hosts SET speed_score = ?, speed_score_at = ? WHERE id = ?",
            (score, now_iso(), host_id),
        )
        await db.commit()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def specs_are_stale(row: dict[str, Any], *, max_age_s: int, now: datetime | None = None) -> bool:
    """True if a host has never been spec-probed or its specs are older than max_age_s."""
    if not row.get("hw_detected_json"):
        return True
    detected_at = _parse_iso(row.get("specs_detected_at"))
    if detected_at is None:
        return True
    now = now or datetime.now(UTC)
    return (now - detected_at).total_seconds() >= max_age_s
