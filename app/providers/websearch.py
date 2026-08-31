"""Free public-web search, used to discover social posts without paying for a
platform API.

This is the backbone of Foxy's free mode. Rather than hitting X's or
LinkedIn's APIs (which now cost money and, for LinkedIn, carry real account
risk), we ask a search engine for indexed *public* posts matching our
announcement phrases, then hydrate what we find.

BE HONEST ABOUT WHAT THIS IS. Scraping a search engine's HTML is best-effort:

  * DuckDuckGo serves an "anomaly" interstitial (HTTP 202, no results) once it
    decides you have asked too often. It recovers, but not on a schedule you
    control.
  * Bing honours `site:` inconsistently for x.com and returns few post URLs.
  * Neither indexes X exhaustively any more.

So free mode has real but *partial* recall. It will find founder announcements;
it will not find all of them. Set SERPER_API_KEY (2,500 free one-off credits,
Google-quality results) or a paid platform provider when recall matters more
than cost. The engine chain below degrades quietly through every option it has.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from ..config import settings
from ..sources.base import client

log = logging.getLogger("foxy.websearch")

# Signatures of a bot-check / throttle page rather than real results.
_BLOCK_MARKERS = ("anomaly", "unusual traffic", "captcha", "are you a robot")


@dataclass
class WebResult:
    url: str
    title: str = ""
    snippet: str = ""


class SearchUnavailable(RuntimeError):
    """Every engine refused or returned nothing usable."""


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    for a, b in (
        ("&amp;", "&"), ("&quot;", '"'), ("&#x27;", "'"), ("&#39;", "'"),
        ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "),
    ):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def _looks_blocked(body: str, status: int) -> bool:
    if status == 202:
        return True
    low = body[:6000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


def _unwrap(href: str) -> str:
    """Undo the engine's click-tracking redirect."""
    if "uddg=" in href:  # DuckDuckGo
        qs = parse_qs(urlparse(href).query)
        if qs.get("uddg"):
            return unquote(qs["uddg"][0])
    if href.startswith("//"):
        return "https:" + href
    return href


# ---------------------------------------------------------------------------
# Engines. Each returns [] on a soft failure and raises only on a hard one.
# ---------------------------------------------------------------------------


