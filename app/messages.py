"""What Foxy says about itself.

Alerts are the product; these are the handful of moments where Foxy has to talk
about its own state instead - the free allowance running out, a plan starting,
headroom being granted, an install that never finished. Each one used to be a
single line of text, or nothing at all, and they are the moments a person is
most likely to decide whether this thing is worth paying for.

Three rules hold them together:

* Say what changed, then what it means for them, then the one thing to do next.
  A notice that reports a state without saying what to do about it just makes
  somebody go and find out.
* One emoji, at the top, chosen because it means something. A channel already
  full of detections does not need more decoration to scroll past.
* Carry the claim code wherever money is involved. Pond does not tell Foxy who
  is calling, so a subscription bought there is joined to a Slack workspace by
  someone quoting that code - a message about upgrading that omits it sends the
  reader back to a settings page to hunt for it.

Every builder returns `(blocks, fallback_text)`, matching `build_alert`. The
fallback is what appears in notifications and on devices that do not render
blocks, so it has to carry the point on its own rather than say "Foxy".
"""

from __future__ import annotations

from typing import Any

from .config import settings


def _url(path: str = "") -> str:
    """An absolute link, or nothing.

    PUBLIC_BASE_URL is unset in development and in most tests. A button whose
    url is empty is rejected by Slack and takes the whole message down with it,
    so callers drop the button rather than send a broken one.
    """
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}{path}" if base else ""


def _price() -> str:
    minor = getattr(settings, "price_monthly_minor", 500)
    major = minor / 100
    return f"${major:.0f}" if major == int(major) else f"${major:.2f}"


def _button(label: str, url: str, primary: bool = False) -> list[dict]:
    if not url:
        return []
    element: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": label[:74], "emoji": True},
        "url": url,
    }
    if primary:
        element["style"] = "primary"
    return [{"type": "actions", "elements": [element]}]


def _head(icon: str, title: str) -> list[dict]:
    """The same opening shape as an announcement, so these read as Foxy
    speaking rather than as another detection."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{icon}  *{title}*"}},
        {"type": "divider"},
    ]


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------


def free_tier_ended(quota: int, claim_code: str = "") -> tuple[list[dict], str]:
    """The free allowance is spent and monitoring has paused.

    The one message here that decides revenue, so it has to answer the three
    questions a person actually has: what stopped, what happens to what I had,
    and what does it cost. It says "paused", not "ended", because that is the
    truth - the seen-set and every alert already delivered are untouched, and
    the moment a plan is on it carries on from where it stopped.
    """
    blocks = [
        *_head(":hourglass_flowing_sand:", "Free alerts used up"),
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Foxy has delivered all *{quota}* of its free alerts, so "
                    "monitoring is paused for this channel.\n\n"
                    "Nothing has been lost. Foxy keeps watching quietly and "
                    "picks up exactly where it stopped the moment Pro is on."
                ),
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Pro*\n{_price()} a month",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Included*\n{settings.pro_included_results:,} alerts",
                },
            ],
        },
        *_button("See Pro", _url("/pricing"), primary=True),
    ]

    if claim_code:
        blocks.append(
            _context(
                f"After subscribing, quote `{claim_code}` and Pro is switched "
                "on for this channel."
            )
        )

    return blocks, (
        f"Foxy has used all {quota} free alerts. Monitoring is paused - "
        f"Pro is {_price()} a month for {settings.pro_included_results:,} alerts."
    )


def pro_activated(plan_label: str = "", claim_code: str = "") -> tuple[list[dict], str]:
    """A plan has been switched on.

    Deliberately short. The person has already decided and paid; what they want
    to know is that it worked and that alerts are coming back.
    """
    until = ""
    if plan_label.startswith("Pro, until "):
        until = plan_label.removeprefix("Pro, until ")

    fields = [
        {"type": "mrkdwn", "text": "*Plan*\nPro"},
        {
            "type": "mrkdwn",
            "text": f"*Alerts*\n{settings.pro_included_results:,} included",
        },
    ]
    if until:
        fields.append({"type": "mrkdwn", "text": f"*Renews*\n{until}"})

    blocks = [
        *_head(":sparkles:", "Pro is on"),
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Monitoring has resumed for this channel. Foxy is watching "
                    "every source again, including founders who announce before "
                    "YC publishes them.\n\nThank you for backing Foxy."
                ),
            },
        },
        {"type": "section", "fields": fields},
    ]

    if claim_code:
        blocks.append(_context(f"Keep `{claim_code}` for renewals."))

    return blocks, "Pro is on. Foxy has resumed monitoring this channel."


def alerts_granted(added: int, remaining: int) -> tuple[list[dict], str]:
    """Headroom added by hand, usually to unblock someone mid-conversation."""
    left = "unlimited" if remaining >= 10**8 else f"{remaining:,}"
    return [
        *_head(":heavy_plus_sign:", f"{added:,} more alerts"),
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Foxy has *{left}* alerts left on this channel and is "
                    "monitoring again."
                ),
            },
        },
    ], f"{added:,} more alerts added. {left} left on this channel."


def pro_ended(quota: int, claim_code: str = "") -> tuple[list[dict], str]:
    """A plan has lapsed or been removed.

    The mirror of `pro_activated`, and the one place where a neutral tone
    matters most: whatever the reason, the reader should not feel scolded.
    """
    blocks = [
        *_head(":hourglass:", "Pro has ended"),
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"This channel is back on the free plan, which includes "
                    f"*{quota}* alerts. Everything Foxy has already sent stays "
                    "where it is."
                ),
            },
        },
        *_button("See Pro", _url("/pricing")),
    ]
    if claim_code:
        blocks.append(_context(f"To switch Pro back on, quote `{claim_code}`."))
    return blocks, f"Pro has ended. This channel is back on the free {quota}-alert plan."


# ---------------------------------------------------------------------------
# an install that never finished
# ---------------------------------------------------------------------------


def channel_missing(team: str, install_id: str) -> tuple[list[dict], str]:
    """Sent as a DM to whoever installed Foxy, once.

    This is the quietest way to lose a customer: Foxy is installed, it looks
    fine in the console, and it has nowhere to post, so the person who added it
    concludes it does not work. They cannot be told in a channel - there isn't
    one - so it goes to them directly, and only one time.
    """
    where = f" in *{team}*" if team else ""
    return [
        *_head(":wave:", "Foxy needs a channel"),
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Foxy is installed{where} but no channel was chosen, so it "
                    "has nowhere to post.\n\nPick one and the first alerts "
                    "arrive on the next sweep - including anything found while "
                    "it was waiting."
                ),
            },
        },
        *_button("Choose a channel", _url(f"/app/{install_id}"), primary=True),
    ], "Foxy is installed but has no channel to post in yet."
