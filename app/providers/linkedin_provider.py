"""LinkedIn providers.

A deliberate constraint first: Foxy never logs into LinkedIn, never uses
session cookies, and never touches non-public data. LinkedIn sued Proxycurl out
of existence in July 2026 and is actively enforcing. Shipping a logged-in
scraper into someone's GTM stack would put their account at risk and would
break within weeks.

So both providers read only public, already-indexed material:

  * `free`  - search-engine discovery of public LinkedIn posts and company
              pages. No key, no cost, partial recall.
  * `apify` - Apify's cookie-free LinkedIn post search actor (~$0.005/post),
              which works over Google-indexed public posts on managed
              infrastructure. Better recall, and the operational risk sits with
              the provider rather than the user.

Adding Bright Data or another vendor later means writing one more class with a
`search_posts` method and registering it in `get_provider`.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from ..config import settings
from ..sources.base import client
from . import websearch

log = logging.getLogger("foxy.linkedin")

POST_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:posts|feed/update)/([A-Za-z0-9_\-%:.]+)",
    re.I,
)
COMPANY_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([A-Za-z0-9_\-%.]+)", re.I
)


@dataclass
class LIPost:
    id: str
    url: str
    text: str
    author_name: str = ""
    author_url: str = ""
    posted_at: dt.datetime | None = None
    kind: str = "post"          # post | company
    hydrated: bool = False
    raw: dict = field(default_factory=dict)


def clean_org_name(title: str) -> str:
    """Normalise a LinkedIn company-page title into a bare company name.

    Page titles arrive as "OpenTrade (YC s26) | LinkedIn" or
    "Loops AI (a16z SR006)". Left as-is, the parenthetical makes the YC
    directory lookup miss, and a miss is reported as an EARLY signal - so this
    directly caused a false "founder announced before YC" alert for a company
    YC had already listed.
    """
    title = (title or "").split("|")[0]
    title = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title)      # drop "(YC s26)"
    title = re.sub(r"\s*[-–—:]\s*(?:YC|Y Combinator|a16z).*$", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" .,-|")
    return title


def _author_from_slug(url: str) -> str:
    """LinkedIn post URLs embed the author's slug:
    /posts/jane-doe-1234_we-got-into-yc-activity-712... -> "jane doe"."""
    m = POST_RE.search(url)
    if not m:
        return ""
    slug = m.group(1).split("_")[0]
    slug = re.sub(r"-[0-9a-f]{6,}$", "", slug)
    parts = [p for p in slug.split("-") if p and not p.isdigit()]
    return " ".join(p.capitalize() for p in parts[:3])


class LinkedInProvider(Protocol):
    name: str

    def search_posts(self, query: str, limit: int) -> list[LIPost]: ...
    def search_companies(self, query: str, limit: int) -> list[LIPost]: ...


class FreeLinkedInProvider:
    """Search-engine discovery of public LinkedIn content. $0.

    We cannot hydrate a LinkedIn post the way we can an X post - there is no
    free public endpoint - so the search snippet *is* the text we score. That
    is weaker signal, which is why the engine holds LinkedIn signals to a
    higher confidence bar than X ones.
    """

    name = "free (search engine)"

    def search_posts(self, query: str, limit: int = 15) -> list[LIPost]:
        results = websearch.search(f"site:linkedin.com/posts {query}", limit=limit)
        out: list[LIPost] = []
        seen: set[str] = set()
        for res in results:
            m = POST_RE.search(res.url)
            if not m:
                continue
            pid = m.group(1)
            if pid in seen:
                continue
            seen.add(pid)
            text = res.snippet or res.title
            out.append(
                LIPost(
                    id=pid,
                    url=res.url.split("?")[0],
                    text=text,
                    author_name=_author_from_slug(res.url),
                    hydrated=False,
                )
            )
        return out

    def search_companies(self, query: str, limit: int = 10) -> list[LIPost]:
        """New company pages referencing YC. Covers the brief's 'detect new
        company page creations' requirement without needing an account."""
        results = websearch.search(f"site:linkedin.com/company {query}", limit=limit)
        out: list[LIPost] = []
        seen: set[str] = set()
        for res in results:
            m = COMPANY_RE.search(res.url)
            if not m:
                continue
            slug = m.group(1)
            if slug in seen:
                continue
            seen.add(slug)
            out.append(
                LIPost(
                    id=f"company:{slug}",
                    url=f"https://www.linkedin.com/company/{slug}",
                    text=res.snippet or res.title,
                    author_name=clean_org_name(res.title),
                    kind="company",
                    hydrated=False,
                )
            )
        return out


class ApifyLinkedInProvider:
    """Apify cookie-free LinkedIn post search. ~$0.005 per post.

    Runs the actor synchronously and reads its dataset straight back, so there
    is no queue to manage. Any failure degrades to the free provider rather
    than taking the whole source down.
    """

    name = "apify (cookie-free)"

    def __init__(self, token: str, actor: str):
        self.token = token
        self.actor = actor

    def _run(self, payload: dict, limit: int) -> list[dict]:
        url = (
            f"https://api.apify.com/v2/acts/{self.actor}/run-sync-get-dataset-items"
            f"?token={self.token}"
        )
        with client(timeout=180.0) as c:
            r = c.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        return data[:limit] if isinstance(data, list) else []

    def search_posts(self, query: str, limit: int = 15) -> list[LIPost]:
        try:
            items = self._run({"keyword": query, "maxItems": limit}, limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("Apify LinkedIn search failed (%s); using free mode", exc)
            return FreeLinkedInProvider().search_posts(query, limit)

        out: list[LIPost] = []
        for it in items:
            url = it.get("url") or it.get("postUrl") or ""
            if not url:
                continue
            posted = None
            for key in ("postedAt", "publishedAt", "date"):
                if it.get(key):
                    try:
                        posted = dt.datetime.fromisoformat(
                            str(it[key]).replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass
                    break
            author = it.get("author") or {}
            out.append(
                LIPost(
                    id=str(it.get("id") or url),
                    url=url.split("?")[0],
                    text=it.get("text") or it.get("content") or "",
                    author_name=(
                        author.get("name")
                        if isinstance(author, dict)
                        else str(author or "")
                    )
                    or it.get("authorName", ""),
                    author_url=(
                        author.get("url") if isinstance(author, dict) else ""
                    )
                    or it.get("authorUrl", ""),
                    posted_at=posted,
                    hydrated=True,
                    raw=it,
                )
            )
        return out

    def search_companies(self, query: str, limit: int = 10) -> list[LIPost]:
        # The post-search actor does not cover company pages; the free
        # search-engine route does, and costs nothing.
        return FreeLinkedInProvider().search_companies(query, limit)


def get_provider() -> LinkedInProvider | None:
    choice = settings.linkedin_provider
    if choice == "none":
        return None
    if choice == "apify":
        if settings.apify_token:
            return ApifyLinkedInProvider(
                settings.apify_token, settings.apify_linkedin_actor
            )
        log.warning("LINKEDIN_PROVIDER=apify but APIFY_TOKEN is empty; using free mode")
    return FreeLinkedInProvider()
