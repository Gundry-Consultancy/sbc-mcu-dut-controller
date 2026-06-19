---
name: feedback-skip-upnp-tests
description: tests/test_upnp.py self-skips when miniupnpc is absent — no --ignore flag needed anymore
metadata:
  type: feedback
---

``tests/test_upnp.py`` now uses ``pytest.importorskip("hil_controller.upnp")``
at module load, so it skips cleanly when the optional ``miniupnpc`` dep is not
installed. Just run ``python -m pytest`` — the full suite collects and the upnp
module shows as ``1 skipped``. The old ``--ignore=tests/test_upnp.py``
workaround is obsolete. User direction 2026-06-12: "mark the upnp test skip".

**Why:** ``src/hil_controller/upnp.py`` imports ``miniupnpc`` at module level;
that package isn't in the dev/CI env, so a bare import-time error used to abort
collection of the whole module. ``importorskip`` turns that into a skip instead
of a collection error. Caught originally 2026-06-07; converted to a skip
2026-06-12.

**How to apply:**
- Run the suite plainly: ``python -m pytest`` (480 passed, 1 skipped as of
  2026-06-12).
- If you actually need the UPnP tests to run, ``pip install miniupnpc`` and
  they'll un-skip automatically.
- Don't reintroduce ``--ignore``; the skip guard supersedes it.
