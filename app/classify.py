"""Is this post a founder announcing their own YC/Speedrun acceptance?

Two stages, deliberately in this order:

  1. RULES (free, instant). Phrase weights minus noise penalties. This kills
     the obvious junk - recruiters, "congrats to my friend", YC-application
     advice threads, alumni posts, newsletters - before anything costs money.
  2. LLM (optional, pennies). Only posts that survive the rules get a single
     Claude Haiku call, which catches phrasings no regex will ever anticipate
     and pulls out a clean company name and batch.

Without ANTHROPIC_API_KEY the rule engine runs alone and the bot still works;
the LLM raises precision, it is not load-bearing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from .config import active_batch_codes, load_rules, settings
from .sources.base import client

log = logging.getLogger("foxy.classify")

# "YC S26", "YC W27", "S26 batch", "Y Combinator Summer 2026"
BATCH_RE = re.compile(
    r"\b(?:yc\s*)?([WSXF])\s?(\d{2})\b|"
    r"\b(winter|spring|summer|fall)\s+(20\d{2})\b",
    re.I,
)
_SEASON = {"W": "Winter", "X": "Spring", "S": "Summer", "F": "Fall"}

# Phrases that introduce a company name, most specific first.
_COMPANY_PATTERNS = (
    # "Agnost AI (YC S26) is the infra for..." - by far the most common way a
    # founder writes their company name in an announcement post.
    r"\b([A-Z][\w.&'-]{1,28}(?:\s+[A-Z][\w.&'-]{1,20})?)\s*\(\s*(?:YC|Y Combinator)",
    # "...scaling SimpleClaw to $40k MRR"
    r"(?:scaling|shipping)\s+([A-Z][\w.&'-]{1,28})",
    r"(?:my|our)\s+(?:startup|company|product)\s*,?\s+([A-Z][\w.&'-]{1,28})",
    r"\b([A-Z][\w.&'-]{1,28})\s+(?:is|has been|was)\s+(?:now\s+)?(?:accepted|backed|funded|part of)",
    r"(?:building|launching|started)\s+([A-Z][\w.&'-]{1,28})",
    r"\bwe(?:'re| are)\s+([A-Z][\w.&'-]{1,28})\b",
    r"\b([A-Z][\w.&'-]{1,28})\s+(?:got|is)\s+into\s+(?:YC|Y Combinator)",
)

# Pronouns, plus openers that imply the author is speaking for the company.
_FIRST_PERSON_RE = re.compile(
    r"\b(i|we|my|our|im|weve|ive)\b"
    r"|\bi'm\b|\bwe're\b|\bwe've\b|\bi've\b"
    r"|excited to announce|thrilled to announce|proud to announce"
    r"|happy to share|excited to share|big news",
    re.I,
)

# Words that look like company names but never are.
_STOPWORDS = {
    "i", "we", "my", "our", "the", "a", "an", "this", "that", "it", "yc",
    "ycombinator", "y", "combinator", "speedrun", "a16z", "today", "excited",
    "thrilled", "proud", "so", "just", "after", "finally", "big", "news",
    "and", "but", "with", "for", "from", "founder", "founders", "startup",
    "batch", "summer", "winter", "spring", "fall", "demo", "day", "sf",
    "san", "francisco", "ai", "team", "solo",
}


@dataclass
class Verdict:
    is_announcement: bool
    confidence: float
    company_name: str | None = None
    batch: str | None = None
    program: str = "YC"
    reasons: list[str] = field(default_factory=list)
    used_llm: bool = False

    def note(self, r: str) -> None:
        if r and r not in self.reasons:
            self.reasons.append(r)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def extract_batch(text: str) -> str | None:
    """Normalise any batch mention to YC's own format, e.g. 'Fall 2026'."""
    m = BATCH_RE.search(text or "")
    if not m:
        return None
    if m.group(1):
        season = _SEASON.get(m.group(1).upper())
        return f"{season} 20{m.group(2)}" if season else None
    return f"{m.group(3).capitalize()} {m.group(4)}"


def extract_company(text: str) -> str | None:
    """Best-effort company name from free text. The LLM does this better; this
    is the free fallback and it is intentionally conservative - a wrong name is
    worse than no name, because it breaks the directory cross-reference."""
    for pattern in _COMPANY_PATTERNS:
        m = re.search(pattern, text or "")
        if not m:
            continue
        name = m.group(1).strip(" .,!'\"")
        if name.lower() in _STOPWORDS or len(name) < 2:
            continue
        return name
    return None


def detect_program(text: str) -> str:
    low = (text or "").lower()
    if "speedrun" in low:
        return "Speedrun"
    return "YC"


# ---------------------------------------------------------------------------
# Stage 1 - rules
# ---------------------------------------------------------------------------


