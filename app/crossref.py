"""Is this company already officially listed by YC?

This is the question the whole product turns on. A founder tweeting "we got
into YC F26" is only interesting to a GTM team if YC has NOT yet published
them - that is the window where an outreach email actually lands first.

So: given an extracted company name / website / founder, ask the YC directory
three ways. If the directory does not know them, the signal is EARLY.

The directory is treated strictly as the *verification* set, never the
discovery set. Right now it lists 24 Fall 2026 companies against a batch of
several hundred, which is precisely why founder posts lead it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .models import domain_of
from .sources.yc_directory import search_directory

log = logging.getLogger("foxy.crossref")

# Name similarity above this counts as the same company.
NAME_THRESHOLD = 88


@dataclass
class Match:
    found: bool
    reason: str
    company: dict | None = None
    #: True when we had nothing to check with, or the directory was unreachable.
    #: This is NOT the same as "not in the directory" - we simply do not know.
    #: Treating unknown as early is how a monitor starts crying wolf.
    unknown: bool = False

    @property
    def is_early(self) -> bool:
        return not self.found and not self.unknown


def _norm(name: str) -> str:
    name = (name or "").lower().strip()
    name = re.sub(r"\b(inc|llc|ltd|corp|co|the)\b", "", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def lookup(
    company_name: str | None,
    company_url: str | None = None,
    *,
    text: str = "",
) -> Match:
    """Check the YC directory for this company.

    Returns a Match saying whether YC already lists them, and why we concluded
    that. `reason` is surfaced in the Slack alert so a human can sanity-check
    the bot's judgement rather than trusting it blindly.
    """
    if not company_name and not company_url:
        return Match(
            False,
            "could not identify a company in the post, so YC listing is unverified",
            unknown=True,
        )

    query = company_name or domain_of(company_url).split(".")[0]
    try:
        hits = search_directory(query, hits=20)
    except Exception as exc:  # noqa: BLE001
        # If we cannot check, do NOT claim the company is early - say so.
        log.warning("directory lookup failed for %r: %s", query, exc)
        return Match(
            False,
            f"could not reach YC directory ({type(exc).__name__})",
            unknown=True,
        )

    target_name = _norm(company_name or "")
    target_domain = domain_of(company_url)

    best_score = 0
    best_hit: dict | None = None

    for hit in hits:
        # 1. Exact / near-exact name match.
        if target_name:
            hit_name = _norm(hit.get("name") or "")
            score = fuzz.ratio(target_name, hit_name)
            if hit_name == target_name:
                return Match(True, f"exact name match: {hit.get('name')}", hit)
            if score > best_score:
                best_score, best_hit = score, hit

        # 2. Website domain match - the strongest signal available, because
        #    two companies rarely share a domain.
        if target_domain:
            if domain_of(hit.get("website")) == target_domain:
                return Match(True, f"website match: {hit.get('website')}", hit)

    if best_hit and best_score >= NAME_THRESHOLD:
        return Match(
            True,
            f"fuzzy name match {best_score}%: {best_hit.get('name')}",
            best_hit,
        )

    # 3. Last resort: the post may name the company only in a URL.
    for url in re.findall(r"https?://[^\s)]+", text or ""):
        d = domain_of(url)
        if not d or "x.com" in d or "twitter.com" in d or "linkedin.com" in d:
            continue
        try:
            for hit in search_directory(d.split(".")[0], hits=10):
                if domain_of(hit.get("website")) == d:
                    return Match(True, f"website match via post link: {d}", hit)
        except Exception:  # noqa: BLE001
            break

    detail = f"closest was {best_score}%" if best_hit else "no similar names"
    return Match(False, f"not in YC directory ({detail})")


# ---------------------------------------------------------------------------
# Speedrun truth set
# ---------------------------------------------------------------------------
#
# A Speedrun company will never appear in YC's directory, so checking one
# against YC would report "not in YC directory" forever - turning every single
# a16z Speedrun company into a permanent false "early signal". Each program
# needs verifying against its own directory.

_speedrun_cache: dict[str, dict] | None = None


def _speedrun_index() -> dict[str, dict]:
    """Name-keyed index of the a16z Speedrun directory, built once per process."""
    global _speedrun_cache
    if _speedrun_cache is not None:
        return _speedrun_cache

    from .sources.speedrun import SpeedrunSource

    index: dict[str, dict] = {}
    try:
        src = SpeedrunSource()
        src.pages = 8  # walk further than a sweep does; this is the truth set
        for sig in src.fetch():
            key = _norm(sig.company_name or sig.title)
            if key:
                index[key] = {
                    "name": sig.company_name or sig.title,
                    "batch": sig.batch,
                    "website": sig.company_url,
                    "slug": sig.url,
                }
    except Exception as exc:  # noqa: BLE001
        log.warning("could not build Speedrun index: %s", exc)
        return {}

    _speedrun_cache = index
    return index


def lookup_speedrun(company_name: str | None) -> Match:
    """Check the a16z Speedrun directory rather than YC's."""
    if not company_name:
        return Match(
            False,
            "could not identify a company in the post, so Speedrun listing is unverified",
            unknown=True,
        )

    index = _speedrun_index()
    if not index:
        return Match(False, "could not reach the Speedrun directory", unknown=True)

    target = _norm(company_name)
    if target in index:
        hit = index[target]
        return Match(True, f"exact name match in Speedrun directory: {hit['name']}", hit)

    best_key, best_score = "", 0
    for key in index:
        score = fuzz.ratio(target, key)
        if score > best_score:
            best_key, best_score = key, score

    if best_score >= NAME_THRESHOLD:
        hit = index[best_key]
        return Match(True, f"fuzzy match {best_score}% in Speedrun: {hit['name']}", hit)

    return Match(False, f"not in the Speedrun directory (closest was {best_score}%)")


def lookup_for_program(
    program: str,
    company_name: str | None,
    company_url: str | None = None,
    *,
    text: str = "",
) -> Match:
    """Verify against whichever directory actually governs this program."""
    if (program or "").lower() == "speedrun":
        return lookup_speedrun(company_name)
    return lookup(company_name, company_url, text=text)
