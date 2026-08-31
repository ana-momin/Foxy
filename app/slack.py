"""Slack delivery.

Uses the Web API directly (chat.postMessage) rather than pulling in the whole
Slack SDK - one dependency less to install, and the payload is easier to read.

Every alert carries what the brief asks for - company name, source, description
and link - plus the thing that actually sells an early signal to a GTM person:
the founder's own words, quoted.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings
from .models import Signal
from .sources.base import client

log = logging.getLogger("foxy.slack")

API = "https://slack.com/api"
PT = ZoneInfo("America/Los_Angeles")


def _fmt_time(when: dt.datetime | None) -> str:
    """Render in Pacific time, which is the timezone a YC-watcher thinks in.

    Note: `%-I` (no zero padding) is a glibc extension and raises on Windows,
    so the hour is stripped manually instead.
    """
    when = when or dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    local = when.astimezone(PT)
    return f"{local.strftime('%b %d, %Y')}, {local.strftime('%I:%M %p').lstrip('0')} PT"


def _truncate(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class SlackClient:
    def __init__(self, token: str | None = None, target: str | None = None):
        self.token = token or settings.slack_bot_token
        self.target = target or settings.slack_target

    # -- low level ---------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("SLACK_BOT_TOKEN is not set")
        with client(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            }
        ) as c:
            r = c.post(f"{API}/{method}", json=payload)
            r.raise_for_status()
            data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {data.get('error')}")
        return data

    def auth_test(self) -> dict[str, Any]:
        """Verify the token and report which workspace/bot it belongs to."""
        return self._call("auth.test", {})

    def list_channels(self) -> list[dict[str, Any]]:
        """Channels the bot can post to, so nobody has to hunt for a channel ID.

        Needs channels:read / groups:read. Returns [] when the scope is absent
        rather than raising, so the setup wizard can fall back to asking.
        """
        out: list[dict[str, Any]] = []
        cursor = ""
        try:
            for _ in range(10):  # up to 1000 channels
                payload: dict[str, Any] = {
                    "types": "public_channel,private_channel",
                    "exclude_archived": True,
                    "limit": 100,
                }
                if cursor:
                    payload["cursor"] = cursor
                data = self._call("conversations.list", payload)
                out.extend(data.get("channels") or [])
                cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
                if not cursor:
                    break
        except Exception:  # noqa: BLE001 - missing scope is not fatal here
            return []
        return out

    def join_channel(self, channel_id: str) -> bool:
        """Join a public channel so alerts land without a manual /invite."""
        try:
            self._call("conversations.join", {"channel": channel_id})
            return True
        except Exception:  # noqa: BLE001 - private channels need an invite
            return False

    def post(
        self,
        blocks: list[dict],
        text: str,
        *,
        thread_ts: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": channel or self.target,
            "blocks": blocks,
            "text": text,          # plain-text fallback for notifications
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._call("chat.postMessage", payload)


# ---------------------------------------------------------------------------
# Block Kit builders
# ---------------------------------------------------------------------------


def _fields(pairs: list[tuple[str, str]]) -> list[dict]:
    """Slack renders at most 10 fields, two per row."""
    return [
        {"type": "mrkdwn", "text": f"*{k}*\n{v}"}
        for k, v in pairs
        if v
    ][:10]


def build_alert(sig: Signal) -> tuple[list[dict], str]:
    """Render one detection. Early signals lead with the founder's own words."""
    early = sig.is_early

    if early:
        header = ":fire: EARLY YC SIGNAL · founder announced before YC"
        status = ":zap: Founder announced / not yet listed by YC"
    elif sig.program == "Speedrun":
        header = ":checkered_flag: NEW SPEEDRUN COMPANY"
        status = ":white_check_mark: Confirmed by Speedrun (a16z)"
    else:
        header = ":new: NEW YC COMPANY"
        status = ":white_check_mark: Confirmed by YC"

    company = sig.company_name or sig.title
    pairs: list[tuple[str, str]] = [
        ("Company", company),
        ("Batch", sig.batch or "Unknown"),
        ("Source", sig.source_label),
        ("Status", status),
    ]

    if sig.author_name or sig.author_handle:
        who = sig.author_name or ""
        if sig.author_handle:
            who = f"{who} (@{sig.author_handle})".strip()
        pairs.insert(2, ("Founder", who))

    if early:
        pairs.append(("Confidence", f"{sig.confidence:.0%}"))

    # Source-specific extras that help a GTM person qualify the lead fast.
    if sig.raw.get("team_size"):
        pairs.append(("Team size", str(sig.raw["team_size"])))
    if sig.raw.get("all_locations"):
        pairs.append(("Location", str(sig.raw["all_locations"])))
    if sig.raw.get("likes"):
        pairs.append(("Engagement", f"{sig.raw['likes']:,} likes"))

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
        {"type": "section", "fields": _fields(pairs)},
    ]

    if sig.description:
        label = "Original post" if not sig.is_official else "Description"
        quoted = "\n".join(f"> {line}" for line in _truncate(sig.description).splitlines())
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{label}*\n{quoted}"},
            }
        )

    # Links.
    links = [f"<{sig.url}|{'Original post' if not sig.is_official else 'Profile'}>"]
    if sig.company_url:
        links.append(f"<{sig.company_url}|Website>")
    if sig.author_url:
        links.append(f"<{sig.author_url}|Founder>")
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": "  •  ".join(links)}}
    )

    # Context line: why we believe this, and when we saw it.
    context_bits = [f"Detected {_fmt_time(None)}"]
    if sig.posted_at:
        context_bits.append(f"Posted {_fmt_time(sig.posted_at)}")
    if early and sig.match_reason:
        context_bits.append(sig.match_reason)
    if sig.raw.get("classifier"):
        context_bits.append(f"classifier: {sig.raw['classifier']}")

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  |  ".join(context_bits)}],
        }
    )

    fallback = f"{header} - {company} ({sig.batch or 'unknown batch'}) via {sig.source_label}"
    return blocks, fallback


