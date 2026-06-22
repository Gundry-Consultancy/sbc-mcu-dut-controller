"""GitDeploy adapter: clone → setup → materialise secrets → run on SBC via SSH transport."""

from __future__ import annotations

import json
import logging
import re
import shlex
from pathlib import PurePosixPath
from typing import Any

log = logging.getLogger(__name__)

# A git ref that is a commit SHA (7-40 hex chars) cannot be used with
# `git clone --branch`; it must be fetched + checked out explicitly.
_SHA_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")


def _is_sha(ref: str) -> bool:
    return bool(_SHA_RE.match(ref or ""))


# Supported format tokens (may be combined with '+', e.g. "json+env")
_FORMATS = frozenset({"env", "json", "dotenv"})


class GitDeployAdapter:
    """Fulfils the DeviceAdapter protocol for SBC (git-source) jobs.

    The adapter is responsible for:
      1. acquire()  — no-op for SBC (no hardware reset needed)
      2. deploy()   — git clone + optional setup + secrets materialisation
      3. run()      — invoke entry point, return 'pass'/'fail'
      4. cleanup()  — rm -rf the work dir
      5. release()  — no-op

    Secrets:
      Pass ``secrets`` as a flat ``{key: value}`` dict.  ``secrets_format``
      controls how they reach the test process:

      * ``"env"``    — injected as subprocess env vars only (nothing on disk)
      * ``"json"``   — written as ``secrets.json`` in the work dir
      * ``"dotenv"`` — written as ``.env`` in the work dir
      * Combine with ``+``, e.g. ``"json+env"``

      Default is ``"env"``.  After the job is done the worker purges the
      values from the DB; the adapter never stores them beyond process memory.
    """

    def __init__(
        self,
        transport: Any,
        job_id: str,
        source: dict[str, Any],
        params: dict[str, Any],
        work_dir: PurePosixPath | None = None,
        secrets_dest: PurePosixPath | None = None,
        secrets: dict[str, str] | None = None,
        secrets_format: str = "env",
    ) -> None:
        self.transport = transport
        self.job_id = job_id
        self.source = source
        self.params = params
        self.work_dir = work_dir or PurePosixPath(f"/tmp/hil/{job_id}")
        self.secrets_dest = secrets_dest
        self._secrets: dict[str, str] = dict(secrets or {})
        self._secrets_format: str = secrets_format
        self._deploy_stdout: str = ""
        self._deploy_stderr: str = ""
        self._run_stdout: str = ""
        self._run_stderr: str = ""
        # Optional per-stdout-line callback for the run phase (set by the worker
        # for HIL capture: it watches WS_HIL_CAPTURE stage markers live).
        self.on_line: Any = None

    async def acquire(self) -> None:
        pass

    async def reset(self) -> None:
        pass

    async def flash(self, artifact: dict) -> None:
        await self.deploy()

    async def open_serial(self):
        return iter([])

    async def release(self) -> None:
        pass

    # ---------------------------------------------------------------------- #
    # SBC-specific operations                                                  #
    # ---------------------------------------------------------------------- #

    async def deploy(self) -> None:
        self._deploy_stdout = ""
        self._deploy_stderr = ""

        repo = self.source["repo"]
        pat = self.source.get("pat")
        if pat and repo.startswith("https://"):
            repo = repo.replace("https://", f"https://{pat}@", 1)
        ref = self.source.get("ref", "main")
        shallow = self.source.get("shallow", True)
        submodules = self.source.get("submodules", False)
        setup = self.source.get("setup") or []

        # mkdir -p workdir
        await self.transport.exec(["mkdir", "-p", str(self.work_dir)])

        wd = str(self.work_dir)
        if _is_sha(ref):
            # `git clone --branch` only accepts a branch/tag name, NOT a commit
            # SHA — and CI commonly passes the exact github.sha. Fetch the commit
            # directly (GitHub allows fetch-by-SHA) and check it out.
            steps: list[list[str]] = [
                ["git", "init", "-q", wd],
                ["git", "-C", wd, "remote", "add", "origin", repo],
                ["git", "-C", wd, "fetch", "--depth", "1"]
                + (["--recurse-submodules"] if submodules else [])
                + ["origin", ref],
                ["git", "-C", wd, "checkout", "-q", "--detach", ref],
            ]
            if submodules:
                steps.append(["git", "-C", wd, "submodule", "update", "--init", "--depth", "1"])
            self._deploy_stdout += (
                f"$ git init && git remote add origin {self.source['repo']} "
                f"&& git fetch --depth 1 origin {ref} && git checkout {ref}\n"
            )
            for cmd in steps:
                result = await self.transport.exec(cmd)
                self._deploy_stdout += result.stdout
                self._deploy_stderr += result.stderr
                if result.exit_status != 0:
                    raise RuntimeError(
                        f"git fetch/checkout of {ref} failed "
                        f"(exit {result.exit_status}): {result.stderr}"
                    )
        else:
            # git clone a branch/tag
            clone_cmd = ["git", "clone"]
            if shallow:
                clone_cmd += ["--depth", "1"]
            if submodules:
                clone_cmd += ["--recurse-submodules"]
            clone_cmd += ["--branch", ref, repo, wd]
            # echo command using original URL (PAT redacted)
            display_clone = ["git", "clone"]
            if shallow:
                display_clone += ["--depth", "1"]
            if submodules:
                display_clone += ["--recurse-submodules"]
            display_clone += ["--branch", ref, self.source["repo"], wd]
            self._deploy_stdout += f"$ {shlex.join(display_clone)}\n"
            result = await self.transport.exec(clone_cmd)
            self._deploy_stdout += result.stdout
            self._deploy_stderr += result.stderr
            if result.exit_status != 0:
                raise RuntimeError(f"git clone failed (exit {result.exit_status}): {result.stderr}")

        # setup command (e.g. pip install)
        if setup:
            self._deploy_stdout += f"$ {shlex.join(setup)}\n"
            result = await self.transport.exec(setup, cwd=str(self.work_dir))
            self._deploy_stdout += result.stdout
            self._deploy_stderr += result.stderr
            if result.exit_status != 0:
                log.warning("setup command exited %d: %s", result.exit_status, result.stderr)

        # materialise secrets files
        if self._secrets:
            fmts = {t.strip() for t in self._secrets_format.split("+")}
            if "json" in fmts:
                await self._write_secrets_json()
            if "dotenv" in fmts:
                await self._write_secrets_dotenv()

    async def run(self) -> str:
        entry = self.params.get("entry", "python")
        args = self.params.get("args", [])
        argv = [entry] + list(args)

        env: dict[str, str] | None = None
        if self._secrets:
            fmts = {t.strip() for t in self._secrets_format.split("+")}
            if "env" in fmts:
                env = dict(self._secrets)

        extra_env = self.params.get("extra_env") or {}
        if extra_env:
            env = {**(env or {}), **extra_env}

        result = await self.transport.exec(
            argv, cwd=str(self.work_dir), env=env, on_line=self.on_line
        )
        self._run_stdout = result.stdout
        self._run_stderr = result.stderr
        log.info("test exit %d", result.exit_status)
        return "pass" if result.exit_status == 0 else "fail"

    async def cleanup(self) -> None:
        await self.transport.exec(["rm", "-rf", str(self.work_dir)])
        if self.secrets_dest:
            await self.transport.exec(["rm", "-f", str(self.secrets_dest)])

    # ---------------------------------------------------------------------- #
    # Secrets materialisation helpers                                          #
    # ---------------------------------------------------------------------- #

    async def _write_secrets_json(self) -> None:
        dest = str(self.work_dir / "secrets.json")
        payload = json.dumps(self._secrets, indent=2).encode()
        await self.transport.exec(["tee", dest], stdin=payload)
        log.debug("wrote secrets.json to %s", dest)

    async def _write_secrets_dotenv(self) -> None:
        dest = str(self.work_dir / ".env")
        lines = "\n".join(f"{k}={v}" for k, v in self._secrets.items()) + "\n"
        await self.transport.exec(["tee", dest], stdin=lines.encode())
        log.debug("wrote .env to %s", dest)
