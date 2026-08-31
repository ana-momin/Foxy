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


# LinkedIn company-page search returns a lot of things that are not a newly
# created startup page: YC's own regional pages, recruiter aggregators, VC
# funds, "Stealth Startup" placeholders. Any title containing one of these is
# not a company detection.
_ORG_BLOCKLIST = (
    "y combinator", "ycombinator", "stealth", "ventures", "capital",
    "partners", "jobs", "careers", "hiring", "alumni", "community",
    "network", "group", "consulting", "recruiting", "accelerator",
    # Events and meetups, which read exactly like company names in a snippet.
    "tech week", "week by", "summit", "conference", "meetup", "hang",
    "demo day", "office hours", "fireside", "panel", "workshop", "cohort",
)

# A company page is weak evidence: it proves a page mentions YC, not that a new
# company was just accepted. Cap its confidence below the default alert
# threshold so these collect in the daily digest instead of firing individually.
# The brief asks us to detect them; it does not ask us to shout about them.
COMPANY_PAGE_CONFIDENCE = 0.45


def _is_plausible_org(name: str) -> bool:
    low = (name or "").lower().strip()
    if len(low) < 3 or len(low) > 48:
        return False
    return not any(bad in low for bad in _ORG_BLOCKLIST)


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

                # A name we cannot trust is worse than no name: it makes the
                # YC lookup miss, and a miss is reported as an early signal.
                # Drop the name rather than let it fabricate an EARLY alert.
                company = verdict.company_name
                if company and not _is_plausible_org(company):
                    company = None

                confidence = verdict.confidence
                if not post.hydrated:
                    confidence *= SNIPPET_DISCOUNT

                sig = Signal(
                    source="linkedin",
                    external_id=post.id,
                    title=company or post.author_name or "LinkedIn post",
                    url=post.url,
                    description=post.text.strip(),
                    company_name=company,
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
                # "Our team includes YC-backed founders" is not a YC company,
                # and "a16z Speedrun Dinner & Board Games" is an event listing.
                # Require the page to describe the company itself as being in
                # the programme.
                low = page.text.lower()
                mentions = any(k in low for k in ("y combinator", "yc ", "yc-", "speedrun"))
                about_others = any(
                    k in low
                    for k in (
                        "team includes", "founders from", "alumni", "advisors",
                        "our investors include", "worked at",
                    )
                )
                is_event = any(
                    k in low
                    for k in (
                        "luma.com", "lu.ma", "dinner", "board games", "rsvp",
                        "comments ·", "· like", "meetup", "happy hour",
                    )
                )
                if not mentions or about_others or is_event:
                    continue

                org = page.author_name or verdict.company_name
                if not _is_plausible_org(org):
                    continue

                sig = Signal(
                    source="linkedin",
                    external_id=page.id,
                    title=page.author_name or "New LinkedIn company page",
                    url=page.url,
                    description=page.text.strip(),
                    company_name=org,
                    batch=verdict.batch,
                    program=verdict.program,
                    confidence=COMPANY_PAGE_CONFIDENCE,
                    raw={"kind": "company_page", "query": query},
                )
                sig.add_note("LinkedIn company page referencing YC/Speedrun")
                signals.append(sig)

        return signals
