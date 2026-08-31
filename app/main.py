"""Pond Protocol V1 agent server + the always-on monitor, in one process.

This is deliberately not "a script with a Pond badge stapled on". The scheduler
that runs the 8-hour sweep and the HTTP endpoints Pond calls are the same
application, sharing the same database, so:

  * Pond's manifest revalidation doubles as the health check the brief asks for
  * `scan_now` from a Pond chat and the scheduled sweep are the same code path
  * one deploy gives you both

Pond Protocol V1 contract (docs.joinpond.ai):

  GET  /manifest        public, no auth, <= 256 KiB
  POST /runs            Bearer + X-Agent-Protocol-Version: 1.0
  GET  /tasks/{id}      same auth, for HTTP 202 polling
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import pathlib
import uuid
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import active_batch_codes, settings
from .db import health_snapshot, init_db, recent_alerts, session
from .engine import Engine, source_modes
from .slack import build_status

log = logging.getLogger("foxy.api")

PROTOCOL_VERSION = "1.0"

app = FastAPI(title="Foxy", docs_url=None, redoc_url=None)
router = APIRouter()

# In-memory task store for HTTP 202 responses. Long sweeps exceed a sensible
# request timeout, so `scan_now` is accepted as a task and polled.
_tasks: dict[str, dict[str, Any]] = {}

# Idempotency: Pond sends Idempotency-Key == run_id and does not retry, but a
# duplicate must return the original result rather than running twice.
_runs: dict[str, dict[str, Any]] = {}

_scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# Errors, in Pond's shape
# ---------------------------------------------------------------------------


def perr(code: str, message: str, status: int, run_id: str | None = None, **details):
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    if run_id:
        body["run_id"] = run_id
    return JSONResponse(status_code=status, content=body)


def _check_auth(authorization: str | None) -> JSONResponse | None:
    """Runtime endpoints require the Access Key. /manifest must not."""
    if not settings.pond_access_key:
        # Not published to Pond yet - refuse rather than run unauthenticated.
        return perr(
            "temporarily_unavailable",
            "This agent has not been configured for Pond yet.",
            503,
        )
    expected = f"Bearer {settings.pond_access_key}"
    if not authorization or authorization.strip() != expected:
        return perr("unauthorized", "A valid Access Key is required.", 401)
    return None


def _check_version(version: str | None) -> JSONResponse | None:
    if not version:
        return perr(
            "invalid_request",
            "The X-Agent-Protocol-Version header is required.",
            400,
        )
    v = version.strip()
    # Exactly Major.Minor. "1.0.1" is malformed; "1.1" is unsupported.
    parts = v.split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return perr("invalid_request", f"Malformed protocol version '{v}'.", 400)
    if v != PROTOCOL_VERSION:
        return perr(
            "unsupported_protocol_version",
            f"This agent supports Pond Protocol {PROTOCOL_VERSION}.",
            400,
        )
    return None


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_ACTIONS = [
    {
        "id": "scan_now",
        "name": "Run a scan now",
        "description": (
            "Use when the user wants an immediate check of all monitored "
            "sources for new YC or Speedrun companies and early founder "
            "announcements, instead of waiting for the scheduled sweep."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "description": "Limit the scan to these sources. Omit to scan all.",
                    "items": {
                        "type": "string",
                        "enum": [
                            "yc_directory",
                            "yc_launches",
                            "speedrun",
                            "x",
                            "linkedin",
                        ],
                    },
                },
                "post_to_slack": {
                    "type": "boolean",
                    "description": "Whether detections should also be posted to Slack.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "id": "search_early_signals",
        "name": "Find early YC signals",
        "description": (
            "Use when the user wants founders who have announced a YC or "
            "Speedrun acceptance on social media that YC has not yet listed "
            "in its own directory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many signals to return.",
                    "minimum": 1,
                    "maximum": 50,
                },
                "min_confidence": {
                    "type": "number",
                    "description": "Only return signals at or above this confidence.",
                    "minimum": 0,
                    "maximum": 1,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "id": "lookup_company",
        "name": "Check a company against YC",
        "description": (
            "Use when the user wants to know whether a specific company is "
            "already listed in the official YC directory, and its batch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "The company name to check.",
                    "minLength": 1,
                },
                "website": {
                    "type": "string",
                    "description": "The company's website, which matches more reliably than a name.",
                },
            },
            "required": ["company_name"],
            "additionalProperties": False,
        },
    },
    {
        "id": "recent_detections",
        "name": "List recent detections",
        "description": (
            "Use when the user wants the companies this monitor has detected "
            "recently, or wants to review what it has alerted on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many detections to return.",
                    "minimum": 1,
                    "maximum": 50,
                },
                "only_early": {
                    "type": "boolean",
                    "description": "Restrict to early signals YC had not yet confirmed.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "id": "health_check",
        "name": "Report monitor health",
        "description": (
            "Use when the user asks whether the monitor is running correctly, "
            "when it last ran, or whether any source is failing."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


@router.get("/manifest")
def manifest() -> dict[str, Any]:
    """Public discovery document. No auth, no version header - per spec."""
    return {
        "protocol": "marketplace-agent",
        "protocol_version": PROTOCOL_VERSION,
        "agent_version": settings.agent_version,
        "metadata": {
            "name": "Foxy — YC Launch Monitor",
            "short_description": (
                "Detects new Y Combinator and Speedrun companies, and catches "
                "founders who announce before YC does."
            ),
            "description": (
                "<p>Foxy continuously monitors the YC startup directory, "
                "Launch YC, the a16z Speedrun directory, X and LinkedIn. It "
                "alerts on new YC and Speedrun companies, and — the point of "
                "the tool — on founders announcing an acceptance that YC has "
                "not yet published, cross-referenced against the official "
                "directory so you know it is genuinely early.</p>"
            ),
            "category": "productivity",
            "key_features": (
                "<ul>"
                "<li>Five sources monitored on a schedule, with per-source health</li>"
                "<li>Early detection: founder posts cross-referenced against the YC directory</li>"
                "<li>Persistent state, so nothing alerts twice</li>"
                "<li>Slack alerts with the founder's own words and a link</li>"
                "<li>Confirmation tracking: reports how many days it beat YC by</li>"
                "</ul>"
            ),
            "use_cases": (
                "<p>Sales and GTM teams reaching out to newly funded founders "
                "before the rest of the market notices them.</p>"
            ),
            "setup_instructions": (
                "Ask for a scan, for recent early signals, or whether a "
                "specific company is already listed by YC."
            ),
            "faqs": [
                {
                    "question": "Does this run continuously?",
                    "answer": (
                        "<p>Yes. It sweeps every 8 hours by default and keeps "
                        "persistent state, so a company is never reported "
                        "twice.</p>"
                    ),
                },
                {
                    "question": "What counts as an early signal?",
                    "answer": (
                        "<p>A founder announcing a YC or Speedrun acceptance on "
                        "X or LinkedIn where the company is not yet present in "
                        "the official YC directory.</p>"
                    ),
                },
            ],
        },
        "actions": _ACTIONS,
        "capabilities": {
            "sync": True,
            "streaming": False,
            "async_tasks": True,
            "cancellation": False,
            "attachments": False,
            "feedback": False,
        },
        "input_modes": ["text/plain"],
        "output_modes": ["text/markdown"],
        "limits": {
            "max_request_bytes": 262144,
            "max_execution_ms": 600000,
        },
    }


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def _usage(quantity: int) -> dict[str, Any]:
    """Pond requires cumulative usage on every terminal response."""
    return {"unit_of_measurement": "result", "quantity": max(0, int(quantity))}


def _ok(run_id: str, markdown: str, quantity: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": "completed",
        "output": [{"type": "text", "text": markdown}],
        "usage": _usage(quantity),
    }


@router.post("/runs")
async def runs(
    request: Request,
    authorization: str | None = Header(default=None),
    x_agent_protocol_version: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
):
    if (err := _check_version(x_agent_protocol_version)) is not None:
        return err
    if (err := _check_auth(authorization)) is not None:
        return err

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return perr("invalid_request", "The request body must be valid JSON.", 400)

    run_id = body.get("run_id") or str(uuid.uuid4())
    action_id = body.get("action_id")
    params = body.get("parameters") or {}

    # Idempotency: return the stored result for a repeated run_id.
    key = idempotency_key or run_id
    if key in _runs:
        return _runs[key]

    known = {a["id"] for a in _ACTIONS}
    if action_id not in known:
        return perr(
            "unsupported_operation",
            f"Unknown action '{action_id}'. Supported: {', '.join(sorted(known))}.",
            400,
            run_id=run_id,
        )

    try:
        if action_id == "scan_now":
            # A full sweep can take minutes, so accept it as a task.
            task_id = f"task_{uuid.uuid4().hex[:16]}"
            _tasks[task_id] = {
                "run_id": run_id,
                "task_id": task_id,
                "status": "queued",
                "created_at": dt.datetime.now(dt.timezone.utc),
            }
            asyncio.create_task(_run_scan(task_id, params))
            return JSONResponse(
                status_code=202,
                content={
                    "run_id": run_id,
                    "task_id": task_id,
                    "status": "queued",
                    "poll_after_ms": 5000,
                },
            )

        if action_id == "lookup_company":
            name = (params.get("company_name") or "").strip()
            if not name:
                return perr(
                    "invalid_input",
                    "A company name is required.",
                    422,
                    run_id=run_id,
                    field="parameters.company_name",
                )
            result = _do_lookup(name, params.get("website"))
        elif action_id == "search_early_signals":
            result = _do_early(
                int(params.get("limit") or 20),
                float(params.get("min_confidence") or 0.0),
            )
        elif action_id == "recent_detections":
            result = _do_recent(
                int(params.get("limit") or 20), bool(params.get("only_early"))
            )
        else:  # health_check
            result = _do_health()

        response = _ok(run_id, result["markdown"], result["count"])
        _runs[key] = response
        return response

    except Exception as exc:  # noqa: BLE001 - never leak internals
        log.exception("run %s failed", run_id)
        return {
            "run_id": run_id,
            "status": "failed",
            "error": {
                "code": "internal_error",
                "message": f"The agent could not complete this request ({type(exc).__name__}).",
            },
            "usage": _usage(0),
        }


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    authorization: str | None = Header(default=None),
    x_agent_protocol_version: str | None = Header(default=None),
):
    if (err := _check_version(x_agent_protocol_version)) is not None:
        return err
    if (err := _check_auth(authorization)) is not None:
        return err

    task = _tasks.get(task_id)
    if not task:
        return perr("task_not_found", "That task does not exist.", 404)

    if task["status"] in {"queued", "running"}:
        return {
            "run_id": task["run_id"],
            "task_id": task_id,
            "status": task["status"],
            "poll_after_ms": 5000,
        }

    if task["status"] == "failed":
        return {
            "run_id": task["run_id"],
            "task_id": task_id,
            "status": "failed",
            "error": {
                "code": "internal_error",
                "message": task.get("error", "The scan did not complete."),
            },
            "usage": _usage(0),
        }

    return {
        "run_id": task["run_id"],
        "task_id": task_id,
        "status": "completed",
        "output": [{"type": "text", "text": task["markdown"]}],
        "usage": _usage(task.get("count", 0)),
    }


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------


async def _run_scan(task_id: str, params: dict) -> None:
    _tasks[task_id]["status"] = "running"
    try:
        post = params.get("post_to_slack")
        previous = settings.dry_run
        if post is False:
            settings.dry_run = True
        try:
            result = await asyncio.to_thread(Engine().sweep)
        finally:
            settings.dry_run = previous

        lines = ["## Scan complete", ""]
        for name, info in result.per_source.items():
            if info.get("error"):
                lines.append(f"- **{name}** — failed: {info['error'][:120]}")
            else:
                lines.append(f"- **{name}** — {info['found']} seen, {info['new']} new")

        early = [s for s in result.alerts if s.is_early]
        lines += ["", f"**{len(result.alerts)} new detections**, {len(early)} of them early.", ""]
        for s in result.alerts[:25]:
            tag = "EARLY" if s.is_early else "listed"
            lines.append(
                f"- `{tag}` **{s.company_name or s.title}** "
                f"({s.batch or 'batch unknown'}) — {s.source_label} — [link]({s.url})"
            )

        _tasks[task_id].update(
            status="completed", markdown="\n".join(lines), count=len(result.alerts)
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("scan task failed")
        _tasks[task_id].update(
            status="failed",
            error=f"The scan did not complete ({type(exc).__name__}).",
        )


def _do_lookup(name: str, website: str | None) -> dict[str, Any]:
    from . import crossref

    match = crossref.lookup(name, website)
    if match.found and match.company:
        c = match.company
        md = (
            f"**{c.get('name')}** is already listed by YC.\n\n"
            f"- Batch: {c.get('batch')}\n"
            f"- {c.get('one_liner') or ''}\n"
            f"- Website: {c.get('website') or 'unknown'}\n"
            f"- YC profile: https://www.ycombinator.com/companies/{c.get('slug')}\n\n"
            f"_Matched by: {match.reason}_"
        )
    else:
        md = (
            f"**{name}** is not in the YC directory.\n\n"
            f"_{match.reason}_\n\n"
            "If a founder has announced an acceptance, this is an early signal — "
            "YC has not published them yet."
        )
    return {"markdown": md, "count": 1}


def _do_early(limit: int, min_conf: float) -> dict[str, Any]:
    from .db import Alert

    with session() as s:
        rows = (
            s.query(Alert)
            .filter(Alert.kind == "early", Alert.confidence >= min_conf)
            .order_by(Alert.created_at.desc())
            .limit(limit)
            .all()
        )
        items = [(r.payload or {}, r.confidence, r.created_at, r.source) for r in rows]

    if not items:
        return {
            "markdown": (
                "No early signals recorded yet. Early signals appear when a "
                "founder announces a YC or Speedrun acceptance that is not yet "
                "in the official directory."
            ),
            "count": 0,
        }

    lines = ["## Early YC signals", "", "_Founders who announced before YC listed them._", ""]
    for payload, conf, created, source in items:
        lines.append(
            f"- **{payload.get('company') or payload.get('title')}** "
            f"({payload.get('batch') or 'batch unknown'}) — {source} — "
            f"{conf:.0%} confidence — [post]({payload.get('url')})"
        )
        if payload.get("match_reason"):
            lines.append(f"  - _{payload['match_reason']}_")
    return {"markdown": "\n".join(lines), "count": len(items)}


def _do_recent(limit: int, only_early: bool) -> dict[str, Any]:
    with session() as s:
        rows = recent_alerts(s, limit=limit)
        items = [
            (r.kind, r.payload or {}, r.source, r.confidence)
            for r in rows
            if not only_early or r.kind == "early"
        ]

    if not items:
        return {"markdown": "No detections recorded yet.", "count": 0}

    lines = ["## Recent detections", ""]
    for kind, payload, source, conf in items:
        tag = "EARLY" if kind == "early" else kind
        lines.append(
            f"- `{tag}` **{payload.get('company') or payload.get('title')}** "
            f"({payload.get('batch') or 'batch unknown'}) — {source} — "
            f"[link]({payload.get('url')})"
        )
    return {"markdown": "\n".join(lines), "count": len(items)}


def _do_health() -> dict[str, Any]:
    with session() as s:
        snap = health_snapshot(s)
    modes = source_modes()

    lines = [
        "## Foxy health",
        "",
        f"- Sweeps completed: **{snap['sweeps_completed']}**",
        f"- Last sweep: `{snap['last_sweep_at'] or 'never'}`",
        f"- Cadence: every {settings.scan_interval_hours}h",
        f"- Watching: {', '.join(active_batch_codes())}",
        "",
        "### Sources",
    ]
    for name, mode in modes.items():
        info = snap["sources"].get(name)
        if not info:
            lines.append(f"- **{name}** (`{mode}`) — not run yet")
        elif info["ok"]:
            lines.append(
                f"- **{name}** (`{mode}`) — OK, {info['found']} seen, {info['new']} new"
            )
        else:
            lines.append(f"- **{name}** (`{mode}`) — FAILING: {(info['error'] or '')[:120]}")
    return {"markdown": "\n".join(lines), "count": len(modes)}


# ---------------------------------------------------------------------------
# Plain health endpoint, for uptime checks
# ---------------------------------------------------------------------------


@router.get("/healthz")
def healthz() -> dict[str, Any]:
    with session() as s:
        snap = health_snapshot(s)
    degraded = [k for k, v in snap["sources"].items() if not v["ok"]]
    return {
        "status": "degraded" if degraded else "ok",
        "degraded_sources": degraded,
        "agent_version": settings.agent_version,
        **snap,
    }


@router.post("/slack/command")
async def slack_command(request: Request):
    """Handles `/foxy status` and `/foxy scan` from Slack.

    Slack expects a reply within 3 seconds, so a scan is acknowledged
    immediately and runs in the background.
    """
    form = await request.form()
    text = (form.get("text") or "").strip().lower()

    if text.startswith("scan"):
        asyncio.create_task(asyncio.to_thread(_scheduled_sweep))
        return {
            "response_type": "ephemeral",
            "text": "Scan started. New detections will appear in this channel shortly.",
        }

    with session() as s:
        snap = health_snapshot(s)
    modes = source_modes()
    blocks, fallback = build_status(snap, modes)
    return {"response_type": "ephemeral", "blocks": blocks, "text": fallback}


_STATIC = pathlib.Path(__file__).parent / "static" / "index.html"


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the documentation page, so one deploy gives both the site and the
    agent. Falls back to a small JSON pointer if the page is not bundled."""
    if _STATIC.exists():
        return HTMLResponse(_STATIC.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Foxy</h1><p>Agent manifest: <a href='/manifest'>/manifest</a></p>"
    )


