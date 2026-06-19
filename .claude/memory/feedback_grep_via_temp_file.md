---
name: feedback-grep-via-temp-file
description: never pipe command output straight into grep — redirect to a file in the Windows temp folder first, then grep that file
metadata:
  type: feedback
---

Don't pipe command output directly into grep (e.g. `pytest ... | grep ...`).
Instead redirect the output to a file under the Windows temp folder
(`$env:TEMP`, e.g. `$env:TEMP\hil-test.log`), then grep that file. Reuse /
overwrite the same temp file on later runs rather than trying to delete it.
User direction 2026-06-12.

**Why:** two reasons. (1) Piping a big/noisy command (like the full pytest run)
into grep often surfaces traceback/warning lines instead of the summary, and
you lose the full output for re-inspection. (2) I do NOT have file-removal
permission in this environment — `rm`/`del` on a scratch log gets denied — so
self-cleaning patterns like `cmd > log; grep log; rm log` fail on the `rm`.
The Windows temp folder is the right home for throwaway logs: leave them there,
the OS handles cleanup.

**How to apply:**
- Redirect first: ``python -m pytest -p no:warnings > "$env:TEMP\hil-test.log" 2>&1``
  then ``grep -aiE "passed|failed|error|skipped" "$env:TEMP\hil-test.log"``.
- Re-run by overwriting the same path; don't append-and-delete.
- Never tack `; rm <file>` onto the command — removal is denied and blocks the
  whole compound command.
- Applies to any verbose command whose output I want to filter, not just pytest.
