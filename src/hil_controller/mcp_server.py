"""Vendor-neutral MCP facade for the HIL controller (stdio or Streamable HTTP)."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ContentBlock, ImageContent, TextContent, ToolAnnotations
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from hil_controller.job_profiles import WIFI_HELP, AgentJob, build_job, job_guide

INSTRUCTIONS = (
    "Run HIL jobs through the existing controller. First call job_guide and list_targets; "
    "read hil-job-api and the relevant current skill before preparing a job. " + WIFI_HELP + " "
    "Production io.adafruit.com, staging io.adafruit.us, and dev-lan require explicitly supplied "
    "IO_USERNAME/IO_KEY. Never request personal credentials for a protoMQ job. "
    "prepare_job previews without running hardware; submit_job acquires hardware and may flash it. "
    "Pass next_since to wait_job; inspect result and proof assets, not just finished state. "
    "Skills are read on demand so updates appear without server restart."
)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
MUTATING = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)


class ControllerClient:
    """Fixed-origin HTTP client; no automatic retries for job submissions."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        url = httpx.URL(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "HIL_API_BASE must be an HTTP(S) controller URL without credentials/query"
            )
        if not token:
            raise ValueError("HIL_API_TOKEN is required (no-secrets refers to DUT credentials)")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport = transport
        self.secret_values: set[str] = {token}

    def sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (
                    "[redacted]"
                    if re.search(r"key|password|token|secret|authorization|pat$", k, re.I)
                    else self.sanitize(v)
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.sanitize(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(self.secret_values, key=len, reverse=True):
                if secret:
                    value = value.replace(secret, "[redacted]")
            return re.sub(r"aio_[A-Za-z0-9_-]+", "[redacted]", value)
        return value

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=65,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return self.sanitize(response.json())
        except httpx.HTTPStatusError as exc:
            # Validation bodies can echo submitted credentials. Do not relay them.
            raise ValueError(
                f"Controller returned HTTP {exc.response.status_code}; "
                "check access, target, and job schema"
            ) from None
        except (httpx.RequestError, ValueError):
            raise ValueError(
                "Controller response unavailable. Submission may have succeeded; "
                "check controller jobs before resubmitting."
            ) from None

    async def asset(self, path: str) -> list[TextContent | ImageContent]:
        try:
            async with (
                httpx.AsyncClient(
                    base_url=self.base_url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=30,
                    follow_redirects=False,
                    transport=self.transport,
                ) as client,
                client.stream("GET", path) as response,
            ):
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError("Asset exceeds 2 MiB; use the authenticated HTTP download")
                mime = response.headers.get("content-type", "").split(";")[0]
                # The existing asset API uses octet-stream for camera artifacts.
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    mime = "image/png"
                elif data.startswith(b"\xff\xd8\xff"):
                    mime = "image/jpeg"
                elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                    mime = "image/webp"
                if mime in {"image/png", "image/jpeg", "image/webp"}:
                    return [
                        ImageContent(
                            type="image", data=base64.b64encode(data).decode(), mimeType=mime
                        )
                    ]
                if mime.startswith("text/") or mime in {"application/json", ""}:
                    return [
                        TextContent(
                            type="text", text=self.sanitize(data.decode("utf-8", "replace"))
                        )
                    ]
                raise ValueError("Binary artifact: use the authenticated HTTP download")
        except (httpx.RequestError, httpx.HTTPStatusError):
            raise ValueError(
                "Asset unavailable; check controller access and job/asset IDs"
            ) from None


class SkillStore:
    """Canonical skills read afresh, with hashes for detecting upcoming updates."""

    def __init__(self, root: Path | None = None):
        checkout = Path(__file__).resolve().parents[2] / ".agent" / "skills"
        bundled = Path(__file__).parent / "agent_skills"
        self.root = (root or (checkout if checkout.is_dir() else bundled)).resolve()

    def catalog(self) -> list[dict[str, str]]:
        return [
            {"name": p.parent.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(self.root.glob("*/SKILL.md"))
            if p.resolve().is_relative_to(self.root)
        ]

    def read(self, name: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError("Choose a skill name from list_skills")
        path = (self.root / name / "SKILL.md").resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Skill is unavailable; call list_skills again")
        return path.read_text(encoding="utf-8")


def _segment(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid controller identifier")
    return quote(value, safe="")


class HilMCP(FastMCP):
    """Validate credential-bearing arguments before SDK error/log formatting."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        if name in {"prepare_job", "submit_job"}:
            try:
                job = AgentJob.model_validate(arguments.get("job"))
            except ValidationError:
                raise ValueError(
                    "Invalid job arguments; consult job_guide request_schema. " + WIFI_HELP
                ) from None
            # Retain SecretStr wrappers for subsequent SDK validation/log formatting.
            arguments = {"job": job}
        return await super().call_tool(name, arguments)


def create_server(
    client: ControllerClient,
    skills: SkillStore | None = None,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    store = skills or SkillStore()
    server = HilMCP(
        "HIL bench",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *(allowed_hosts or [])],
        ),
    )

    @server.tool(name="job_guide", annotations=READ_ONLY)
    def guide() -> dict[str, Any]:
        """Recipes and secrets help. Public Wi-Fi: free4all / password; no personal IO needed."""
        return {**job_guide(), "request_schema": AgentJob.model_json_schema()}

    @server.tool(annotations=READ_ONLY)
    def list_skills() -> list[dict[str, str]]:
        """List the current canonical skills and content hashes; re-read after an update."""
        return store.catalog()

    @server.tool(annotations=READ_ONLY)
    def read_skill(name: str) -> str:
        """Read a live canonical job skill. Start with hil-job-api."""
        return store.read(name)

    @server.resource("hil://skills/{name}")
    def skill_resource(name: str) -> str:
        return store.read(name)

    @server.resource("hil://job-guide")
    def guide_resource() -> str:
        return json.dumps(job_guide())

    @server.tool(annotations=READ_ONLY)
    async def list_targets() -> Any:
        """Discover live bench targets, including unavailable devices and reasons."""
        return await client.request("GET", "/v1/targets")

    @server.tool(annotations=READ_ONLY)
    def prepare_job(job: AgentJob) -> dict[str, Any]:
        """Preview a job without hardware changes; secret values are omitted."""
        client.secret_values.update(v.get_secret_value() for v in job.secrets.values())
        request = build_job(job)
        request["secrets"] = {
            k: "[supplied]" if k in job.secrets else "[public test default]"
            for k in request["secrets"]
        }
        return {
            "request": client.sanitize(request),
            "wifi_help": WIFI_HELP,
            "next": "Call submit_job with the original AgentJob, not this redacted preview.",
        }

    @server.tool(annotations=MUTATING)
    async def submit_job(job: AgentJob) -> Any:
        """Run on real hardware; may flash/reset the selected DUT."""
        request = build_job(job)
        client.secret_values.update(v.get_secret_value() for v in job.secrets.values())
        return await client.request("POST", "/v1/jobs", json=request)

    @server.tool(annotations=MUTATING)
    async def upload_firmware(filename: str, content_base64: str) -> Any:
        """Upload up to 2 MiB; larger firmware uses the HTTP upload API or URL+sha256."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(bin|uf2|hex)", filename):
            raise ValueError("Use a simple .bin, .uf2, or .hex filename")
        if len(content_base64) > 2796204:
            raise ValueError("Firmware exceeds the 2 MiB MCP upload limit")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Firmware content must be valid base64") from None
        if not content or len(content) > 2 * 1024 * 1024:
            raise ValueError("Firmware must be nonempty and at most 2 MiB")
        return await client.request(
            "POST",
            "/v1/firmware",
            params={"filename": filename},
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )

    @server.tool(annotations=READ_ONLY)
    async def get_job(job_id: str) -> Any:
        """Read job state and result; finished alone does not establish test success."""
        return await client.request("GET", f"/v1/jobs/{_segment(job_id)}")

    @server.tool(annotations=READ_ONLY)
    async def wait_job(job_id: str, since: int = 0, timeout: int = 10) -> Any:
        """Poll once (1–30s). Reuse next_since; stop on terminal state and inspect result."""
        if since < 0 or not 1 <= timeout <= 30:
            raise ValueError("since must be nonnegative; timeout must be 1–30 seconds")
        return await client.request(
            "GET", f"/v1/jobs/{_segment(job_id)}/wait", params={"since": since, "timeout": timeout}
        )

    @server.tool(annotations=READ_ONLY)
    async def list_assets(job_id: str) -> Any:
        """List job proof artifacts. Download URLs require the controller bearer token."""
        return await client.request("GET", f"/v1/jobs/{_segment(job_id)}/assets")

    @server.tool(annotations=READ_ONLY)
    async def read_asset(job_id: str, asset_id: str) -> list[TextContent | ImageContent]:
        """Read a proof log or camera image (up to 2 MiB) with controller authentication."""
        return await client.asset(
            f"/v1/jobs/{_segment(job_id)}/assets/{_segment(asset_id)}/download"
        )

    @server.tool(annotations=MUTATING)
    async def cancel_job(job_id: str) -> Any:
        """Cancel this job and let the controller release its hardware lease."""
        return await client.request("POST", f"/v1/jobs/{_segment(job_id)}/cancel")

    return server


class BearerMiddleware:
    """Authenticate every remote MCP request before handing it to the SDK."""

    def __init__(self, app: ASGIApp, token: str):
        if not token:
            raise ValueError("HIL_MCP_TOKEN is required for Streamable HTTP")
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            authorization = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(authorization, self.expected):
                await JSONResponse(
                    {"error": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app(server: FastMCP, token: str) -> Starlette:
    app = server.streamable_http_app()
    if not token:
        raise ValueError("HIL_MCP_TOKEN is required for Streamable HTTP")
    app.add_middleware(BearerMiddleware, token=token)
    return app


def cli() -> None:
    parser = argparse.ArgumentParser(description=INSTRUCTIONS)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)  # stderr, never the stdio protocol stream
    client = ControllerClient(
        os.environ.get("HIL_API_BASE", "http://127.0.0.1:8080"), os.environ.get("HIL_API_TOKEN", "")
    )
    root = os.environ.get("HIL_SKILLS_DIR")
    hosts = [h.strip() for h in os.environ.get("HIL_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    server = create_server(client, SkillStore(Path(root) if root else None), allowed_hosts=hosts)
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        import uvicorn

        uvicorn.run(
            http_app(server, os.environ.get("HIL_MCP_TOKEN", "")),
            host=args.host,
            port=args.port,
            log_level="warning",
        )


if __name__ == "__main__":
    cli()
