"""Partial secret masking for logs, transcripts, and stored job records.

Policy (matches the firmware-bench/PR-930 expectation): **commands and their
arguments are always kept** in the logs — they're what makes a run reproducible
and auditable — but any secret *value* (token / key / password) is shown only as
its last 4 characters, prefixed with ``****``. So an operator can confirm *which*
credential was used and eyeball it against the source, without the full secret
landing in the UI, the API responses, or downloaded GH assets.

``mask_secret`` masks one value; ``mask_values`` redacts every occurrence of a
set of known secret values inside a larger blob (a command line, stdout, a
rendered ``secrets.json`` that ``tee`` echoed back, …).
"""

from __future__ import annotations

from collections.abc import Iterable

#: A value shorter than this is too short to safely substring-replace (it would
#: mask incidental matches in normal text), so such values are masked whole.
_MIN_MATCH_LEN = 4


def mask_secret(value: str, *, keep: int = 4) -> str:
    """Return ``value`` with all but its last ``keep`` chars hidden.

    ``"aio_…000EXMP"`` → ``"****EXMP"``. A value of ``keep`` chars or fewer
    reveals nothing — it becomes a bare ``"****"``.
    """
    v = value or ""
    if len(v) <= keep:
        return "****"
    return "****" + v[-keep:]


def mask_values(text: str, values: Iterable[str], *, keep: int = 4) -> str:
    """Replace every occurrence of each secret in ``values`` with its mask.

    Longest values are masked first so a secret that is a substring of another
    (e.g. an anonymous username equal to the first half of a key) can't leave a
    partial reveal. Values shorter than :data:`_MIN_MATCH_LEN` are skipped to
    avoid masking incidental text.
    """
    if not text:
        return text
    uniq = sorted({v for v in values if v and len(v) >= _MIN_MATCH_LEN}, key=len, reverse=True)
    for v in uniq:
        if v in text:
            text = text.replace(v, mask_secret(v, keep=keep))
    return text
