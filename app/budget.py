"""How much of the search allowance has been spent.

Serper is the one metered thing Foxy depends on. Everything else - the YC
directory, Launch YC, Speedrun, the X syndication endpoint, GitHub Actions,
Slack - is either free or has no ceiling worth tracking.

Nothing counted it, so running out would not have announced itself. The credits
would simply stop, X and LinkedIn would fall back to the free engines that
mostly return nothing, and early detection would quietly get worse while every
health check still said "ok". Silent degradation is the failure mode this whole
project keeps having, so this counts the calls and says something before the
allowance is gone.

The count is kept in the database rather than in memory: the searches happen in
GitHub Actions, the reading happens on the web app, and those are two different
machines.
"""

from __future__ import annotations

import logging

from .config import settings
from .db import meta_get, meta_set, session

log = logging.getLogger("foxy.budget")

KEY = "serper_calls"

# Warn once past this share of the allowance.
WARN_AT = 0.8
NOTIFIED = "serper_warned"


def record_call(n: int = 1) -> None:
    """Count searches actually sent to serper.

    Never raises: a failure to count must not fail the search that prompted it.
    """
    try:
        with session() as s:
            used = int(meta_get(s, KEY, "0") or 0)
            meta_set(s, KEY, str(used + n))
    except Exception:  # noqa: BLE001 - counting is not worth an outage
        log.debug("could not record search usage", exc_info=True)


def snapshot() -> dict[str, object]:
    """Used, remaining and how close to the edge, for /healthz and the CLI."""
    allowance = max(0, int(settings.serper_allowance or 0))
    try:
        with session() as s:
            used = int(meta_get(s, KEY, "0") or 0)
    except Exception:  # noqa: BLE001
        return {"tracked": False}

    out: dict[str, object] = {"tracked": True, "used": used, "allowance": allowance}
    if allowance:
        out["remaining"] = max(0, allowance - used)
        out["spent_share"] = round(used / allowance, 3)
        out["low"] = used >= allowance * WARN_AT
    return out


def should_warn() -> bool:
    """True once, the first time the allowance goes past the warning mark.

    Once is the point. A warning on every sweep is a warning nobody reads.
    """
    snap = snapshot()
    if not snap.get("tracked") or not snap.get("low"):
        return False
    try:
        with session() as s:
            if meta_get(s, NOTIFIED, "") == "1":
                return False
            meta_set(s, NOTIFIED, "1")
        return True
    except Exception:  # noqa: BLE001
        return False


def reset(allowance_note: str = "") -> None:
    """Start counting again, for when the key is replaced or topped up."""
    with session() as s:
        meta_set(s, KEY, "0")
        meta_set(s, NOTIFIED, "")
        if allowance_note:
            meta_set(s, "serper_key_note", allowance_note)
