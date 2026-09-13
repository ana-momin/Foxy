"""How much longer GitHub will keep running the sweep.

GitHub switches a scheduled workflow off after 60 days with no commit to the
repository. It sends one email and then waits to be switched back on by hand.
Nothing in Foxy would notice: the site stays up, the console answers, the
sources still read healthy, and no sweep ever runs again.

The sweep pushes an empty commit before that deadline, so in normal operation
the clock never runs out. This module exists so the console can show the clock
anyway - an automatic renewal nobody can see is indistinguishable from one that
has quietly stopped working.

The reading comes from the sweep, which is the only place with both a checkout
and the database. That has a useful property: if the sweep stops, this stops
being refreshed too, and the console says so rather than counting down from a
number that is no longer true.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .db import meta_get, meta_set, session

log = logging.getLogger("foxy.schedule")

AT = "schedule:last_commit_at"
SHA = "schedule:last_commit_sha"

# GitHub's rule. Not ours to change.
LIMIT_DAYS = 60

# When the sweep pushes its empty commit. Held here as well as in the workflow
# so the console can say when the next renewal is due; a test keeps the two
# honest about each other.
RENEW_DAYS = 45

# A reading older than this means the sweep is not running, so the countdown
# below it is stale and should not be trusted.
STALE_HOURS = 30


def _utcnow() -> dt.datetime:
    """Naive UTC, matching how the database stores its timestamps."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _naive(when: dt.datetime) -> dt.datetime:
    if when.tzinfo is not None:
        return when.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return when


def record(when: dt.datetime, sha: str = "") -> None:
    """Note the newest commit, as seen by a runner that has the repository."""
    with session() as s:
        meta_set(s, AT, _naive(when).isoformat())
        meta_set(s, SHA, (sha or "")[:12])
        meta_set(s, f"{AT}:read", _utcnow().isoformat())
    log.info("newest commit recorded: %s", sha[:7] or "unknown")


def _read(key: str) -> dt.datetime | None:
    try:
        with session() as s:
            raw = meta_get(s, key, "")
    except Exception:  # noqa: BLE001 - the console must render without this
        log.debug("could not read %s", key, exc_info=True)
        return None
    if not raw:
        return None
    try:
        # Normalised on the way out, not trusted to have been normalised on the
        # way in: everything here is subtracted from a naive now, and mixing the
        # two raises rather than reading wrong, in production only.
        return _naive(dt.datetime.fromisoformat(raw))
    except ValueError:
        return None


def status() -> dict[str, Any]:
    """What the console shows about the schedule's remaining life."""
    commit = _read(AT)
    if commit is None:
        return {
            "known": False,
            "note": "waiting for the next sweep to report",
        }

    checked = _read(f"{AT}:read")
    stale = checked is None or (_utcnow() - checked) > dt.timedelta(hours=STALE_HOURS)

    age = (_utcnow() - commit).total_seconds() / 86400
    age_days = max(0, int(age))
    left = max(0, LIMIT_DAYS - age_days)
    renew_in = max(0, RENEW_DAYS - age_days)

    try:
        with session() as s:
            sha = meta_get(s, SHA, "")
    except Exception:  # noqa: BLE001
        sha = ""

    return {
        "known": True,
        "last_commit": commit,
        "sha": sha,
        "age_days": age_days,
        "days_left": left,
        "renew_in": renew_in,
        "renews_on": (commit + dt.timedelta(days=RENEW_DAYS)).date(),
        "expires_on": (commit + dt.timedelta(days=LIMIT_DAYS)).date(),
        # How much of the window is spent, for the bar.
        "share": min(1.0, age_days / LIMIT_DAYS),
        # Past the renewal point means the sweep should have pushed and has not.
        "overdue": renew_in == 0,
        "stale": stale,
        "checked": checked,
    }
