"""LinkedIn as a monitored source.

Covers both things the brief asks for:
  * founder launch posts referencing YC or Speedrun
  * new company pages referencing YC or Speedrun

LinkedIn text is weaker evidence than X text - in free mode we only ever see a
search snippet, never the full post - so signals from here carry a deliberate
confidence discount. Better to under-claim than to fire a confident alert built
on two truncated sentences.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..classify import classify, expand_queries
from ..config import load_rules, settings
from ..models import Signal
from ..providers import linkedin_provider
from .base import Source

log = logging.getLogger("foxy.linkedin")

PER_QUERY = 12

# LinkedIn snippets are truncated, so the same words carry less certainty than
# they would in a full post.
SNIPPET_DISCOUNT = 0.8


class LinkedInSource(Source):
    name = "linkedin"
    label = "LinkedIn"

    def __init__(self) -> None:
        self._provider = linkedin_provider.get_provider()

    @property
    def enabled(self) -> bool:
        return settings.linkedin_enabled() and self._provider is not None

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
        queries = expand_queries(rules.queries.get("linkedin", []))
        seen: set[str] = set()
        signals: list[Signal] = []

        # --- founder posts -------------------------------------------------
        for query in queries:
            try:
                posts = self._provider.search_posts(query, limit=PER_QUERY)
            except Exception as exc:  # noqa: BLE001
                log.warning("LinkedIn query %r failed: %s", query, exc)
                continue

            for post in posts:
                if post.id in seen:
                    continue
                seen.add(post.id)

                if post.posted_at and post.posted_at < oldest:
                    continue

                verdict = classify(post.text, author=post.author_name)
                if not verdict.is_announcement:
                    continue

                confidence = verdict.confidence
                if not post.hydrated:
                    confidence *= SNIPPET_DISCOUNT

                sig = Signal(
                    source="linkedin",
                    external_id=post.id,
                    title=verdict.company_name or post.author_name or "LinkedIn post",
                    url=post.url,
                    description=post.text.strip(),
                    company_name=verdict.company_name,
                    batch=verdict.batch,
                    program=verdict.program,
                    author_name=post.author_name,
                    author_url=post.author_url,
                    posted_at=post.posted_at,
                    confidence=round(confidence, 3),
                    raw={
                        "hydrated": post.hydrated,
                        "query": query,
                        "kind": "post",
                        "classifier": "llm" if verdict.used_llm else "rules",
                    },
                )
                for reason in verdict.reasons[:3]:
                    sig.add_note(reason)
                if not post.hydrated:
                    sig.add_note("snippet only - full post text unavailable")
                signals.append(sig)

        # --- new company pages ---------------------------------------------
        # A brand-new company page that already describes itself as YC-backed
        # is a strong early tell, and it is a distinct requirement in the brief.
        for query in ('"Y Combinator"', '"YC backed"', '"a16z Speedrun"'):
            try:
                pages = self._provider.search_companies(query, limit=8)
            except Exception as exc:  # noqa: BLE001
                log.warning("LinkedIn company query %r failed: %s", query, exc)
                continue

            for page in pages:
                if page.id in seen:
                    continue
                seen.add(page.id)

                verdict = classify(page.text, author=page.author_name)
                # Company pages describe themselves in third person, so the
                # first-person test in the classifier works against them.
                # Require only that YC is mentioned alongside a company name.
                looks_relevant = (
                    "y combinator" in page.text.lower()
                    or "yc" in page.text.lower()
                    or "speedrun" in page.text.lower()
                )
                if not looks_relevant:
                    continue

                sig = Signal(
                    source="linkedin",
                    external_id=page.id,
                    title=page.author_name or "New LinkedIn company page",
                    url=page.url,
                    description=page.text.strip(),
                    company_name=page.author_name or verdict.company_name,
                    batch=verdict.batch,
                    program=verdict.program,
                    confidence=round(max(0.45, verdict.confidence) * SNIPPET_DISCOUNT, 3),
                    raw={"kind": "company_page", "query": query},
                )
                sig.add_note("LinkedIn company page referencing YC/Speedrun")
                signals.append(sig)

        return signals