app.include_router(router)


# ---------------------------------------------------------------------------
# Scheduler - this is what makes it a monitor rather than an API
# ---------------------------------------------------------------------------


def _scheduled_sweep() -> None:
    try:
        result = Engine().sweep()
        log.info(
            "scheduled sweep: %d alerts, sources=%s",
            len(result.alerts),
            {k: v.get("new") for k, v in result.per_source.items()},
        )
    except Exception:  # noqa: BLE001
        log.exception("scheduled sweep failed")


@app.on_event("startup")
async def _startup() -> None:
    global _scheduler
    logging.basicConfig(level=logging.INFO)
    init_db()

    # Serverless hosts (Vercel) have no long-lived process to hold a
    # scheduler; there the sweep is driven by an external cron instead.
    if os.getenv("DISABLE_SCHEDULER", "").strip() in {"1", "true", "yes"}:
        log.info("scheduler disabled; expecting an external cron to drive sweeps")
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        lambda: asyncio.get_event_loop().run_in_executor(None, _scheduled_sweep),
        "interval",
        hours=settings.scan_interval_hours,
        id="sweep",
        # Run shortly after boot so a fresh deploy proves itself immediately.
        next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    log.info("Foxy started; sweeping every %sh", settings.scan_interval_hours)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _scheduler:
        with contextlib.suppress(Exception):
            _scheduler.shutdown(wait=False)
