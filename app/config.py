"""Settings and keyword config.

Two layers:
  * `Settings`  - secrets and switches, from environment / .env
  * `load_rules()` - editable keyword packs, from config.yaml

Nothing here should ever raise on a missing optional key. The bot is designed
to boot with only a Slack token and degrade gracefully everywhere else.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
import pathlib
import re

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = pathlib.Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def _b(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


class Settings:
    """Runtime configuration. Reads env once at import."""

    # --- Slack -------------------------------------------------------------
    slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN", "").strip()
    slack_target: str = os.getenv("SLACK_TARGET", "").strip()

    # --- State -------------------------------------------------------------
    database_url: str = (os.getenv("DATABASE_URL") or "").strip() or (
        f"sqlite:///{ROOT / 'foxy.db'}"
    )

    # --- Providers ---------------------------------------------------------
    x_provider: str = os.getenv("X_PROVIDER", "free").strip().lower()
    twitterapi_key: str = os.getenv("TWITTERAPI_KEY", "").strip()

    # Optional Google-quality search, used by the free X/LinkedIn providers.
    serper_api_key: str = os.getenv("SERPER_API_KEY", "").strip()

    linkedin_provider: str = os.getenv("LINKEDIN_PROVIDER", "free").strip().lower()
    apify_token: str = os.getenv("APIFY_TOKEN", "").strip()
    apify_linkedin_actor: str = os.getenv(
        "APIFY_LINKEDIN_ACTOR", "khadinakbar~linkedin-post-search-scraper"
    ).strip()

    # --- Classifier --------------------------------------------------------
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    classifier_model: str = os.getenv(
        "CLASSIFIER_MODEL", "claude-haiku-4-5-20251001"
    ).strip()

    # --- Slack one-click install (optional) --------------------------------
    slack_client_id: str = os.getenv("SLACK_CLIENT_ID", "").strip()
    slack_client_secret: str = os.getenv("SLACK_CLIENT_SECRET", "").strip()

    # --- Hosted mode (optional) --------------------------------------------
    # Encrypts stored Slack tokens. Falls back to the client secret so hosted
    # mode still works if this is forgotten, but set it explicitly.
    encryption_key: str = os.getenv("ENCRYPTION_KEY", "").strip()
    # Search credits this key started with. serper gives 2,500 on signup; set
    # this to whatever a replacement key carries so the warning stays honest.
    serper_allowance: int = int(os.getenv("SERPER_ALLOWANCE", "2500") or 0)
    # Shared secret the scheduled sweep presents to POST /internal/sweep.
    sweep_key: str = os.getenv("SWEEP_KEY", "").strip()

    # --- Pond --------------------------------------------------------------
    pond_access_key: str = os.getenv("POND_ACCESS_KEY", "").strip()
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

    # --- Behaviour ---------------------------------------------------------
    scan_interval_hours: int = _i("SCAN_INTERVAL_HOURS", 8)
    min_confidence: float = _f("MIN_CONFIDENCE", 0.55)
    backfill_days: int = _i("BACKFILL_DAYS", 7)
    # Hard ceiling on alerts posted in one sweep, per workspace.
    max_alerts_per_sweep: int = _i("MAX_ALERTS_PER_SWEEP", 25)
    # How many of the newest detections a brand-new workspace receives on its
    # very first sweep. Without this it receives nothing at all and has to wait
    # for the next genuinely new company, which is a poor first impression.
    first_run_alerts: int = _i("FIRST_RUN_ALERTS", 6)
    # Alerts included on the free plan, per workspace, lifetime.
    free_alert_quota: int = _i("FREE_ALERT_QUOTA", 50)

    # Pricing, kept here so the site, the upgrade page and the Slack notice
    # cannot quote three different numbers.
    price_monthly_usd: str = os.getenv("PRICE_MONTHLY_USD", "3").strip()
    price_yearly_usd: str = os.getenv("PRICE_YEARLY_USD", "10").strip()
    # Where payment goes. USDC on Base: the fee is a fraction of a cent, which
    # is what makes a three dollar plan possible at all - a card processor's
    # flat 30c would take a tenth of it.
    pay_wallet: str = os.getenv("PAY_WALLET", "").strip()
    pay_chain: str = os.getenv("PAY_CHAIN", "Base").strip()
    pay_asset: str = os.getenv("PAY_ASSET", "USDC").strip()
    dry_run: bool = _b("DRY_RUN")

    agent_version: str = "2026.08.29"

    # --- Derived -----------------------------------------------------------
    @classmethod
    def x_enabled(cls) -> bool:
        if cls.x_provider == "none":
            return False
        if cls.x_provider == "twitterapi":
            return bool(cls.twitterapi_key)
        return True  # free provider always available

    @classmethod
    def linkedin_enabled(cls) -> bool:
        if cls.linkedin_provider == "none":
            return False
        if cls.linkedin_provider == "apify":
            return bool(cls.apify_token)
        return True

    @classmethod
    def slack_configured(cls) -> bool:
        return bool(cls.slack_bot_token and cls.slack_target)


settings = Settings()


# ---------------------------------------------------------------------------
# Keyword rules
# ---------------------------------------------------------------------------


class Phrase(BaseModel):
    text: str
    weight: float = 0.5
    regex: bool = False

    def matches(self, haystack: str) -> bool:
        if self.regex:
            return re.search(self.text, haystack, re.I) is not None
        return self.text.lower() in haystack


class Rules(BaseModel):
    announcement_phrases: list[Phrase] = []
    veto_phrases: list[str] = []
    negative_phrases: list[str] = []
    batches: dict = {}
    queries: dict = {}
    sources: dict = {}
    scoring: dict = {}
    max_post_age_days: int = 120

    def source_enabled(self, name: str) -> bool:
        return bool(self.sources.get(name, {}).get("enabled", True))

    def every_n(self, name: str) -> int:
        return int(self.sources.get(name, {}).get("every_n_sweeps", 1) or 1)

    def score_cfg(self, key: str, default: float) -> float:
        try:
            return float(self.scoring.get(key, default))
        except (TypeError, ValueError):
            return default


@functools.lru_cache(maxsize=1)
def load_rules(path: str | None = None) -> Rules:
    p = pathlib.Path(path) if path else ROOT / "config.yaml"
    if not p.exists():
        return Rules()
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Rules(**raw)


# ---------------------------------------------------------------------------
# Batch codes
# ---------------------------------------------------------------------------

# YC now runs four batches a year. Codes look like "W27", "S26", "F26", "X26"
# (Spring). We generate a rolling window around today so the bot keeps working
# across batch turnover without anyone editing config.
_BATCH_SEQ = [("W", 1), ("X", 4), ("S", 7), ("F", 10)]  # letter, start month


def active_batch_codes(today: dt.date | None = None) -> list[str]:
    """Return YC batch codes around today, e.g. ['YC S26', 'YC F26', 'YC W27']."""
    rules = load_rules()
    cfg = rules.batches or {}
    if not cfg.get("auto_generate", True):
        return list(cfg.get("extra") or [])

    today = today or dt.date.today()
    ahead = int(cfg.get("lookahead_batches", 3))
    behind = int(cfg.get("lookbehind_batches", 2))

    # Build a flat timeline of (year, letter) and find where we are in it.
    timeline: list[tuple[int, str]] = []
    for year in range(today.year - 2, today.year + 3):
        for letter, _month in _BATCH_SEQ:
            timeline.append((year, letter))

    # Current index = the latest batch whose start month has passed.
    idx = 0
    for i, (year, letter) in enumerate(timeline):
        month = dict(_BATCH_SEQ)[letter]
        if (year, month) <= (today.year, today.month):
            idx = i

    lo = max(0, idx - behind)
    hi = min(len(timeline), idx + ahead + 1)

    codes = [f"YC {letter}{str(year)[2:]}" for year, letter in timeline[lo:hi]]
    codes.extend(cfg.get("extra") or [])
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(codes))
