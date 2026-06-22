"""Tests for last-4 partial secret masking (hil_controller.redact)."""

from __future__ import annotations

from hil_controller.redact import mask_secret, mask_values


def test_mask_secret_keeps_last_four() -> None:
    assert mask_secret("aio_REDACTEDEXAMPLEKEY000000EXMP") == "****EXMP"
    assert mask_secret("supersecret") == "****cret"


def test_mask_secret_hides_short_values_entirely() -> None:
    assert mask_secret("abcd") == "****"
    assert mask_secret("ab") == "****"
    assert mask_secret("") == "****"


def test_mask_values_redacts_each_occurrence_longest_first() -> None:
    key = "aio_LONGKEYvalue1234"
    user = "aio_LONGKEY"  # a substring of the key — must not leave a partial reveal
    body = f'{{"io_username": "{user}", "io_key": "{key}"}}'
    out = mask_values(body, [key, user])
    assert key not in out
    assert "io_key" in out  # the field name (an arg) is preserved
    assert "****1234" in out  # key → last-4
    assert "****GKEY" in out  # username → last-4


def test_mask_values_skips_short_and_empty() -> None:
    # A 3-char value would mask incidental text, so it's left alone.
    assert mask_values("the cat sat", ["cat", "", None]) == "the cat sat"
