"""The hosted sweep.

Fetch the five sources once, then hand the same results to every installed
workspace. The cost of an extra workspace is one `chat.postMessage`, not
another set of scrapes, which is the whole reason hosting a handful of
workspaces is cheap.

Each workspace still keeps its own seen-set and its own confidence threshold,
so nobody consumes anybody else's detections and nobody inherits their tuning.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from . import installs
from .config import settings
from .db import init_db, session
from .engine import Engine, build_sources
from .models import Signal
from .slack import SlackClient

log = logging.getLogger("foxy.hosted")


def fetch_all_sources() -> tuple[dict[str, list[Signal]], dict[str, str]]:
    """Read every enabled source once. Returns (signals, errors-by-source).

    A source that fails is reported and skipped; it never aborts the sweep, and
    the workspaces still get everything the other sources found.
    """
    signals: dict[str, list[Signal]] = {}
    errors: dict[str, str] = {}

    for source in build_sources():
        if not source.enabled:
            signals[source.name] = []
            continue
        try:
            signals[source.name] = source.fetch()
        except Exception as exc:  # noqa: BLE001 - isolate every source
            log.exception("source %s failed during hosted sweep", source.name)
            signals[source.name] = []
            errors[source.name] = f"{type(exc).__name__}: {exc}"[:300]

    return signals, errors


def run_sweep() -> dict[str, Any]:
    """One shared fetch, then a delivery pass per workspace."""
    init_db()

    with session() as s:
        targets = installs.active_installs(s)
        # Read what each worker needs now; the ORM objects must not outlive
        # this session, since delivery below opens its own.
        plan = [
            {
                "id": i.id,
                "team": i.team_name,
                "token": i.token,
                "channel": i.channel_id,
                "namespace": i.namespace,
                "confidence": i.confidence,
                "serper": i.serper_key,
                "anthropic": i.anthropic_key,
            }
            for i in targets
        ]

    if not plan:
        return {"installs": 0, "note": "no configured workspaces", "results": []}

    # Any workspace-supplied key improves the shared fetch, so apply the first
    # one available before reading the sources.
    for p in plan:
        if p["serper"] and not settings.serper_api_key:
            settings.serper_api_key = p["serper"]
            os.environ["SERPER_API_KEY"] = p["serper"]
        if p["anthropic"] and not settings.anthropic_api_key:
            settings.anthropic_api_key = p["anthropic"]
        if settings.serper_api_key and settings.anthropic_api_key:
            break

    signals, source_errors = fetch_all_sources()
    found = {k: len(v) for k, v in signals.items()}
    log.info("hosted sweep fetched %s", found)

    results = []
    for p in plan:
        entry: dict[str, Any] = {"team": p["team"], "alerts": 0}
        try:
            engine = Engine(
                slack=SlackClient(token=p["token"], target=p["channel"]),
                namespace=p["namespace"],
                min_confidence=p["confidence"],
            )
            outcome = engine.sweep(prefetched=signals)
            entry["alerts"] = len(outcome.alerts)
            entry["digest"] = len(outcome.digest)
            entry["new"] = {k: v.get("new", 0) for k, v in outcome.per_source.items()}

            with session() as s:
                row = installs.get(s, p["id"])
                if row:
                    row.last_error = None
                    if outcome.alerts:
                        row.last_alert_at = dt.datetime.now(dt.timezone.utc)
        except Exception as exc:  # noqa: BLE001 - one workspace must not break the rest
            log.exception("delivery failed for %s", p["team"])
            entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
            with session() as s:
                row = installs.get(s, p["id"])
                if row:
                    row.last_error = entry["error"]
        results.append(entry)

    return {
        "installs": len(plan),
        "found": found,
        "source_errors": source_errors,
        "results": results,
    }