def _duckduckgo(query: str, limit: int) -> list[WebResult]:
    with client() as c:
        r = c.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
    if _looks_blocked(r.text, r.status_code):
        raise SearchUnavailable("duckduckgo throttled")

    out: list[WebResult] = []
    # The class attribute carries several names ("links_main links_deep
    # result__body"), so split on the distinctive one, not the whole value.
    for block in re.split(r"result__body", r.text)[1:]:
        m = re.search(r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        sm = re.search(r'result__snippet"[^>]*>(.*?)</a>', block, re.S)
        out.append(
            WebResult(
                url=_unwrap(m.group(1)),
                title=_clean(m.group(2)),
                snippet=_clean(sm.group(1)) if sm else "",
            )
        )
        if len(out) >= limit:
            break
    return out


def _bing(query: str, limit: int) -> list[WebResult]:
    with client() as c:
        r = c.get(f"https://www.bing.com/search?q={quote_plus(query)}&count={limit}")
    if _looks_blocked(r.text, r.status_code):
        raise SearchUnavailable("bing throttled")

    out: list[WebResult] = []
    for block in re.split(r'<li class="b_algo"', r.text)[1:]:
        m = re.search(r'<h2>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        sm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        out.append(
            WebResult(
                url=_unwrap(m.group(1)),
                title=_clean(m.group(2)),
                snippet=_clean(sm.group(1)) if sm else "",
            )
        )
        if len(out) >= limit:
            break
    return out


def _mojeek(query: str, limit: int) -> list[WebResult]:
    """Independent index, no throttling in practice. Small, but it is a genuine
    third opinion when the big two are sulking."""
    with client() as c:
        r = c.get(f"https://www.mojeek.com/search?q={quote_plus(query)}")
    if _looks_blocked(r.text, r.status_code):
        raise SearchUnavailable("mojeek throttled")

    out: list[WebResult] = []
    for block in re.split(r'<li class="r[a-z]*"', r.text)[1:]:
        m = re.search(r'<a[^>]*class="ob"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            m = re.search(r'<h2>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        sm = re.search(r'<p class="s">(.*?)</p>', block, re.S)
        out.append(
            WebResult(
                url=_unwrap(m.group(1)),
                title=_clean(m.group(2)),
                snippet=_clean(sm.group(1)) if sm else "",
            )
        )
        if len(out) >= limit:
            break
    return out


def _serper(query: str, limit: int) -> list[WebResult]:
    """serper.dev - Google results via API. 2,500 free credits on signup, which
    at the default cadence lasts months. Optional: set SERPER_API_KEY."""
    # Read through settings, not os.getenv - settings is what loads .env, and
    # this module can be imported without app.config ever being touched.
    key = settings.serper_api_key
    if not key:
        raise SearchUnavailable("no SERPER_API_KEY")
    # Free serper accounts reject any page size other than 10 with
    # "Query pattern not allowed for free accounts" - so always ask for 10 and
    # trim locally. Paid accounts accept 20/30/…, but 10 costs one credit and
    # is plenty per query at this cadence.
    with client(headers={"X-API-KEY": key, "Content-Type": "application/json"}) as c:
        r = c.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": 10},
        )
        if r.status_code in (401, 403):
            raise SearchUnavailable("serper rejected the API key")
        if r.status_code == 429:
            raise SearchUnavailable("serper credits exhausted or rate limited")
        if r.status_code >= 400:
            # A malformed query is this query's problem, not the engine's, so
            # report empty rather than benching serper for every later query.
            log.debug("serper %s for %r: %s", r.status_code, query, r.text[:160])
            return []
        data = r.json()
    return [
        WebResult(
            url=item.get("link", ""),
            title=item.get("title", ""),
            snippet=item.get("snippet", ""),
        )
        for item in (data.get("organic") or [])[:limit]
        if item.get("link")
    ]


# Best quality first. serper is skipped instantly when no key is set, so the
# default free experience starts at DuckDuckGo.
ENGINES = (
    ("serper", _serper),
    ("duckduckgo", _duckduckgo),
    ("bing", _bing),
    ("mojeek", _mojeek),
)

# Engines that recently threw, with the time they may be retried. Stops us
# hammering something that has already told us to go away.
_cooldown: dict[str, float] = {}
COOLDOWN_SECONDS = 900


def _available(name: str) -> bool:
    until = _cooldown.get(name, 0.0)
    return time.monotonic() >= until


def _benched(name: str) -> None:
    _cooldown[name] = time.monotonic() + COOLDOWN_SECONDS


def search(query: str, limit: int = 20, pause: float = 1.5) -> list[WebResult]:
    """Run one query against the first engine that actually answers.

    Engines that refuse are benched for 15 minutes rather than retried on every
    query, and a small random delay keeps us a well-behaved low-volume client.
    """
    errors: list[str] = []
    for name, fn in ENGINES:
        if not _available(name):
            continue
        try:
            results = fn(query, limit)
            if results:
                return results
            errors.append(f"{name}: no results")
        except SearchUnavailable as exc:
            errors.append(f"{name}: {exc}")
            if "no SERPER_API_KEY" not in str(exc):
                _benched(name)
        except Exception as exc:  # noqa: BLE001 - try the next engine
            errors.append(f"{name}: {type(exc).__name__}")
            _benched(name)
        time.sleep(pause + random.uniform(0, 0.6))

    log.info("web search found nothing for %r (%s)", query, "; ".join(errors))
    return []


def search_site(site: str, phrase: str, limit: int = 20) -> list[WebResult]:
    """Restrict a phrase search to one domain."""
    return search(f"site:{site} {phrase}", limit=limit)


def engine_status() -> dict[str, str]:
    """Reported in `/foxy status` so a degraded free mode is visible
    rather than silently returning nothing."""
    now = time.monotonic()
    out = {}
    for name, _ in ENGINES:
        if name == "serper" and not settings.serper_api_key:
            out[name] = "not configured"
        elif now < _cooldown.get(name, 0.0):
            out[name] = f"cooling down {int(_cooldown[name] - now)}s"
        else:
            out[name] = "ready"
    return out
