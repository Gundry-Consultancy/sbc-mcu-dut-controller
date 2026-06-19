"""Tests for host hardware probing: pure parsers, override merge, speed score."""

from __future__ import annotations

from datetime import UTC

from hil_controller import host_hardware as hh

# Real-ish framed output of SPECS_CMD for a Pi Zero W (1 core) and a Pi 5 (4 core).
_ZEROW = (
    "@@MODEL@@\n"
    "Raspberry Pi Zero W Rev 1.1\n"
    "@@CPUINFO@@\n"
    "processor\t: 0\n"
    "model name\t: ARMv6-compatible processor rev 7 (v6l)\n"
    "Hardware\t: BCM2835\n"
    "Revision\t: 9000c1\n"
    "Model\t\t: Raspberry Pi Zero W Rev 1.1\n"
    "@@MEMINFO@@\n"
    "MemTotal:         437740 kB\n"
    "MemFree:          200000 kB\n"
    "@@MAXFREQ@@\n"
    "1000000\n"
    "@@NPROC@@\n"
    "1\n"
    "@@DF@@\n"
    "15300000\n"
    "@@COMPAT@@\n"
    "raspberrypi,model-zero-w\nbrcm,bcm2835\n"
)

# aarch64 SoC: no model name / Hardware in cpuinfo; SoC named only in device-tree.
_TACHYON = (
    "@@MODEL@@\n"
    "Particle Tachyon\n"
    "@@CPUINFO@@\n"
    "processor\t: 0\n"
    "processor\t: 1\n"
    "BogoMIPS\t: 38.40\n"
    "CPU implementer\t: 0x51\n"
    "@@MEMINFO@@\n"
    "MemTotal:        7404568 kB\n"
    "@@MAXFREQ@@\n"
    "1958400\n"
    "@@NPROC@@\n"
    "8\n"
    "@@DF@@\n"
    "115642664\n"
    "@@COMPAT@@\n"
    "particle,tachyon\nqcom,qcm6490\nqcom,yupik-iot\nqcom,idp\n"
)

_PI5 = (
    "@@MODEL@@\n"
    "Raspberry Pi 5 Model B Rev 1.0\n"
    "@@CPUINFO@@\n"
    "processor\t: 0\n"
    "processor\t: 1\n"
    "processor\t: 2\n"
    "processor\t: 3\n"
    "model name\t: Cortex-A76\n"
    "Model\t\t: Raspberry Pi 5 Model B Rev 1.0\n"
    "@@MEMINFO@@\n"
    "MemTotal:        8235432 kB\n"
    "@@MAXFREQ@@\n"
    "2400000\n"
    "@@NPROC@@\n"
    "4\n"
    "@@DF@@\n"
    "60000000\n"
)


def test_parse_specs_zerow():
    s = hh.parse_specs(_ZEROW)
    assert s["model"] == "Raspberry Pi Zero W Rev 1.1"
    assert s["cpu_cores"] == 1
    assert s["cpu_mhz"] == 1000.0
    assert s["mem_total_kb"] == 437740
    assert s["storage_total_kb"] == 15300000
    assert s["cpu_model"] == "ARMv6-compatible processor rev 7 (v6l)"


def test_parse_specs_pi5_distinct_from_zerow():
    s = hh.parse_specs(_PI5)
    assert s["model"] == "Raspberry Pi 5 Model B Rev 1.0"
    assert s["cpu_cores"] == 4
    assert s["cpu_mhz"] == 2400.0
    assert s["mem_total_kb"] == 8235432


def test_parse_specs_soc_cpu_model_from_device_tree_compatible():
    # No model name/Hardware in cpuinfo -> fall back to first compatible string.
    s = hh.parse_specs(_TACHYON)
    assert s["model"] == "Particle Tachyon"
    # entry 0 is the board (particle,tachyon); entry 1 is the SoC.
    assert s["cpu_model"] == "qcom,qcm6490"
    assert s["cpu_cores"] == 8
    assert s["mem_total_kb"] == 7404568


def test_parse_specs_cpuinfo_wins_over_compatible():
    # When cpuinfo HAS a model name, the device-tree compatible is not used.
    s = hh.parse_specs(_ZEROW)
    assert s["cpu_model"] == "ARMv6-compatible processor rev 7 (v6l)"


