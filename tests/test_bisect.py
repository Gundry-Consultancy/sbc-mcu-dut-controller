"""Unit tests for the pure parts of the version-bisection engine.

No network: release JSON is hand-built and ``test_fn`` is an in-memory oracle.
"""

from __future__ import annotations

import pytest

from hil_controller.bisect import (
    BisectRunner,
    Release,
    Verdict,
    bisect,
    parse_releases,
    select_window,
    version_key,
)


def test_classify_reads_checkin_verdict() -> None:
    assert (
        BisectRunner.classify("finished", "…\nCHECKIN_VERDICT ok=true uid=abc\n…") == Verdict.PASS
    )
    assert BisectRunner.classify("finished", "…\nCHECKIN_VERDICT ok=false uid=\n…") == Verdict.FAIL
    # no verdict line (errored before verify_checkin) → infra, retry
    assert BisectRunner.classify("error", "enter_bootloader failed") == Verdict.INFRA
    assert BisectRunner.classify("timeout", "") == Verdict.INFRA


def test_event_msg_extracts_bench_message() -> None:
    ev = {
        "seq": 9,
        "kind": "log",
        "payload_json": '{"stream": "bench", "msg": "CHECKIN_VERDICT ok=true uid=x"}',
    }
    assert BisectRunner._event_msg(ev) == "CHECKIN_VERDICT ok=true uid=x"
    # already-parsed payload + plain-string fallback
    assert BisectRunner._event_msg({"payload": {"msg": "hi"}}) == "hi"
    assert BisectRunner._event_msg({"payload_json": "raw line"}) == "raw line"


def test_version_key_orders_betas_and_finals() -> None:
    assert version_key("1.0.0-beta.78") == (1, 0, 0, 78)
    assert version_key("1.0.0-offline-beta.5") == (1, 0, 0, 5)
    assert version_key("1.0.0") == (1, 0, 0, 1_000_000)
    assert version_key("garbage") is None
    # ordering: beta.9 < beta.10 < final
    assert version_key("1.0.0-beta.9") < version_key("1.0.0-beta.10") < version_key("1.0.0")


def _rel_json(n: int, *, with_asset: bool = True) -> dict:
    assets = []
    if with_asset:
        assets.append(
            {
                "name": f"wippersnapper.pyportal_titano_tinyusb.1.0.0-beta.{n}.uf2",
                "browser_download_url": f"https://x/beta.{n}.uf2",
            }
        )
    # a non-matching asset is always present (other boards)
    assets.append({"name": f"wippersnapper.esp32.1.0.0-beta.{n}.uf2", "browser_download_url": "x"})
    return {"tag_name": f"1.0.0-beta.{n}", "assets": assets}


GLOB = "*pyportal_titano_tinyusb*.uf2"


def test_parse_releases_filters_by_asset_and_sorts() -> None:
    # beta.112 has no titano asset → dropped; input order is shuffled.
    raw = [_rel_json(128), _rel_json(78), _rel_json(112, with_asset=False), _rel_json(100)]
    rels = parse_releases(raw, GLOB)
    assert [r.tag for r in rels] == ["1.0.0-beta.78", "1.0.0-beta.100", "1.0.0-beta.128"]
    assert rels[0].asset_url == "https://x/beta.78.uf2"


def test_select_window_inclusive_and_indices() -> None:
    rels = parse_releases([_rel_json(n) for n in (70, 78, 100, 128, 130)], GLOB)
    window, wi, bi = select_window(rels, "1.0.0-beta.78", "1.0.0-beta.128")
    assert [r.tag for r in window] == [
        "1.0.0-beta.78",
        "1.0.0-beta.100",
        "1.0.0-beta.128",
    ]
    assert window[wi].tag == "1.0.0-beta.78"
    assert window[bi].tag == "1.0.0-beta.128"


def test_select_window_unknown_ref_raises() -> None:
    rels = parse_releases([_rel_json(n) for n in (78, 128)], GLOB)
    with pytest.raises(ValueError, match="not a release"):
        select_window(rels, "1.0.0-beta.78", "1.0.0-beta.999")


def _window(ns: list[int]) -> list[Release]:
    return parse_releases([_rel_json(n) for n in ns], GLOB)


def test_bisect_finds_forward_boundary() -> None:
    # working=78 (low) PASS, broken=128 (high) FAIL; break introduced at beta.120.
    win = _window([78, 90, 100, 110, 120, 125, 128])
    wi, bi = 0, len(win) - 1
    broke_at = (1, 0, 0, 120)

    def test_fn(r: Release) -> Verdict:
        return Verdict.PASS if r.key < broke_at else Verdict.FAIL

    res = bisect(win, wi, bi, test_fn)
    assert res["first_broken"] == "1.0.0-beta.120"
    assert res["last_good"] == "1.0.0-beta.110"
    assert res["direction"] == "forward"


def test_bisect_finds_backward_boundary() -> None:
    # working=128 (high) PASS, broken=78 (low) FAIL — a fix that "broke" going back.
    win = _window([78, 90, 100, 110, 120, 128])
    wi, bi = len(win) - 1, 0  # working is the high end, broken the low end
    fixed_at = (1, 0, 0, 110)  # >=110 works, <110 broken

    def test_fn(r: Release) -> Verdict:
        return Verdict.PASS if r.key >= fixed_at else Verdict.FAIL

    res = bisect(win, wi, bi, test_fn)
    assert res["first_broken"] == "1.0.0-beta.100"  # first broken on the broken side
    assert res["last_good"] == "1.0.0-beta.110"
    assert res["direction"] == "backward"


def test_bisect_adjacent_endpoints_no_interior_tests() -> None:
    win = _window([78, 128])
    calls = []

    def test_fn(r: Release) -> Verdict:
        calls.append(r.tag)
        return Verdict.PASS

    res = bisect(win, 0, 1, test_fn)
    assert res["first_broken"] == "1.0.0-beta.128"
    assert res["last_good"] == "1.0.0-beta.78"
    assert calls == []  # nothing between adjacent endpoints