def build_promotion(company: str, days: int | None, url: str) -> tuple[list[dict], str]:
    """Threaded reply posted when YC later confirms a company we called early.

    This is the bot proving its own value in place, on the original alert,
    instead of firing a second notification.
    """
    lead = (
        f"*{company}* is now listed in the YC directory."
        if days is None
        else f"*{company}* is now listed in the YC directory · "
        f"*{days} day{'s' if days != 1 else ''}* after Foxy flagged it."
    )
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":white_check_mark: {lead}\n<{url}|YC profile>"},
        }
    ]
    return blocks, f"{company} confirmed by YC"


def build_health(failures: dict[str, str]) -> tuple[list[dict], str]:
    """Posted when a source fails twice in a row.

    Silent failure is the only unrecoverable bug in a monitoring product, so
    the bot reports on itself rather than just going quiet.
    """
    lines = "\n".join(f"• *{src}* · {err}" for src, err in failures.items())
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":warning: *Foxy · source degraded*\n"
                    f"{lines}\n\n_Other sources are still running normally._"
                ),
            },
        }
    ]
    return blocks, "Foxy: source degraded"


def build_status(snapshot: dict[str, Any], modes: dict[str, str]) -> tuple[list[dict], str]:
    """Response to `/foxy status`."""
    rows = []
    for name, mode in modes.items():
        info = snapshot.get("sources", {}).get(name)
        if not info:
            rows.append(f"• *{name}* · `{mode}` · _not run yet_")
            continue
        icon = ":large_green_circle:" if info["ok"] else ":red_circle:"
        detail = info.get("error") or f"{info['found']} seen, {info['new']} new"
        rows.append(f"{icon} *{name}* · `{mode}` · {detail}")

    last = snapshot.get("last_sweep_at") or "never"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Foxy status*\n"
                    f"Sweeps completed: *{snapshot.get('sweeps_completed', 0)}*\n"
                    f"Last sweep: `{last}`\n\n" + "\n".join(rows)
                ),
            },
        }
    ]
    return blocks, "Foxy status"


def build_digest(signals: list[Signal]) -> tuple[list[dict], str]:
    """Low-confidence signals, batched into one message instead of many.

    A GTM lead who gets spammed once turns the bot off, so anything below the
    confidence floor collects here rather than firing individually.
    """
    lines = []
    for s in signals[:20]:
        who = f"@{s.author_handle}" if s.author_handle else (s.author_name or "unknown")
        name = s.company_name or s.title
        lines.append(f"• <{s.url}|{name}> · {who} · {s.source_label} · {s.confidence:.0%}")

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":mag: *Possible signals* ({len(signals)} below the alert threshold)\n"
                    + "\n".join(lines)
                    + "\n\n_Lower `MIN_CONFIDENCE` to have these alert individually._"
                ),
            },
        }
    ]
    return blocks, f"{len(signals)} possible signals"
