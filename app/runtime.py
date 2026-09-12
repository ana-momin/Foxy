"""Settings that can be changed while Foxy is running.

Everything in `config` comes from the environment, which means changing it is a
deployment. That is right for secrets that never move, and wrong for the search
key: it is the one thing guaranteed to need replacing one day, and needing a
developer to do it is how an agent quietly stops finding things.

So these live in the database, with the environment as the fallback. Stored
beats configured; configured beats nothing. The sweeps run in GitHub Actions and
the console runs on Vercel, so a value set in one has to be visible to the
other, and the database is the only thing they share.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import settings
from .db import meta_get, meta_set, session

log = logging.getLogger("foxy.runtime")

SERPER = "runtime:serper_key"

# Read at most this often. A sweep is a fresh process and sees changes at once;
# a warm web instance picks them up within the minute, which is soon enough for
# something replaced a few times a year.
_TTL = 60.0
_cache: dict[str, tuple[float, str]] = {}


def _stored(key: str) -> str:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    try:
        with session() as s:
            value = meta_get(s, key, "")
    except Exception:  # noqa: BLE001 - a missing database must not break search
        log.debug("could not read %s", key, exc_info=True)
        return ""
    _cache[key] = (time.monotonic(), value)
    return value


def _store(key: str, value: str) -> None:
    with session() as s:
        meta_set(s, key, value.strip())
    _cache.pop(key, None)


def serper_key() -> str:
    """The search key in force: whatever was set here, else the environment."""
    return _stored(SERPER) or settings.serper_api_key


def set_serper_key(value: str) -> None:
    _store(SERPER, value)
    log.info("search key replaced from the console")


def serper_source() -> str:
    """Where the key in force came from, for the console to show."""
    if _stored(SERPER):
        return "console"
    if settings.serper_api_key:
        return "environment"
    return "none"


def snapshot() -> dict[str, Any]:
    key = serper_key()
    return {
        "serper_set": bool(key),
        "serper_source": serper_source(),
        # Enough to tell two keys apart, useless to anyone who sees it.
        "serper_hint": f"…{key[-4:]}" if len(key) > 4 else "",
    }
