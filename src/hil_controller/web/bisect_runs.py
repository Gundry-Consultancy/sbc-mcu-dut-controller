"""In-process registry for version-bisection runs launched from the web UI.

A bisection is a long-lived *orchestration* (it submits many ``firmware-bench``
child jobs), not a single queued job — so the UI tracks it here rather than in
the job table. :class:`BisectRun` holds a live log buffer + status; the runner
executes in a worker thread (``asyncio.to_thread``) because
:class:`hil_controller.bisect.BisectRunner` is synchronous (blocking ``httpx`` +
``time.sleep``), and its ``log`` callback appends to the buffer that the result
page polls. Secrets are never stored on the run — only the (secret-free) summary.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hil_controller.bisect import BisectConfig, BisectError, BisectRunner


@dataclass
class BisectRun:
    id: str
    summary: dict[str, Any]  # device / refs / asset_glob — NO secrets
    status: str = "running"  # running | done | error
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = ""


_RUNS: dict[str, BisectRun] = {}
_LOCK = threading.Lock()


def get_run(run_id: str) -> BisectRun | None:
    with _LOCK:
        return _RUNS.get(run_id)


def list_runs() -> list[BisectRun]:
    with _LOCK:
        return sorted(_RUNS.values(), key=lambda r: r.created_at, reverse=True)


async def start_bisect(cfg: BisectConfig, summary: dict[str, Any]) -> str:
    """Register a run and launch :class:`BisectRunner` on a worker thread.

    Returns the run id immediately; the UI redirects to the result page, which
    polls the growing log. ``cfg`` carries the secrets (used only at runtime, in
    memory); ``summary`` is the secret-free description shown in the UI.
    """
    run_id = uuid.uuid4().hex[:12]
    run = BisectRun(id=run_id, summary=summary, created_at=datetime.now(UTC).isoformat())
    with _LOCK:
        _RUNS[run_id] = run

    def _log(msg: str) -> None:
        with _LOCK:
            run.log.append(msg)

    def _work() -> None:
        try:
            result = BisectRunner(cfg, log=_log).run()
            with _LOCK:
                run.result = result
                run.status = "done"
            _log(
                f"DONE: first_broken={result.get('first_broken')} "
                f"last_good={result.get('last_good')}"
            )
        except BisectError as exc:
            with _LOCK:
                run.error = str(exc)
                run.status = "error"
            _log(f"BISECT FAILED: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface any unexpected failure to the UI
            with _LOCK:
                run.error = f"unexpected: {exc}"
                run.status = "error"
            _log(f"ERROR: {exc}")

    asyncio.create_task(asyncio.to_thread(_work))
    return run_id