def test_parse_specs_tolerates_missing_sections():
    # Only the model section came back; everything else is absent.
    s = hh.parse_specs(
        "@@MODEL@@\nSomeBoard\n@@CPUINFO@@\n@@MEMINFO@@\n@@MAXFREQ@@\n@@NPROC@@\n@@DF@@\n"
    )
    assert s["model"] == "SomeBoard"
    assert s["cpu_cores"] is None
    assert s["mem_total_kb"] is None


def test_parse_load():
    out = hh.parse_load("0.52 0.40 0.31 1/123 4567\n@@TEMP@@\n48312\n")
    assert out["load1"] == 0.52
    assert out["load5"] == 0.40
    assert out["load15"] == 0.31
    assert out["temp_c"] == 48.3


def test_parse_load_missing_temp():
    out = hh.parse_load("1.0 0.9 0.8 2/200 999\n@@TEMP@@\n")
    assert out["load1"] == 1.0
    assert out["temp_c"] is None


def test_parse_openssl_speed_takes_largest_block():
    text = (
        "Doing sha256 ops ...\n"
        "version: 3.0.19\n"
        "type             16 bytes     64 bytes    256 bytes   1024 bytes   8192 bytes  16384 bytes\n"  # noqa: E501
        "sha256            1236.95k     4504.58k    12596.10k    22735.36k    29409.28k    29802.50k\n"  # noqa: E501
    )
    assert hh.parse_openssl_speed(text) == 29802.50


def test_parse_sysbench_events_per_second():
    text = "    events per second:   842.13\nGeneral statistics:\n"
    assert hh.parse_sysbench(text) == 842.13


def test_merge_specs_override_wins_per_field():
    detected = {"model": "pi5-wrong", "cpu_cores": 4, "mem_total_kb": 8000000}
    override = {"model": "Raspberry Pi Zero W"}  # operator corrects only the model
    merged = hh.merge_specs(detected, override)
    assert merged["model"] == "Raspberry Pi Zero W"  # override wins
    assert merged["cpu_cores"] == 4  # detected stands
    assert merged["mem_total_kb"] == 8000000


def test_merge_specs_blank_override_ignored():
    detected = {"model": "Detected Board", "cpu_cores": 2}
    merged = hh.merge_specs(detected, {"model": "", "cpu_cores": None})
    assert merged["model"] == "Detected Board"
    assert merged["cpu_cores"] == 2


def test_merge_specs_handles_none_inputs():
    merged = hh.merge_specs(None, None)
    assert set(merged) == set(hh.SPEC_KEYS)
    assert all(v is None for v in merged.values())


def test_host_hw_view_merges_and_flattens_load():
    import json

    row = {
        "hw_detected_json": json.dumps({"model": "pi5", "cpu_cores": 4}),
        "hw_override_json": json.dumps({"model": "Raspberry Pi 4 Model B"}),
        "load_json": json.dumps(
            {
                "load1": 0.5,
                "load5": 0.4,
                "load15": 0.3,
                "temp_c": 50.0,
                "updated_at": "2026-06-19T00:00:00+00:00",
            }
        ),
        "speed_score": 8.2,
        "speed_score_at": "2026-06-19T00:00:00+00:00",
        "specs_detected_at": "2026-06-19T00:00:00+00:00",
    }
    view = hh.host_hw_view(row)
    assert view["model"] == "Raspberry Pi 4 Model B"  # override wins
    assert view["cpu_cores"] == 4
    assert view["load1"] == 0.5
    assert view["speed_score"] == 8.2


def test_specs_are_stale():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 6, 19, tzinfo=UTC)
    fresh = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(days=2)).isoformat()
    assert hh.specs_are_stale({}, max_age_s=86400, now=now) is True  # never probed
    assert (
        hh.specs_are_stale(
            {"hw_detected_json": "{}", "specs_detected_at": fresh}, max_age_s=86400, now=now
        )
        is False
    )
    assert (
        hh.specs_are_stale(
            {"hw_detected_json": "{}", "specs_detected_at": old}, max_age_s=86400, now=now
        )
        is True
    )
