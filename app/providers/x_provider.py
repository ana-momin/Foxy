"""X / Twitter providers.

Two halves to this job, and they have very different economics:

  DISCOVERY - "find posts where someone says they got into YC"
      X killed its free tier in Feb 2026. Pay-per-use is $0.005 per post read
      and full-archive search sits behind a $42k/mo enterprise contract, so
      there is no free official route.
        * `free`       - search engines index public X posts. No key, no cost,
                         lower recall.
        * `twitterapi` - twitterapi.io, ~$0.15 per 1,000 tweets. Much better
                         recall. Drop-in: set TWITTERAPI_KEY.

  HYDRATION - "given a post, get its full text and author"
      X's own syndication endpoint does this for free, forever, with no key.
      Verified against the reference post in the brief. We use it in BOTH
      modes: it turns a thin search snippet into the real post text, and it
      doubles as a pre-send check so deleted posts never reach Slack.
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

log = logging.getLogger("foxy.x")

STATUS_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)", re.I
)
SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result"


@dataclass
class XPost:
    id: str
    url: str
    text: str
    author_handle: str = ""
    author_name: str = ""
    created_at: dt.datetime | None = None
    likes: int = 0
    verified: bool = False
    hydrated: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def author_url(self) -> str:
        return f"https://x.com/{self.author_handle}" if self.author_handle else ""


# ---------------------------------------------------------------------------
# Free hydration - works in every mode, costs nothing
# ---------------------------------------------------------------------------


def hydrate(post_id: str) -> XPost | None:
    """Fetch a single public post via X's syndication endpoint.

    No API key, no OAuth, no rate-limit budget consumed. Returns None if the
    post was deleted, protected, or otherwise no longer public - which is
    exactly the check we want before posting an alert about it.
    """
    try:
        with client() as c:
            r = c.get(
                SYNDICATION,
                params={"id": post_id, "lang": "en", "token": "a"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("hydrate %s failed: %s", post_id, exc)
        return None

    if not data or not data.get("text"):
        return None

    user = data.get("user") or {}
    handle = user.get("screen_name") or ""
    created = None
    if data.get("created_at"):
        try:
            created = dt.datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        except ValueError:
            created = None

    return XPost(
        id=str(data.get("id_str") or post_id),
        url=f"https://x.com/{handle}/status/{post_id}",
        text=data.get("text") or "",
        author_handle=handle,
        author_name=user.get("name") or handle,
        created_at=created,
        likes=int(data.get("favorite_count") or 0),
        verified=bool(user.get("is_blue_verified")),
        hydrated=True,
    )


# ---------------------------------------------------------------------------
# Discovery providers
# ---------------------------------------------------------------------------


class XProvider(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[XPost]: ...


class FreeXProvider:
    """Search-engine discovery + free syndication hydration. $0."""

    name = "free (search engine + syndication)"

    def search(self, query: str, limit: int = 15) -> list[XPost]:
        results = websearch.search(f"site:x.com {query}", limit=limit)
        seen: set[str] = set()
        posts: list[XPost] = []

        for res in results:
            m = STATUS_RE.search(res.url)
            if not m:
                continue
            handle, post_id = m.group(1), m.group(2)
            if post_id in seen:
                continue
            seen.add(post_id)

            # Try to get the real post text for free. If X will not serve it,
            # fall back to the search snippet so we still have something to
            # score - flagged as unhydrated so the engine can be stricter.
            post = hydrate(post_id)
            if post is None:
                post = XPost(
                    id=post_id,
                    url=f"https://x.com/{handle}/status/{post_id}",
                    text=res.snippet or res.title,
                    author_handle=handle,
                    author_name=handle,
                    hydrated=False,
                )
            posts.append(post)

        return posts


class TwitterAPIProvider:
    """twitterapi.io - ~$0.15 per 1,000 tweets, single x-api-key header.

    Roughly 30x cheaper than X direct and returns real search results rather
    than whatever a search engine happened to index.
    """

    name = "twitterapi.io"
    ENDPOINT = "https://api.twitterapi.io/twitter/tweet/advanced_search"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, limit: int = 20) -> list[XPost]:
        posts: list[XPost] = []
        try:
            with client(headers={"x-api-key": self.api_key}) as c:
                r = c.get(
                    self.ENDPOINT,
                    params={"query": query, "queryType": "Latest"},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("twitterapi.io search failed (%s); falling back to free", exc)
            return FreeXProvider().search(query, limit)

        for t in (data.get("tweets") or [])[:limit]:
            author = t.get("author") or {}
            created = None
            for key in ("createdAt", "created_at"):
                if t.get(key):
                    try:
                        created = dt.datetime.fromisoformat(
                            str(t[key]).replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass
                    break
            handle = author.get("userName") or author.get("screen_name") or ""
            tid = str(t.get("id") or t.get("id_str") or "")
            if not tid:
                continue
            posts.append(
                XPost(
                    id=tid,
                    url=t.get("url") or f"https://x.com/{handle}/status/{tid}",
                    text=t.get("text") or "",
                    author_handle=handle,
                    author_name=author.get("name") or handle,
                    created_at=created,
                    likes=int(t.get("likeCount") or 0),
                    verified=bool(author.get("isBlueVerified")),
                    hydrated=True,
                    raw=t,
                )
            )
        return posts


def get_provider() -> XProvider | None:
    """Pick the provider from config. Never raises - a misconfigured paid
    provider silently degrades to the free one rather than killing the sweep."""
    choice = settings.x_provider
    if choice == "none":
        return None
    if choice == "twitterapi":
        if settings.twitterapi_key:
            return TwitterAPIProvider(settings.twitterapi_key)
        log.warning("X_PROVIDER=twitterapi but TWITTERAPI_KEY is empty; using free mode")
    return FreeXProvider()
