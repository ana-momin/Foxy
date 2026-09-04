"""Durable, resumable execution for Pond's asynchronous actions.

Pond accepts a long action as a task and then polls it. The old implementation
kept those tasks in a module-level dict and ran the work in a background
`asyncio` task. On a serverless host neither half holds:

* each request may land on a different instance, so a poll could reach a worker
  that had never heard of the task;
* an instance is frozen once it has responded, so the background work stopped
  mid-sweep and the database connection it was holding died with it. That is
  the `OperationalError` at 166 seconds.

So the work is driven *by the polls themselves*. Each poll takes a lease, does
as much as it can inside a time budget that comfortably fits a request, writes
what it finished to the database, and returns. The next poll picks up where the
last one stopped, on whichever instance happens to answer it. Nothing depends
on an instance staying warm, and no single request runs long enough to lose its
connection.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from typing import Any

from .config import settings
from .db import PondTask, init_db, session

log = logging.getLogger("foxy.pond.tasks")

# How long one poll may spend working. Well inside a serverless request limit,
# and short enough that Pond gets a prompt answer either way.
SLICE_SECONDS = 45.0

# A lease outlives one slice, so a crashed worker's task is retried rather than
# stranded, but not by so much that a poll waits pointlessly.
LEASE_SECONDS = 90

# Sources that answer in seconds. The paced social searches take minutes, and
# are only read when the caller asks for them by name.
FAST = ("yc_directory", "yc_launches", "speedrun", "yc_speedrun_watch")
SLOW = ("x", "linkedin")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def create(run_id: str, action_id: str, params: dict[str, Any], sources: list[str]) -> str:
    """Record a task and return its id. Does no work."""
    init_db()
    task_id = f"task_{uuid.uuid4().hex[:16]}"
    with session() as s:
        s.add(
            PondTask(
                task_id=task_id,
                run_id=run_id,
                action_id=action_id,
                status="queued",
                params=params,
                pending=list(sources),
                progress={},
                findings=[],
            )
        )
    return task_id


def get(task_id: str) -> dict[str, Any] | None:
    """Read a task's state. Returns plain data, not an ORM row."""
    init_db()
    with session() as s:
        row = s.get(PondTask, task_id)
        if row is None:
            return None
        return {
            "task_id": row.task_id,
            "run_id": row.run_id,
            "action_id": row.action_id,
            "status": row.status,
            "params": dict(row.params or {}),
            "pending": list(row.pending or []),
            "progress": dict(row.progress or {}),
            "findings": list(row.findings or []),
            "count": row.count or 0,
            "error": row.error,
            "leased": bool(row.leased_until and row.leased_until > _now().replace(tzinfo=None)),
        }


def _take_lease(task_id: str) -> bool:
    """Claim the right to work on this task. False if someone else holds it."""
    with session() as s:
        row = s.get(PondTask, task_id)
        if row is None or row.status in {"completed", "failed"}:
            return False
        now = _now().replace(tzinfo=None)
        if row.leased_until and row.leased_until > now:
            return False
        row.leased_until = now + dt.timedelta(seconds=LEASE_SECONDS)
        row.status = "running"
        row.updated_at = now
    return True


def _release(task_id: str) -> None:
    with session() as s:
        row = s.get(PondTask, task_id)
        if row is not None and row.status not in {"completed", "failed"}:
            row.leased_until = None


def advance(task_id: str) -> None:
    """Do as much of the task as fits in one slice.

    Safe to call from any instance and from concurrent polls: the lease means
    only one of them works, and the others simply report current state.
    """
    if not _take_lease(task_id):
        return

    started = time.monotonic()
    try:
        while time.monotonic() - started < SLICE_SECONDS:
            state = get(task_id)
            if state is None or state["status"] in {"completed", "failed"}:
                return
            if not state["pending"]:
                _finish(task_id)
                return
            _do_one_source(task_id, state)
    except Exception as exc:  # noqa: BLE001 - a task must fail cleanly, not vanish
        log.exception("task %s failed", task_id)
        with session() as s:
            row = s.get(PondTask, task_id)
            if row is not None:
                row.status = "failed"
                row.error = f"{type(exc).__name__}: {exc}"[:300]
                row.leased_until = None
    finally:
        _release(task_id)


def _do_one_source(task_id: str, state: dict[str, Any]) -> None:
    """Scan exactly one source and record what it produced.

    One source per step is what makes the task resumable. It is also why a
    failing source cannot take the whole scan down with it: the failure is
    recorded against that source and the scan moves on.
    """
    from .engine import Engine

    name = state["pending"][0]
    progress = dict(state["progress"])
    findings = list(state["findings"])

    try:
        post = state["params"].get("post_to_slack")
        previous = settings.dry_run
        if post is False:
            settings.dry_run = True
        try:
            result = Engine().sweep(only=(name,))
        finally:
            settings.dry_run = previous

        info = result.per_source.get(name, {})
        progress[name] = {
            "found": info.get("found", 0),
            "new": info.get("new", 0),
            "error": info.get("error"),
        }
        for sig in result.alerts:
            findings.append(
                {
                    "early": bool(sig.is_early),
                    "company": sig.company_name or sig.title,
                    "batch": sig.batch or "",
                    "source": sig.source_label,
                    "url": sig.url,
                    "confidence": round(float(sig.confidence), 2),
                }
            )
    except Exception as exc:  # noqa: BLE001 - isolate the source, keep the scan
        log.warning("source %s failed inside task %s: %s", name, task_id, exc)
        progress[name] = {"found": 0, "new": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}

    with session() as s:
        row = s.get(PondTask, task_id)
        if row is None:
            return
        row.pending = [x for x in (row.pending or []) if x != name]
        row.progress = progress
        row.findings = findings
        row.count = len(findings)
        row.updated_at = _now().replace(tzinfo=None)
        # Hold the lease open across the slice.
        row.leased_until = _now().replace(tzinfo=None) + dt.timedelta(seconds=LEASE_SECONDS)


def _finish(task_id: str) -> None:
    with session() as s:
        row = s.get(PondTask, task_id)
        if row is None:
            return
        row.status = "completed"
        row.leased_until = None
        row.updated_at = _now().replace(tzinfo=None)


def render(state: dict[str, Any]) -> str:
    """The finished task as markdown for Pond."""
    progress = state["progress"]
    findings = state["findings"]
    early = [f for f in findings if f["early"]]

    lines = ["## Scan complete", ""]
    for name, info in progress.items():
        if info.get("error"):
            lines.append(f"- **{name}** · failed: {str(info['error'])[:120]}")
        else:
            lines.append(f"- **{name}** · {info.get('found', 0)} seen, {info.get('new', 0)} new")

    lines += [
        "",
        f"**{len(findings)} new detections**, {len(early)} of them early.",
        "",
    ]
    for f in findings[:25]:
        tag = "EARLY" if f["early"] else "listed"
        batch = f["batch"] or "batch unknown"
        lines.append(
            f"- `{tag}` **{f['company']}** ({batch}) · {f['source']} · [link]({f['url']})"
        )
    return "\n".join(lines)


def resolve_sources(requested: list[str] | None) -> list[str]:
    """Which sources a scan should read.

    An explicit request is honoured exactly, including the slow ones - the
    caller asked. With nothing requested, the fast feeds are read, because a
    default scan should answer promptly rather than spend minutes in paced
    search engines.
    """
    if requested:
        return list(requested)
    return list(FAST)
