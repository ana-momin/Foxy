"""The messages Foxy sends about itself.

These are the moments a person decides whether Foxy is worth paying for: the
free allowance running out, a plan starting, an install that never finished.
Each is sent once, to someone who is not expecting it, and a malformed one does
not fail loudly - Slack rejects the payload and the message simply never
arrives, which looks exactly like the bot having nothing to say.

So two kinds of test here: that each message is structurally valid Slack, and
that it actually says the thing the reader needs.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-the-suite")

BASE = "https://tryfoxy.example"


@pytest.fixture()
def site(monkeypatch):
    """A public base url, so buttons are built rather than dropped."""
    from app.config import settings

    monkeypatch.setattr(settings, "public_base_url", BASE)
    return settings


# ---------------------------------------------------------------------------
# structural validity
# ---------------------------------------------------------------------------


def check_blocks(blocks: list[dict], fallback: str) -> None:
    """The subset of Slack's rules these messages can actually break.

    Written out rather than trusted, because every one of these produces the
    same symptom in production - no message, no error anyone sees.
    """
    assert isinstance(blocks, list) and blocks, "no blocks"
    assert len(blocks) <= 50, "Slack renders at most 50 blocks"
    assert fallback and fallback.strip(), "an empty fallback shows as a blank notification"
    assert len(fallback) <= 3000

    for i, b in enumerate(blocks):
        where = f"block {i} ({b.get('type')})"
        assert "type" in b, where

        if b["type"] == "section":
            assert "text" in b or "fields" in b, f"{where} says nothing"
            if "text" in b:
                assert b["text"]["type"] in ("mrkdwn", "plain_text"), where
                assert b["text"]["text"].strip(), f"{where} has empty text"
                assert len(b["text"]["text"]) <= 3000, where
            if "fields" in b:
                assert 1 <= len(b["fields"]) <= 10, f"{where} has a bad field count"
                for f in b["fields"]:
                    assert f["text"].strip(), f"{where} has an empty field"
                    assert len(f["text"]) <= 2000, where

        elif b["type"] == "actions":
            assert 1 <= len(b["elements"]) <= 5, where
            for el in b["elements"]:
                assert el["type"] == "button", where
                assert el["text"]["text"].strip(), f"{where} has an unlabelled button"
                assert len(el["text"]["text"]) <= 75, where
                # The one that bites: Slack rejects the whole message for a
                # button with an empty or relative url.
                assert el.get("url", "").startswith("http"), (
                    f"{where} has a url Slack will reject: {el.get('url')!r}"
                )
                assert el.get("style") in (None, "primary", "danger"), where

        elif b["type"] == "context":
            assert 1 <= len(b["elements"]) <= 10, where
            for el in b["elements"]:
                assert el["text"].strip(), f"{where} has an empty context element"

        elif b["type"] == "header":
            assert b["text"]["type"] == "plain_text", where
            assert len(b["text"]["text"]) <= 150, where

        else:
            assert b["type"] == "divider", f"{where} is an unexpected block type"


def every_message(with_code: str = "FOXY-7K2Q"):
    from app import messages

    return {
        "free_tier_ended": messages.free_tier_ended(50, with_code),
        "pro_activated": messages.pro_activated("Pro, until 13 Oct 2026", with_code),
        "alerts_granted": messages.alerts_granted(50, 52),
        "pro_ended": messages.pro_ended(50, with_code),
        "channel_missing": messages.channel_missing("Foxy Land", "abc123"),
    }


def test_every_message_is_valid_slack(site):
    for name, (blocks, text) in every_message().items():
        try:
            check_blocks(blocks, text)
        except AssertionError as exc:
            raise AssertionError(f"{name}: {exc}") from exc


def test_every_message_is_valid_without_a_claim_code(site):
    for name, (blocks, text) in every_message(with_code="").items():
        try:
            check_blocks(blocks, text)
        except AssertionError as exc:
            raise AssertionError(f"{name}: {exc}") from exc


def test_a_missing_base_url_drops_buttons_rather_than_breaking_the_message(monkeypatch):
    """PUBLIC_BASE_URL is unset in development. A button with an empty url is
    not a cosmetic problem: Slack refuses the whole message."""
    from app.config import settings

    monkeypatch.setattr(settings, "public_base_url", "")

    for name, (blocks, text) in every_message().items():
        assert not [b for b in blocks if b["type"] == "actions"], (
            f"{name} built a button with no url to point at"
        )
        try:
            check_blocks(blocks, text)
        except AssertionError as exc:
            raise AssertionError(f"{name}: {exc}") from exc


def test_the_messages_stay_quiet_with_emoji(site):
    """One at the top, chosen deliberately. A channel full of detections does
    not need more decoration to scroll past."""
    import re

    for name, (blocks, _) in every_message().items():
        found = re.findall(r":[a-z0-9_+\-]+:", str(blocks))
        assert len(found) <= 1, f"{name} uses {len(found)} emoji: {found}"


# ---------------------------------------------------------------------------
# what they actually say
# ---------------------------------------------------------------------------


def test_the_end_of_the_free_tier_says_the_price_and_how_to_buy(site):
    from app import messages
    from app.config import settings

    blocks, text = messages.free_tier_ended(50, "FOXY-7K2Q")
    body = str(blocks)

    assert "50" in body, "it should say how many alerts were included"
    assert "$5" in body, "the price is the question the reader has"
    assert f"{settings.pro_included_results:,}" in body, "and what that buys"
    assert "FOXY-7K2Q" in body, "without the code they cannot be switched on"

    button = [b for b in blocks if b["type"] == "actions"][0]["elements"][0]
    assert button["url"] == f"{BASE}/pricing"
    assert button["style"] == "primary"

    # The fallback is what appears in the notification, so it has to carry the
    # point rather than say "Foxy".
    assert "paused" in text and "$5" in text


def test_the_end_of_the_free_tier_does_not_read_as_a_loss(site):
    """Someone who thinks their history is gone does not upgrade, they
    reinstall - and reinstalling would not give it back either."""
    from app import messages

    blocks, _ = messages.free_tier_ended(50, "")
    body = str(blocks).lower()
    assert "paused" in body
    assert "nothing has been lost" in body


def test_pro_activated_confirms_what_was_bought(site):
    from app import messages
    from app.config import settings

    blocks, text = messages.pro_activated("Pro, until 13 Oct 2026", "FOXY-7K2Q")
    body = str(blocks)

    assert "13 Oct 2026" in body, "the renewal date is the thing they will look for"
    assert f"{settings.pro_included_results:,}" in body
    assert "resumed" in text


def test_pro_activated_survives_a_plan_with_no_end_date(site):
    """A granted plan has no expiry, and a date field built from one would
    read "Renews Pro"."""
    from app import messages

    blocks, _ = messages.pro_activated("Pro", "")
    fields = [b for b in blocks if b.get("fields")][0]["fields"]
    assert not [f for f in fields if "Renews" in f["text"]]
    check_blocks(*messages.pro_activated("Pro", ""))


def test_granting_alerts_says_what_is_left(site):
    from app import messages

    blocks, text = messages.alerts_granted(50, 52)
    assert "50" in text and "52" in text
    assert "52" in str(blocks)


def test_an_unmetered_plan_is_not_reported_as_a_billion_alerts(site):
    """`remaining` is 10**9 on a paid plan. Printing that verbatim would be
    the single most obviously broken thing Foxy could say."""
    from app import messages

    blocks, text = messages.alerts_granted(50, 10**9)
    assert "1,000,000,000" not in text and "1,000,000,000" not in str(blocks)
    assert "unlimited" in text


def test_the_channel_nudge_points_at_that_workspace_settings(site):
    from app import messages

    blocks, text = messages.channel_missing("Foxy Land", "abc123")
    assert "Foxy Land" in str(blocks)

    button = [b for b in blocks if b["type"] == "actions"][0]["elements"][0]
    assert button["url"] == f"{BASE}/app/abc123", "a generic link makes them hunt"
    assert "no channel" in text.lower()


def test_the_channel_nudge_reads_normally_without_a_team_name(site):
    from app import messages

    blocks, _ = messages.channel_missing("", "abc123")
    body = str(blocks)
    assert "installed in **" not in body and "in *" not in body.replace("in *Foxy", "")
    check_blocks(*messages.channel_missing("", "abc123"))
