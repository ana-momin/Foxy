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


def fetch_all_sources(only: tuple[str, ...] | None = None) -> tuple[dict[str, list[Signal]], dict[str, str]]:
    """Read every enabled source once. Returns (signals, errors-by-source).

    A source that fails is reported and skipped; it never aborts the sweep, and
    the workspaces still get everything the other sources found.
    """
    signals: dict[str, list[Signal]] = {}
    errors: dict[str, str] = {}

    for source in build_sources():
        if not source.enabled or (only is not None and source.name not in only):
            signals[source.name] = []
            continue
        try:
            signals[source.name] = source.fetch()
        except Exception as exc:  # noqa: BLE001 - isolate every source
            log.exception("source %s failed during hosted sweep", source.name)
            signals[source.name] = []
            errors[source.name] = f"{type(exc).__name__}: {exc}"[:300]

    return signals, errors


# The three YC and Speedrun feeds are plain JSON and answer in seconds. X and
# LinkedIn go through paced search providers and take minutes, which is why a
# full sweep cannot run inside a serverless request.
FAST_SOURCES = ("yc_directory", "yc_launches", "speedrun", "yc_speedrun_watch")

# What a brand-new workspace is introduced with, inside a web request.
WELCOME_SOURCES = ("yc_directory", "yc_launches")


def replay(count: int, sources: tuple[str, ...] = ("yc_directory", "yc_launches")) -> int:
    """Forget the most recent N detections so they report again.

    For demonstrating and for testing delivery. The companies and posts are
    real and already collected; only the record of having reported them is
    removed, so the same detections run through the same pipeline.
    """
    from sqlalchemy import select

    from .db import Seen

    freed = 0
    with session() as s:
        for install in installs.active_installs(s):
            for source in sources:
                ids = (
                    s.execute(
                        select(Seen.fingerprint)
                        .where(
                            Seen.source == source,
                            Seen.fingerprint.like(f"{install.namespace}%"),
                        )
                        .order_by(Seen.first_seen.desc())
                        .limit(count)
                    )
                    .scalars()
                    .all()
                )
                if ids:
                    s.query(Seen).filter(Seen.fingerprint.in_(ids)).delete(
                        synchronize_session=False
                    )
                    freed += len(ids)
    return freed


def welcome(install_id: str) -> dict[str, Any]:
    """Deliver a new workspace's first alerts straight away.

    Otherwise the channel sits empty until the next scheduled sweep - up to
    eight hours of a bot that looks broken, which is the worst possible first
    impression and the one thing a new user cannot distinguish from failure.

    Only the fast feeds are read. YC, Launch YC and Speedrun are plain JSON and
    answer in seconds; the paced social searches take minutes and would not fit
    in a web request. The engine's own first-run budget decides how many of them
    to introduce, so this is the scheduled sweep arriving early, not a special
    case with its own rules.
    """
    init_db()

    with session() as s:
        row = installs.get(s, install_id)
        if row is None or not row.active or not row.channel_id:
            return {"ok": False, "reason": "not configured"}
        p = {
            "id": row.id,
            "team": row.team_name,
            "token": row.token,
            "channel": row.channel_id,
            "namespace": row.namespace,
            "confidence": row.confidence,
            "remaining": row.remaining,
        }

    if not p["token"]:
        return {"ok": False, "reason": "stored token could not be decrypted"}

    # Already introduced. Saying so beats sending the same six twice.
    with session() as s:
        from sqlalchemy import func, select

        from .db import Alert

        sent = s.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.ts.isnot(None), Alert.fingerprint.like(f"{p['namespace']}%"))
        ).scalar()
    if sent:
        return {"ok": True, "alerts": 0, "reason": "already introduced"}

    # Nothing has ever been delivered here, so anything already marked seen was
    # marked by an attempt that did not finish - the first try at this ran into
    # the serverless timeout after seeding the seen-set and before sending a
    # word. Left alone, those companies are treated as reported forever and the
    # workspace stays silent. A workspace that has heard nothing has no history
    # worth keeping, so start it clean.
    with session() as s:
        from sqlalchemy import delete

        from .db import Entity, Seen

        stale = s.execute(
            delete(Seen).where(Seen.fingerprint.like(f"{p['namespace']}%"))
        ).rowcount
        s.execute(delete(Entity).where(Entity.entity_key.like(f"{p['namespace']}%")))
    if stale:
        log.info("cleared %d item(s) seeded by an unfinished welcome", stale)

    # The two YC feeds only. They answer in about ten seconds together, and
    # they are what a new user is here for; Speedrun and the paced social
    # searches can wait for the first scheduled sweep.
    signals, errors = fetch_all_sources(WELCOME_SOURCES)

    engine = Engine(
        slack=SlackClient(token=p["token"], target=p["channel"]),
        namespace=p["namespace"],
        min_confidence=p["confidence"],
    )
    engine.max_alerts = min(engine.max_alerts, p["remaining"])
    outcome = engine.sweep(prefetched=signals)
    delivered = len(outcome.delivered)

    with session() as s:
        row = installs.get(s, install_id)
        if row:
            row.last_error = (
                "; ".join(outcome.delivery_errors)[:300]
                if outcome.delivery_errors
                else None
            )
            if delivered:
                row.last_alert_at = dt.datetime.now(dt.timezone.utc)
                row.alerts_used = (row.alerts_used or 0) + delivered

    log.info("welcome sweep for %s delivered %d", p["team"], delivered)
    return {
        "ok": not outcome.delivery_errors,
        "alerts": delivered,
        "errors": outcome.delivery_errors,
        "source_errors": errors,
    }


