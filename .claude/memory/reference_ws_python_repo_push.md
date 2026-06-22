---
name: reference-ws-python-repo-push
description: Where/how to push the Adafruit_Wippersnapper_Python HIL branch (adafruit upstream, via the `tyeth` gh account)
metadata:
  type: reference
---

For the **Adafruit_Wippersnapper_Python** repo (the python-snapper client, separate
from this controller repo), the HIL display-test work lives on branch
`hil-test-suite` and its PR is **adafruit#271**.

- **Push target:** always push to the **adafruit upstream** remote directly — the
  PR head branch lives in `adafruit/Adafruit_Wippersnapper_Python` itself, not a
  fork (user: "always push to adafruit upstream ideally"). The local clone at
  `C:\dev\python\cpython\Adafruit_Wippersnapper_Python` has remotes `tyeth` (a fork,
  currently 404/gone) and `upstream` (adafruit). Use `git push upstream hil-test-suite`.
- **gh account:** push access is via the **`tyeth`** account, NOT `tyeth-ai-assisted`
  (the latter 404s on the adafruit repo). If a push/`gh` call 404s, run
  `gh auth switch --user tyeth` first (the git credential helper uses the active
  gh account).
- **Branch diverges often:** the HIL CI commits proof images then removes them in a
  follow-up `[skip ci]` commit (Arduino-style, for inline PR-comment links), so
  `upstream/hil-test-suite` is usually ahead — `git fetch upstream hil-test-suite`
  then `git rebase upstream/hil-test-suite` before pushing.
- **Test venv:** `.venv` in the repo root (`.venv/Scripts/python.exe` on Windows);
  the package must be `pip install -e .[test]`. Real-display tests gate on
  `WS_REAL_DISPLAY_TEST=1`; without it they skip. Shared webcam-proof helper is
  `test/_hil_webcam.py` (`test/` is on sys.path via conftest). See [[reference-bench-host]].
