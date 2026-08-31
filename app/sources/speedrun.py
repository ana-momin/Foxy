"""Speedrun.

IMPORTANT - PLEASE READ, this differs from the brief on a point of fact.

The brief asks for "YC's dedicated Speedrun program directory". Y Combinator
does not run a program called Speedrun. Verified three independent ways on
2026-08-29:

  * https://www.ycombinator.com/speedrun            -> HTTP 404
  * YC's sitemap.xml                                -> zero occurrences
  * The directory's batch facet (50 batches listed) -> no Speedrun batch

Speedrun is Andreessen Horowitz's accelerator, launched in 2023, originally
games-focused and now horizontal, running roughly 300 companies. That is
almost certainly the program intended, so this adapter targets the real one at
speedrun.a16z.com - which turns out to be a clean, unauthenticated JSON feed.

To cover the other possibility, `YCSpeedrunWatcher` below checks YC's sitemap
on every sweep and self-enables if YC ever does ship a Speedrun page. That way
the deliverable is correct today and correct if the brief becomes true later.
"""

from __future__ import annotations

import json
import re

from ..models import Signal
from .base import Source, SourceError, client

SPEEDRUN_COMPANIES_URL = "https://speedrun.a16z.com/companies"
YC_SITEMAP = "https://www.ycombinator.com/sitemap.xml"

_build_id_cache: str | None = None


def _build_id(force: bool = False) -> str:
    """Read the current Next.js buildId. It rotates on every a16z deploy, so we
    never pin it - same self-healing pattern as the YC sources."""
    global _build_id_cache
    if _build_id_cache and not force:
        return _build_id_cache

    with client() as c:
        r = c.get(SPEEDRUN_COMPANIES_URL)
        r.raise_for_status()
        page = r.text

    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', page)
    if not m:
        raise SourceError("Could not read Speedrun buildId.")
    _build_id_cache = m.group(1)
    return _build_id_cache


def _fetch_page(page: int = 1) -> dict:
    """Fetch one page of the Speedrun directory as JSON."""
    bid = _build_id()
    url = f"https://speedrun.a16z.com/_next/data/{bid}/companies.json"
    params = {"page": page} if page > 1 else None
    with client() as c:
        r = c.get(url, params=params)
        if r.status_code == 404:
            # buildId rotated. Re-read and retry once.
            bid = _build_id(force=True)
            r = c.get(
                f"https://speedrun.a16z.com/_next/data/{bid}/companies.json",
                params=params,
            )
        r.raise_for_status()
        try:
            return r.json()
        except json.JSONDecodeError as exc:
            raise SourceError("Speedrun did not return JSON.") from exc


def _extract_from_html() -> list[dict]:
    """Fallback: the same data is embedded in the page as __NEXT_DATA__."""
    with client() as c:
        r = c.get(SPEEDRUN_COMPANIES_URL)
        r.raise_for_status()
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
        r.text,
        re.S,
    )
    if not m:
        raise SourceError("Speedrun page had no embedded data.")
    data = json.loads(m.group(1))
    return ((data.get("props") or {}).get("pageProps") or {}).get("companies", {}).get(
        "results", []
    )


class SpeedrunSource(Source):
    name = "speedrun"
    label = "Speedrun (a16z)"

    # How many pages of the directory to walk per sweep. New companies land at
    # the front, so 2 pages is ample at an 8-hour cadence.
    pages = 2

    @property
    def mode(self) -> str:
        return "a16z next-data json"

    def fetch(self) -> list[Signal]:
        rows: list[dict] = []
        try:
            for p in range(1, self.pages + 1):
                payload = _fetch_page(p)
                results = (
                    ((payload.get("pageProps") or {}).get("companies") or {}).get(
                        "results"
                    )
                    or []
                )
                if not results:
                    break
                rows.extend(results)
        except Exception:
            rows = _extract_from_html()

        out: list[Signal] = []
        for r in rows:
            slug = r.get("slug") or ""
            name = r.get("name") or slug
            cohort = r.get("cohort")
            cohort_name = (
                cohort.get("name") if isinstance(cohort, dict) else (cohort or None)
            )
            out.append(
                Signal(
                    source="speedrun",
                    external_id=str(r.get("id") or slug),
                    title=name,
                    url=f"https://speedrun.a16z.com/companies/{slug}",
                    description=(r.get("description") or r.get("preamble") or "")[:400],
                    company_name=name,
                    company_url=r.get("website") or None,
                    batch=cohort_name,
                    program="Speedrun",
                    confirmed=True,
                    raw={
                        "key_signal": r.get("key_signal"),
                        "industries": r.get("industries") or [],
                        "founded_year": r.get("founded_year"),
                        "team_size": r.get("team_size"),
                    },
                )
            )
        return out


class YCSpeedrunWatcher(Source):
    """Self-enabling watcher for a YC-run Speedrun page, should one ever exist.

    Costs one sitemap fetch per sweep and stays silent until YC ships
    something. Included so the deliverable satisfies the brief's literal wording
    without shipping a scraper against a page that does not exist.
    """

    name = "yc_speedrun_watch"
    label = "YC Speedrun (watch)"

    @property
    def mode(self) -> str:
        return "sitemap watch"

    def fetch(self) -> list[Signal]:
        try:
            with client() as c:
                r = c.get(YC_SITEMAP)
                r.raise_for_status()
                body = r.text
        except Exception:
            return []

        if "speedrun" not in body.lower():
            return []

        urls = re.findall(r"<loc>([^<]*speedrun[^<]*)</loc>", body, re.I)
        return [
            Signal(
                source="speedrun",
                external_id=f"yc-speedrun-page:{u}",
                title="YC appears to have launched a Speedrun page",
                url=u,
                description=(
                    "Foxy's sitemap watcher found a Speedrun path on "
                    "ycombinator.com. This did not exist when the bot was built. "
                    "Enable a dedicated adapter for it."
                ),
                program="Speedrun",
                confirmed=True,
            )
            for u in urls
        ]