def run_sweep(only: tuple[str, ...] | None = None) -> dict[str, Any]:
    """One shared fetch, then a delivery pass per workspace.

    `only` restricts which sources are read, so a caller with a short deadline
    can take the fast feeds and skip the paced social searches.
    """
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
                "plan": i.plan,
                "remaining": i.remaining,
                "quota": i.quota,
                "quota_notified": i.quota_notified,
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

    # Fresh sweep, fresh answers.
    from . import crossref

    crossref.clear_cache()

    signals, source_errors = fetch_all_sources(only)
    found = {k: len(v) for k, v in signals.items()}
    log.info("hosted sweep fetched %s", found)

    results = []
    for p in plan:
        entry: dict[str, Any] = {"team": p["team"], "alerts": 0, "plan": p["plan"]}

        # Out of quota: say so once, then stay silent rather than nagging.
        if p["remaining"] <= 0:
            entry["skipped"] = "quota exhausted"
            if not p["quota_notified"]:
                _notify_quota(p)
                entry["notified"] = True
            results.append(entry)
            continue

        # A token that will not decrypt is the one failure that looks exactly
        # like success: the workspace stays "active", the sweep runs, alerts are
        # decided, and nothing is ever sent. Catch it before that happens.
        if not p["token"]:
            entry["error"] = (
                "stored Slack token could not be decrypted - "
                "ENCRYPTION_KEY does not match the one used at install"
            )
            with session() as s:
                row = installs.get(s, p["id"])
                if row:
                    row.last_error = entry["error"]
            log.error("%s: %s", p["team"], entry["error"])
            results.append(entry)
            continue

        try:
            engine = Engine(
                slack=SlackClient(token=p["token"], target=p["channel"]),
                namespace=p["namespace"],
                min_confidence=p["confidence"],
            )
            # Never deliver more than the plan allows, or more than one sweep
            # should ever post. The overflow is still recorded and shows up in
            # the digest, so nothing is lost, it is just not shouted.
            engine.max_alerts = min(engine.max_alerts, p["remaining"])

            outcome = engine.sweep(prefetched=signals)
            sent = len(outcome.delivered)

            # Report deliveries, not decisions. `alerts` is kept alongside so a
            # gap between the two is visible rather than rounded away.
            entry["alerts"] = sent
            entry["decided"] = len(outcome.alerts)
            entry["remaining"] = max(0, p["remaining"] - sent)
            entry["digest"] = len(outcome.digest)
            entry["new"] = {k: v.get("new", 0) for k, v in outcome.per_source.items()}
            if outcome.delivery_errors:
                entry["error"] = "; ".join(outcome.delivery_errors)[:300]
                entry["undelivered"] = outcome.undelivered

            with session() as s:
                row = installs.get(s, p["id"])
                if row:
                    row.last_error = entry.get("error")
                    # Charge the quota for what arrived. An alert nobody
                    # received is not one the workspace has spent.
                    if sent:
                        row.last_alert_at = dt.datetime.now(dt.timezone.utc)
                        row.alerts_used = (row.alerts_used or 0) + sent
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


def _notify_quota(p: dict[str, Any]) -> None:
    """Tell a workspace once that its free allowance is spent."""
    try:
        SlackClient(token=p["token"], target=p["channel"]).post(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":hourglass: *Foxy has used its {p['quota']} "
                            "free alerts.*\n"
                            "Monitoring has paused. Nothing was lost, and it picks up "
                            "again the moment the plan is upgraded."
                        ),
                    },
                }
            ],
            "Foxy: free allowance used",
        )
    except Exception:  # noqa: BLE001 - a workspace we cannot reach is not fatal
        log.warning("could not deliver the quota notice to %s", p["team"])

    with session() as s:
        row = installs.get(s, p["id"])
        if row:
            row.quota_notified = True
