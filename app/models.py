"""The one shape every source produces.

Adding a new platform later means writing something that emits `Signal`
objects. Nothing downstream - dedupe, scoring, cross-reference, Slack - needs
to know where a signal came from.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

SourceName = Literal["yc_directory", "yc_launches", "speedrun", "x", "linkedin"]

# How each source is described in the Slack alert.
SOURCE_LABELS: dict[str, str] = {
    "yc_directory": "YC Directory",
    "yc_launches": "Launch YC",
    "speedrun": "Speedrun (a16z)",
    "x": "X",
    "linkedin": "LinkedIn",
}

# Sources that are, by definition, YC confirming a company itself.
OFFICIAL_SOURCES = {"yc_directory", "yc_launches", "speedrun"}


def _slug(text: str) -> str:
    """Loose key used for matching a company across sources."""
    text = (text or "").lower().strip()
    text = re.sub(r"\b(inc|llc|ltd|corp|co|labs?|ai|hq|technologies|tech)\b", "", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def domain_of(url: str | None) -> str:
    """Bare registrable-ish domain, for matching a post against a company site."""
    if not url:
        return ""
    m = re.search(r"https?://(?:www\.)?([^/?#]+)", url.strip(), re.I)
    host = (m.group(1) if m else url).lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class Signal:
    """One thing we noticed, from one source, at one point in time."""

    source: str                      # yc_directory | yc_launches | speedrun | x | linkedin
    external_id: str                 # stable id within that source
    title: str                       # company name, or post headline
    url: str                         # link a human should click

    description: str = ""            # one-liner / post text
    company_name: str | None = None  # extracted or authoritative
    company_url: str | None = None
    batch: str | None = None         # "Fall 2026", "YC F26", cohort name
    program: str = "YC"              # YC | Speedrun
    author_name: str | None = None
    author_handle: str | None = None
    author_url: str | None = None
    posted_at: dt.datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # Is this someone announcing their own acceptance?
    #
    # Set by the source, from the classifier's verdict. It is not the same
    # question as "is this company missing from the directory", and conflating
    # the two is what put LinkedIn company pages - UzCombinator, Grubwithus
    # (YC W2011) - into the early-signal results as founder announcements.
    # Only an announcement can be early; everything else is at most a lead.
    is_announcement: bool = False

    # Filled in by the engine, not by sources.
    confidence: float = 1.0
    is_early: bool = False           # founder announced, YC has not confirmed
    confirmed: bool = False          # present in the YC directory
    match_reason: str = ""           # why we did or did not find it in YC
    notes: list[str] = field(default_factory=list)

    # -- identity ----------------------------------------------------------

    @property
    def fingerprint(self) -> str:
        """Stable dedupe key. Same post or same company never alerts twice."""
        basis = f"{self.source}:{self.external_id}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    @property
    def entity_key(self) -> str:
        """Cross-source company key, used to link an early signal to its later
        confirmation. Falls back to the fingerprint when we have no name."""
        s = _slug(self.company_name or self.title)
        return s or self.fingerprint

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def is_official(self) -> bool:
        return self.source in OFFICIAL_SOURCES

    def add_note(self, note: str) -> None:
        if note and note not in self.notes:
            self.notes.append(note)


@dataclass
class SweepResult:
    """What one full pass over all sources produced."""

    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    per_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    alerts: list[Signal] = field(default_factory=list)
    digest: list[Signal] = field(default_factory=list)

    # Alerts that Slack actually accepted, and why any of them did not land.
    #
    # `alerts` counts decisions; this counts deliveries. Conflating the two is
    # how 687 alerts came to be recorded and reported without a single one
    # being sent, so quota and success are both measured from here.
    delivered: list[Signal] = field(default_factory=list)
    delivery_errors: list[str] = field(default_factory=list)

    def record(self, source: str, *, found: int, new: int, error: str | None = None) -> None:
        self.per_source[source] = {"found": found, "new": new, "error": error}

    @property
    def ok(self) -> bool:
        return (
            all(v.get("error") is None for v in self.per_source.values())
            and not self.delivery_errors
        )

    @property
    def undelivered(self) -> int:
        """Alerts that were decided on but never reached Slack."""
        return len(self.alerts) - len(self.delivered)

    @property
    def failed_sources(self) -> list[str]:
        return [k for k, v in self.per_source.items() if v.get("error")]