def score_rules(text: str) -> Verdict:
    rules = load_rules()
    low = (text or "").lower()
    v = Verdict(is_announcement=False, confidence=0.0)

    if not low.strip():
        v.note("empty text")
        return v

    # Hard veto first. A post celebrating someone else's acceptance contains a
    # textbook announcement phrase and first-person words, so it scores well on
    # every other signal. Only an outright disqualifier catches it.
    for veto in rules.veto_phrases:
        if veto.lower() in low:
            v.confidence = 0.0
            v.is_announcement = False
            v.note(f"vetoed by '{veto.strip()}'")
            return v

    # Positive phrases: take the strongest match rather than summing, so a post
    # repeating the same idea three times does not out-score a clearer one.
    best = 0.0
    for phrase in rules.announcement_phrases:
        if phrase.matches(low):
            if phrase.weight > best:
                best = phrase.weight
                v.note(f"matched '{phrase.text}'")
    v.confidence = best

    # A batch code on its own is weak; alongside a phrase it is corroboration.
    batch = extract_batch(text)
    if batch:
        v.batch = batch
        v.confidence += 0.12 if best else 0.25
        v.note(f"batch {batch}")

    # First person is what separates "I got in" from "they got in", and in live
    # testing it was the single most reliable discriminator. A post with no
    # first-person voice is someone reporting on another company - a news
    # write-up, an investor, an advice thread - so this is a requirement rather
    # than a penalty.
    #
    # "Excited to announce X is in YC" carries no pronoun but is still the
    # founder speaking, so announcement openers count as first person too.
    if not _FIRST_PERSON_RE.search(low):
        v.confidence = 0.0
        v.is_announcement = False
        v.note("no first-person voice - reads as a third-party report")
        return v
    v.confidence += 0.08

    # Negatives.
    for neg in rules.negative_phrases:
        if neg.lower() in low:
            v.confidence -= 0.3
            v.note(f"negative '{neg}'")

    v.company_name = extract_company(text)
    if not v.company_name:
        v.confidence -= rules.score_cfg("no_company_penalty", 0.2)
        v.note("no company name extracted")

    v.program = detect_program(text)
    v.confidence = max(0.0, min(1.0, v.confidence))
    v.is_announcement = v.confidence >= rules.score_cfg("floor", 0.35)
    return v


# ---------------------------------------------------------------------------
# Stage 2 - optional LLM
# ---------------------------------------------------------------------------

_SYSTEM = """You classify social media posts for a venture-capital GTM team.

Decide whether the post is a FOUNDER ANNOUNCING THEIR OWN company's acceptance
into Y Combinator or a16z Speedrun.

Answer true ONLY when the author is announcing their own company. Answer false
for: congratulating someone else, advice about applying, recruiter or hiring
posts, alumni/investor commentary, newsletters, and posts merely mentioning YC.

Reply with JSON only, no prose:
{"is_announcement": bool, "company_name": string|null, "batch": string|null,
 "program": "YC"|"Speedrun", "confidence": 0.0-1.0, "reason": string}

`batch` must be normalised like "Fall 2026" or null."""


def classify_llm(text: str, author: str = "") -> Verdict | None:
    """One Haiku call. Returns None if unavailable, so callers fall back."""
    if not settings.anthropic_api_key:
        return None

    prompt = f"Author: {author or 'unknown'}\n\nPost:\n\"\"\"\n{text[:2000]}\n\"\"\""
    try:
        with client(
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=45.0,
        ) as c:
            r = c.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    "model": settings.classifier_model,
                    "max_tokens": 300,
                    "system": _SYSTEM,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            body = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("classifier unavailable (%s); using rules only", exc)
        return None

    try:
        raw = "".join(
            part.get("text", "") for part in body.get("content", []) if isinstance(part, dict)
        ).strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        data = json.loads(raw)
    except (json.JSONDecodeError, KeyError, TypeError):
        log.debug("classifier returned unparseable output")
        return None

    v = Verdict(
        is_announcement=bool(data.get("is_announcement")),
        confidence=float(data.get("confidence") or 0.0),
        company_name=(data.get("company_name") or None),
        batch=(data.get("batch") or None),
        program=(data.get("program") or "YC"),
        used_llm=True,
    )
    v.note(str(data.get("reason") or "")[:160])
    return v


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def classify(text: str, author: str = "") -> Verdict:
    """Rules first; escalate to the LLM only for posts worth the call."""
    rules_verdict = score_rules(text)

    # Not even close - do not spend a token on it.
    if rules_verdict.confidence < 0.25:
        return rules_verdict

    llm_verdict = classify_llm(text, author)
    if llm_verdict is None:
        return rules_verdict

    # The LLM decides the verdict; the rules keep it honest by contributing a
    # floor, so a confidently-worded post is not dropped on a shaky LLM score.
    merged = llm_verdict
    merged.confidence = max(
        llm_verdict.confidence,
        rules_verdict.confidence * 0.8 if llm_verdict.is_announcement else 0.0,
    )
    merged.company_name = llm_verdict.company_name or rules_verdict.company_name
    merged.batch = llm_verdict.batch or rules_verdict.batch
    for r in rules_verdict.reasons:
        merged.note(r)
    return merged


def expand_queries(templates: list[str]) -> list[str]:
    """Turn '{batch}' templates into concrete queries for the current batches."""
    codes = active_batch_codes()
    out: list[str] = []
    for t in templates:
        if "{batch}" in t:
            out.extend(t.replace("{batch}", code) for code in codes)
        else:
            out.append(t)
    return list(dict.fromkeys(out))
