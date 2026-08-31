"""YC Startup Directory - the source of truth.

How this works, and why it is not a fragile scraper:

The directory at ycombinator.com/companies is a Rails + Inertia app whose
company list is served by Algolia. The page injects its own public search
credentials into `window.AlgoliaOpts`. Decoding that key reveals two indices,
and the second one - YCCompany_By_Launch_Date_production - is sorted
newest-first.

So detecting new companies is not a 6,000-company diff. It is: read page 0,
stop at the first company we have already seen.

Two things matter for reliability:

  1. YC ROTATES THE KEY. The widely-copied public key found in older projects
     is already dead (403). We re-extract it from the live page on every run
     and cache it for the process lifetime, so rotation heals itself.
  2. If Algolia is unreachable for any reason, we fall back to the yc-oss
     public mirror, which rebuilds daily from the same index.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re

from ..models import Signal
from .base import Source, SourceError, client

DIRECTORY_URL = "https://www.ycombinator.com/companies"
INDEX_BY_LAUNCH = "YCCompany_By_Launch_Date_production"
MIRROR_URL = "https://yc-oss.github.io/api/companies/all.json"

# How many of the newest companies to pull per sweep. At ~1-3 new companies a
# day, 100 covers a month of downtime without missing anything.
PAGE_SIZE = 100

_creds_cache: tuple[str, str] | None = None


def extract_algolia_credentials(force: bool = False) -> tuple[str, str]:
    """Scrape the live public search credentials out of the directory page.

    Returns (app_id, api_key). These are public, client-side credentials that
    the website hands to every visitor's browser - the same ones the search box
    uses. Cached per process; call with force=True to re-read after a 403.
    """
    global _creds_cache
    if _creds_cache and not force:
        return _creds_cache

    with client() as c:
        r = c.get(DIRECTORY_URL)
        r.raise_for_status()
        page = r.text

    m = re.search(r"window\.AlgoliaOpts\s*=\s*(\{.*?\});", page, re.S)
    if not m:
        raise SourceError(
            "Could not find AlgoliaOpts on the YC directory page. YC may have "
            "changed their frontend; the yc-oss mirror fallback will be used."
        )
    opts = json.loads(html.unescape(m.group(1)))
    app_id, key = opts.get("app"), opts.get("key")
    if not (app_id and key):
        raise SourceError("YC directory search credentials were empty.")

    _creds_cache = (app_id, key)
    return _creds_cache


def _query_algolia(app_id: str, api_key: str, *, hits: int, page: int = 0) -> dict:
    body = {
        "requests": [
            {
                "indexName": INDEX_BY_LAUNCH,
                "params": f"query=&hitsPerPage={hits}&page={page}",
            }
        ]
    }
    with client(
        headers={
            "X-Algolia-Application-Id": app_id,
            "X-Algolia-API-Key": api_key,
            "Content-Type": "application/json",
        }
    ) as c:
        r = c.post(f"https://{app_id}-dsn.algolia.net/1/indexes/*/queries", json=body)
        r.raise_for_status()
        return r.json()["results"][0]


def _hit_to_signal(h: dict) -> Signal:
    ts = h.get("launched_at")
    posted = (
        dt.datetime.fromtimestamp(ts, dt.timezone.utc) if isinstance(ts, (int, float)) else None
    )
    slug = h.get("slug") or ""
    return Signal(
        source="yc_directory",
        external_id=str(h.get("id") or h.get("objectID") or slug),
        title=h.get("name") or slug,
        url=f"https://www.ycombinator.com/companies/{slug}",
        description=h.get("one_liner") or h.get("long_description") or "",
        company_name=h.get("name"),
        company_url=h.get("website"),
        batch=h.get("batch"),
        program="YC",
        posted_at=posted,
        confirmed=True,
        raw={
            "team_size": h.get("team_size"),
            "all_locations": h.get("all_locations"),
            "industry": h.get("industry"),
            "tags": h.get("tags") or [],
            "status": h.get("status"),
            "stage": h.get("stage"),
        },
    )


class YCDirectorySource(Source):
    name = "yc_directory"
    label = "YC Directory"

    @property
    def mode(self) -> str:
        return "algolia (live key)"

    def fetch(self) -> list[Signal]:
        try:
            app_id, key = extract_algolia_credentials()
            try:
                res = _query_algolia(app_id, key, hits=PAGE_SIZE)
            except Exception:
                # Most likely a rotated key. Re-scrape once, then retry.
                app_id, key = extract_algolia_credentials(force=True)
                res = _query_algolia(app_id, key, hits=PAGE_SIZE)
            return [_hit_to_signal(h) for h in res.get("hits", [])]
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            return self._from_mirror(reason=str(exc))

    # -- fallback ----------------------------------------------------------

    def _from_mirror(self, reason: str) -> list[Signal]:
        """yc-oss rebuilds the same index daily. Slower to update, but it means
        a YC frontend change never takes this source fully offline."""
        try:
            with client() as c:
                r = c.get(MIRROR_URL)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(
                f"YC directory unavailable via Algolia ({reason}) and mirror ({exc})."
            ) from exc

        rows = data if isinstance(data, list) else data.get("companies", [])
        rows = [r for r in rows if r.get("launched_at")]
        rows.sort(key=lambda r: r.get("launched_at") or 0, reverse=True)
        out = [_hit_to_signal(h) for h in rows[:PAGE_SIZE]]
        for s in out:
            s.add_note("via yc-oss mirror")
        return out


# ---------------------------------------------------------------------------
# Truth-set lookup, used by the early-detection cross-reference
# ---------------------------------------------------------------------------


def search_directory(query: str, hits: int = 20) -> list[dict]:
    """Free-text search of the YC directory. Used to answer 'is this company
    already officially listed?' - the question the whole bot hinges on."""
    app_id, key = extract_algolia_credentials()
    body = {
        "requests": [
            {
                "indexName": "YCCompany_production",
                "params": f"query={httpx_quote(query)}&hitsPerPage={hits}",
            }
        ]
    }
    with client(
        headers={
            "X-Algolia-Application-Id": app_id,
            "X-Algolia-API-Key": key,
            "Content-Type": "application/json",
        }
    ) as c:
        try:
            r = c.post(f"https://{app_id}-dsn.algolia.net/1/indexes/*/queries", json=body)
            r.raise_for_status()
        except Exception:
            app_id2, key2 = extract_algolia_credentials(force=True)
            c.headers["X-Algolia-Application-Id"] = app_id2
            c.headers["X-Algolia-API-Key"] = key2
            r = c.post(f"https://{app_id2}-dsn.algolia.net/1/indexes/*/queries", json=body)
            r.raise_for_status()
        return r.json()["results"][0].get("hits", [])


def httpx_quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")
