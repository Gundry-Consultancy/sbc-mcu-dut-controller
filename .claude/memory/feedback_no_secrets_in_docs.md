---
name: feedback-no-secrets-in-docs
description: Never propagate hardcoded secret values into this repo's docs or commit messages, even when describing a cleanup
metadata:
  type: feedback
---

Never paste the literal value of a hardcoded secret (password, token,
key) into this repo's `docs/`, `README`, commit messages, PR
descriptions, or memory files — even when the prose is *about*
cleaning it up.

Refer to it by its **constant name** or **file:line location** only.
For example, write "`vendor/hil-detection/tests/conftest.py:RPI_PASSWORD`
(value not duplicated here)" — never reproduce the value itself.

**Why:** caught 2026-06-07 mid-session. While writing the M4 OQ8
cleanup plan I quoted the literal value of `RPI_PASSWORD` (a constant
in the `hil-detection` submodule's `tests/conftest.py`) verbatim into
`docs/ARCHITECTURE.md`. The submodule already leaks it (that's the
bug we're cleaning up), but our doc didn't need to repeat the value —
that adds a *second* indexable copy in git history. User correction:
"you've also saved a password into a file". The leak was also
pre-existing in `docs/AGENT_HANDOFF.md` and `docs/ARCHITECTURE.md`
from prior sessions; my edit added a third location. Then —
hypocritically — I quoted the value AGAIN inside the first draft of
this memory file. Scrubbed on the second pass. **Lesson within the
lesson: when writing the "don't do X" rule, double-check that the
rule's own text doesn't do X.**

**How to apply:**
- When describing a hardcoded-secret cleanup, reference the constant
  by name + file:line, never reproduce the value.
- When scrubbing a found leak, also scrub the commit message you're
  about to write — a `git log` quoting the value is just as bad as
  the file.
- Scrubbing the working tree does NOT unleak prior history. If a
  value is already on a pushed branch, flag that to the user — only
  the user can authorise a force-push + rewrite.
- This applies to .env keys, API tokens, SSH passphrases, bench
  passwords, anything labeled a secret. If in doubt, redact.
