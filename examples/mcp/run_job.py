"""Run an AgentJob through an MCP subprocess and save its proof assets."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    # The SDK intentionally does not inherit arbitrary environment variables.
    env = {
        key: os.environ[key]
        for key in ("HIL_API_BASE", "HIL_API_TOKEN", "HIL_SKILLS_DIR", "SSH_AUTH_SOCK")
        if key in os.environ
    }
    server = StdioServerParameters(command=args.command, args=args.server_args, env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call(name, arguments):
            response = await session.call_tool(name, arguments)
            if response.isError:
                raise RuntimeError(str(response.content))
            if response.structuredContent is not None:
                data = response.structuredContent
                return data.get("result", data)
            return json.loads(response.content[0].text)

        if args.job_id:
            job_id = args.job_id
        else:
            job = json.loads(Path(args.job).read_text(encoding="utf-8-sig"))
            for tool, arguments in [
                ("job_guide", {}),
                ("list_targets", {}),
                ("read_skill", {"name": "hil-job-api"}),
            ]:
                discovery = await call(tool, arguments)
                (output / f"{tool}.json").write_text(
                    json.dumps(discovery, indent=2), encoding="utf-8"
                )
            preview = await call("prepare_job", {"job": job})
            (output / "preview.json").write_text(json.dumps(preview, indent=2), encoding="utf-8")
            submitted = await call("submit_job", {"job": job})
            job_id = submitted["id"]
            (output / "submitted.json").write_text(json.dumps(submitted), encoding="utf-8")
        print(f"job_id={job_id}", flush=True)
        since = 0
        with (output / "events.jsonl").open("a", encoding="utf-8") as events:
            while True:
                update = await call("wait_job", {"job_id": job_id, "since": since, "timeout": 10})
                events.write(json.dumps(update) + "\n")
                events.flush()
                since = update.get("next_since", since)
                print(
                    f"state={update.get('state')} events={len(update.get('events', []))}",
                    flush=True,
                )
                if update.get("state") in {"finished", "failed", "cancelled", "error", "timeout"}:
                    break
                await asyncio.sleep(1)
        result = await call("get_job", {"job_id": job_id})
        (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        # Terminal state can precede cleanup's publication of log assets.
        assets = {}
        for _ in range(15):
            latest = await call("list_assets", {"job_id": job_id})
            if any(a.get("kind") == "log" for a in latest.get("assets", [])) and latest == assets:
                break
            assets = latest
            await asyncio.sleep(1)
        (output / "assets.json").write_text(json.dumps(assets, indent=2), encoding="utf-8")
        for asset in assets.get("assets", []):
            if asset.get("kind") != "log":
                continue
            response = await session.call_tool(
                "read_asset", {"job_id": job_id, "asset_id": asset["id"]}
            )
            for index, content in enumerate(response.content):
                if content.type == "text":
                    (output / f"{asset['id']}-{index}.txt").write_text(
                        content.text, encoding="utf-8"
                    )
        print(json.dumps(result, indent=2), flush=True)
        if result.get("state") != "finished" or result.get("result") != "pass":
            raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    request = parser.add_mutually_exclusive_group(required=True)
    request.add_argument("--job", help="AgentJob JSON file")
    request.add_argument("--job-id", help="Resume observing a submitted job")
    parser.add_argument("--output", required=True)
    parser.add_argument("--command", default="ssh")
    parser.add_argument("server_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.server_args[:1] == ["--"]:
        args.server_args = args.server_args[1:]
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
