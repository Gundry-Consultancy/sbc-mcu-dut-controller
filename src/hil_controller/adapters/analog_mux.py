"""AnalogMuxAdapter — async HTTP client for the sbc-dut-analog-mux-api device.

The analog I2C-*strand* mux is a networked CircuitPython box (Adafruit ADG729
dual-4:1 analog switches) that physically routes a shared I2C component strand's
SDA/SCL onto exactly one DUT at a time. It exposes a small HTTP API; this adapter
drives the exclusive, break-before-make channel select the controller needs when
a job runs on a DUT that must receive a strand.

Contract (see Gundry-Consultancy/sbc-dut-analog-mux-api-circuitpy):

    POST /api/groups/<group>/select/<channel>   exclusive select (break-before-make)
    POST /api/isolate                            open every switch
    GET  /api/status                             current state

NOTE: this is the *outer* analog strand-to-DUT mux. A strand may itself carry an
on-strand I2C address mux (TCA9548) for its components — that second level is
modelled in the DB (``strand_components.tca_channel``) but driven separately
(the WipperSnapper inject stages), not by this adapter.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0


class AnalogMuxError(RuntimeError):
    """An analog-mux API call failed (transport error or non-2xx response)."""


class AnalogMuxAdapter:
    """Minimal async client for one analog strand-mux box."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_s
        self._client = client  # injectable for tests (uses MockTransport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _request(self, method: str, path: str) -> dict[str, Any]:
        url = self.base_url + path
        try:
            if self._client is not None:
                resp = await self._client.request(
                    method, url, headers=self._headers(), timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(method, url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise AnalogMuxError(f"{method} {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise AnalogMuxError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except Exception:  # noqa: BLE001 - a 2xx with no/invalid JSON is still success
            return {}

    async def select(self, group: str, channel: int) -> dict[str, Any]:
        """Exclusively route the strand to ``group``+``channel`` (break-before-make)."""
        return await self._request("POST", f"/api/groups/{group}/select/{int(channel)}")

    async def isolate(self) -> dict[str, Any]:
        """Open every switch — disconnect the strand from all DUTs."""
        return await self._request("POST", "/api/isolate")

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/status")
