"""X / Twitter as a monitored source.

Runs the configured discovery provider over every announcement query, scores
what comes back, and emits a Signal for anything that reads like a founder
announcing their own acceptance. The engine then cross-references each one
against the YC directory to decide whether it is EARLY.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..classify import classify, expand_queries
from ..config import load_rules, settings
from ..models import Signal
from ..providers import x_provider
from .base import Source

log = logging.getLogger("foxy.x")

# Per-query result cap. Free mode is limited by what a search engine returns
# anyway; paid mode uses this to keep spend predictable.
PER_QUERY = 15


class XSource(Source):
    name = "x"
    label = "X"

    def __init__(self) -> None:
        self._provider = x_provider.get_provider()

    @property
    def enabled(self) -> bool:
        return settings.x_enabled() and self._provider is not None

    @property
    def mode(self) -> str:
        return self._provider.name if self._provider else "disabled"

    def fetch(self) -> list[Signal]:
        if not self._provider:
            return []

        rules = load_rules()
        oldest = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=rules.max_post_age_days
        )
        queries = expand_queries(rules.queries.get("x", []))
        seen: set[str] = set()
        signals: list[Signal] = []

        for query in queries:
            try:
                posts = self._provider.search(query, limit=PER_QUERY)
            except Exception as exc:  # noqa: BLE001 - one bad query is not fatal
                log.warning("X query %r failed: %s", query, exc)
                continue

            for post in posts:
                if post.id in seen:
                    continue
                seen.add(post.id)

                # Search engines surface old posts. An announcement from a
                # year ago is not news, and alerting on it makes the bot look
                # broken. Posts with no readable date are kept.
                if post.created_at and post.created_at < oldest:
                    continue

                verdict = classify(post.text, author=post.author_handle)
                if not verdict.is_announcement:
                    continue

                # An unhydrated post means we only ever saw a search snippet,
                # which is thin evidence. Discount it rather than dropping it.
                confidence = verdict.confidence
                if not post.hydrated:
                    confidence *= 0.85

                sig = Signal(
                    source="x",
                    external_id=post.id,
                    title=verdict.company_name or f"@{post.author_handle}",
                    url=post.url,
                    description=post.text.strip(),
                    company_name=verdict.company_name,
                    batch=verdict.batch,
                    program=verdict.program,
                    author_name=post.author_name,
                    author_handle=post.author_handle,
                    author_url=post.author_url,
                    posted_at=post.created_at,
                    confidence=round(confidence, 3),
                    # Reached here, so the classifier judged it a self-announcement.
                    is_announcement=True,
                    raw={
                        "likes": post.likes,
                        "verified": post.verified,
                        "hydrated": post.hydrated,
                        "query": query,
                        "classifier": "llm" if verdict.used_llm else "rules",
                    },
                )
                for reason in verdict.reasons[:3]:
                    sig.add_note(reason)
                if not post.hydrated:
                    sig.add_note("snippet only - full post text unavailable")
                signals.append(sig)

        return signals
