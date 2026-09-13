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

from .db import PondTask, init_db, record, session

log = logging.getLogger("foxy.pond.tasks")

# How long one poll may spend working. Well inside a serverless request limit,
# and short enough that Pond gets a prompt answer either way.
SLICE_SECONDS = 45.0

# A lease outlives one slice, so a crashed worker's task is retried rather than
# stranded, but not by so much that a poll waits pointlessly.
LEASE_SECONDS = 90

# How often one source may be attempted before the scan writes it off.
MAX_ATTEMPTS = 3

# Most an answer may list. Kept well below the free plan's allowance so one
# call cannot spend a large share of what a customer has.
MAX_RESULTS = 10

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
            "attempts": dict(row.attempts or {}),
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

    # A source that outlives the request it is running in would be retried
    # forever: the lease expires, the next poll picks the same source, and the
    # task never terminates. Give up on it after a few tries and move on, so a
    # scan always finishes even when one source cannot.
    attempts = dict(state.get("attempts") or {})
    attempts[name] = attempts.get(name, 0) + 1
    if attempts[name] > MAX_ATTEMPTS:
        progress[name] = {
            "found": 0,
            "new": 0,
            "error": f"gave up after {MAX_ATTEMPTS} attempts",
        }
        _record(task_id, name, progress, findings, attempts)
        return
    _record(task_id, name, progress, findings, attempts, keep_pending=True)

    try:
        # Carried into the engine rather than set on global settings. Two tasks
        # can be advanced concurrently by two polls, and flipping a global
        # between them meant one task could restore the flag while another was
        # mid-delivery - a run told not to post, posting.
        post = state["params"].get("post_to_slack")
        # Its own namespace, thrown away when the task finishes.
        #
        # Every Pond scan used to share the global seen-set, so the first
        # person to run one consumed all 508 detections and everybody
        # afterwards was told "0 new" - correct by the letter of the code and
        # useless as an answer. A chat question asks what is out there, not
        # what has changed since a stranger last looked.
        result = Engine(
            namespace=f"pond:{task_id}:",
            dry_run=True if post is False else None,
        ).sweep(only=(name,))

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

    _record(task_id, name, progress, findings, attempts)


def _record(
    task_id: str,
    name: str,
    progress: dict[str, Any],
    findings: list,
    attempts: dict[str, int],
    *,
    keep_pending: bool = False,
) -> None:
    """Write progress, and renew the lease so the slice keeps its claim."""
    with session() as s:
        row = s.get(PondTask, task_id)
        if row is None:
            return
        if not keep_pending:
            row.pending = [x for x in (row.pending or []) if x != name]
        row.progress = progress
        row.findings = findings
        row.attempts = attempts
        row.count = len(findings)
        row.updated_at = _now().replace(tzinfo=None)
        row.leased_until = _now().replace(tzinfo=None) + dt.timedelta(seconds=LEASE_SECONDS)


def effective_cap(params: dict[str, Any]) -> int:
    """How many results an answer may carry."""
    return max(1, min(MAX_RESULTS, int(params.get("limit") or 3)))


def _finish(task_id: str) -> None:
    with session() as s:
        row = s.get(PondTask, task_id)
        if row is not None:
            found = len(row.findings or [])
            early = sum(1 for f in (row.findings or []) if f.get("early"))
            sources = ", ".join(sorted(row.progress or {})) or "none"
            record(
                "scan",
                subject=f"{found} found",
                detail=f"{early} early · {sources}",
                actor="pond",
            )
    _forget(task_id)
    with session() as s:
        row = s.get(PondTask, task_id)
        if row is None:
            return
        # Usage is what the answer carries, not what the search turned up.
        # Reporting every finding billed eighteen results for a reply that
        # listed three, which is charging for work the customer never saw.
        row.count = min(len(row.findings or []), effective_cap(row.params or {}))
        row.status = "completed"
        row.leased_until = None
        row.updated_at = _now().replace(tzinfo=None)


def _forget(task_id: str) -> None:
    """Drop the scratch rows a run created.

    The findings are already on the task; keeping the seen-set would make the
    next scan report nothing, which is the bug this namespace exists to fix.
    """
    from sqlalchemy import delete

    from .db import Alert, Entity, Seen

    like = f"pond:{task_id}:%"
    try:
        with session() as s:
            s.execute(delete(Seen).where(Seen.fingerprint.like(like)))
            s.execute(delete(Entity).where(Entity.entity_key.like(like)))
            s.execute(delete(Alert).where(Alert.fingerprint.like(like)))
    except Exception:  # noqa: BLE001 - tidying must not fail a finished scan
        log.debug("could not clear scratch rows for %s", task_id, exc_info=True)


def render(state: dict[str, Any]) -> str:
    """The finished task as markdown for Pond."""
    from .config import settings

    progress = state["progress"]
    findings = state["findings"]
    early = [f for f in findings if f["early"]]

    # Three unless asked otherwise. A chat answer is read, not scanned, and
    # twenty-five companies in one message is a wall nobody finishes.
    # Bounded here as well as in the schema: render should not be able to
    # print more than an answer may carry, whatever reaches it.
    cap = effective_cap(state.get("params") or {})
    lines = ["## Scan complete", ""]
    for name, info in progress.items():
        if info.get("error"):
            lines.append(f"- **{name}** · failed: {str(info['error'])[:120]}")
        else:
            # Not "new": a scan gets a fresh namespace, so everything it finds
            # is new to it and the number only ever equalled the first one.
            lines.append(f"- **{name}** · {info.get('found', 0)} checked")

    lines += [
        "",
        f"**{len(findings)} detections**, {len(early)} of them early."
        # Say when the list is shorter than the count, or the two look wrong
        # together and the answer reads as though something went missing.
        + (f" Showing {cap}." if len(findings) > cap else ""),
        "",
    ]
    for f in findings[:cap]:
        tag = "EARLY" if f["early"] else "listed"
        batch = f["batch"] or "batch unknown"
        lines.append(
            f"- `{tag}` **{f['company']}** ({batch}) · {f['source']} · [link]({f['url']})"
        )

    # Someone asked for Slack from a Pond conversation, which cannot reach
    # one: nothing here identifies a Slack workspace, so the request was
    # honoured as far as it could be and the rest has to be said rather than
    # implied. A user told "Slack notifications on" who then gets nothing in
    # Slack has been misled, however good the results in front of them are.
    if state.get("params", {}).get("post_to_slack"):
        base = settings.public_base_url or "https://tryfoxy.vercel.app"
        lines += [
            "",
            "_These results are here only. To get them in Slack as they appear, install Foxy in your workspace: " + base + "_",
        ]
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
    # Everything. The conversation offers "a scan across all sources", and a
    # default that quietly skipped X and LinkedIn made that a false promise -
    # which is also why an unscoped scan never found an early signal, the one
    # thing people come for.
    return list(FAST) + list(SLOW)
