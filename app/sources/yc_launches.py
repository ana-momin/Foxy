"""Launch YC - a free source that is not in the brief, and should be.

ycombinator.com/launches is where YC founders post their public launch
announcements. It frequently fires BEFORE the company appears in the directory,
which makes it a genuine early signal that costs nothing and carries no ban
risk.

The page is an Inertia app. Sending `X-Inertia: true` plus the current version
hash returns the whole feed as JSON instead of HTML. The version hash rotates
with YC deploys, so - exactly like the directory's Algolia key - we read it off
the live page rather than pinning it.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re

from ..models import Signal
from .base import Source, SourceError, client

LAUNCHES_URL = "https://www.ycombinator.com/launches"
# The Inertia version is global to the YC app, but only some routes render it
# into the HTML. /companies reliably does, so we read it from there.
VERSION_SOURCE_URL = "https://www.ycombinator.com/companies"

_version_cache: str | None = None


def _inertia_version(force: bool = False) -> str:
    """Read the current Inertia asset version from the live page."""
    global _version_cache
    if _version_cache and not force:
        return _version_cache

    with client() as c:
        r = c.get(VERSION_SOURCE_URL)
        r.raise_for_status()
        page = r.text

    # The page embeds its Inertia state in a data-page attribute.
    i = page.find('data-page="')
    if i == -1:
        raise SourceError("YC page did not contain Inertia state.")
    seg = page[i + len('data-page="') :]
    end = seg.find('">')
    decoded = html.unescape(seg[:end])
    obj, _ = json.JSONDecoder().raw_decode(decoded)
    version = obj.get("version")
    if not version:
        raise SourceError("Launch YC page had no Inertia version.")

    _version_cache = version
    return version


def _fetch_feed() -> dict:
    version = _inertia_version()
    # NOTE: do NOT send an `Accept: text/html` header here. With it, YC renders
    # the page shell and the launch list is withheld for the client to fetch.
    # Without it, this route hands back the launch feed as JSON directly.
    headers = {
        "X-Inertia": "true",
        "X-Inertia-Version": version,
        "Accept": "application/json",
    }
    with client(headers=headers) as c:
        r = c.get(LAUNCHES_URL)
        # 409 means the version rotated mid-flight. Re-read and retry once.
        if r.status_code == 409:
            version = _inertia_version(force=True)
            c.headers["X-Inertia-Version"] = version
            r = c.get(LAUNCHES_URL)
        r.raise_for_status()
        try:
            return r.json()
        except json.JSONDecodeError as exc:
            raise SourceError("Launch YC did not return JSON.") from exc


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class YCLaunchesSource(Source):
    name = "yc_launches"
    label = "Launch YC"

    @property
    def mode(self) -> str:
        return "inertia json (live version)"

    def fetch(self) -> list[Signal]:
        data = _fetch_feed()
        # Depending on the route the payload is either the feed itself or
        # nested under props. Handle both so a YC refactor does not break us.
        hits = data.get("hits")
        if hits is None:
            hits = (data.get("props") or {}).get("hits") or []

        out: list[Signal] = []
        for h in hits:
            company = h.get("company") or {}
            slug = h.get("slug") or ""
            name = company.get("name") or (h.get("title") or "").split(":")[0].strip()
            out.append(
                Signal(
                    source="yc_launches",
                    external_id=str(h.get("id") or slug),
                    title=name or h.get("title") or "Untitled launch",
                    url=h.get("search_path")
                    or f"https://www.ycombinator.com/launches/{slug}",
                    description=h.get("tagline") or h.get("title") or "",
                    company_name=name or None,
                    company_url=company.get("url"),
                    batch=company.get("batch"),
                    program="YC",
                    posted_at=_parse_dt(h.get("created_at")),
                    confirmed=True,
                    raw={
                        "headline": h.get("title"),
                        "votes": h.get("total_vote_count"),
                        "tags": company.get("tags") or [],
                        "industry": company.get("industry"),
                    },
                )
            )
        return out
