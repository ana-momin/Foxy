"""Interactive setup.

This exists because the alternative is telling someone to right-click a channel,
dig out an ID like C0ABC123DEF, hand-edit a dotfile, and hope. That is where
most people give up, so `python -m app.cli init` does it for them: validates the
token, lists the channels it can see, joins the one you pick, sends a test
message to prove it works, and only then writes `.env`.
"""

from __future__ import annotations

from .config import ROOT
from .slack import SlackClient

RULE = "  " + "-" * 56


def _banner() -> None:
    print()
    print("  Foxy setup")
    print(RULE)
    print()
    print("  Step 1 - create the Slack app (about two minutes):")
    print()
    print("     1. Open   https://api.slack.com/apps")
    print("     2. Click  Create New App  ->  From an app manifest")
    print("     3. Pick your workspace")
    print("     4. Paste in  slack-app-manifest.json  (in this folder)")
    print("     5. Click    Install to Workspace  ->  Allow")
    print("     6. Copy the Bot User OAuth Token from OAuth & Permissions")
    print()


def _ask_token() -> str | None:
    token = input("  Paste your bot token (xoxb-...): ").strip()
    if not token:
        print()
        print("  Nothing entered. Run this again when you have the token.")
        print()
        return None
    if not token.startswith("xoxb-"):
        print()
        print("  That does not look like a bot token.")
        print("  It should start with 'xoxb-' and live under")
        print("  OAuth & Permissions -> Bot User OAuth Token.")
        print("  (A token starting 'xapp-' or 'xoxp-' is a different one.)")
        print()
        return None
    return token


def _choose_channel(client: SlackClient) -> str:
    """Offer a numbered list where possible; fall back to asking for an ID."""
    channels = client.list_channels()

    if not channels:
        print("  I could not list your channels, which is fine - it just means")
        print("  the app does not have the channels:read scope.")
        print()
        print("  To find a channel ID: right-click the channel in Slack ->")
        print("  View channel details -> scroll to the very bottom.")
        print()
        return input("  Channel ID (or a user ID to get DMs): ").strip()

    # Channels the bot is already in are the likeliest choice, so show them first.
    channels.sort(key=lambda c: (not c.get("is_member"), c.get("name", "")))
    shown = channels[:25]

    print("  Step 2 - where should alerts go?")
    print()
    for i, c in enumerate(shown, 1):
        prefix = "*" if c.get("is_private") else "#"
        here = "   (already added)" if c.get("is_member") else ""
        print(f"     {i:>2}. {prefix}{c.get('name')}{here}")
    print()

    choice = input(f"  Enter 1-{len(shown)}, or paste a channel ID: ").strip()
    if not (choice.isdigit() and 1 <= int(choice) <= len(shown)):
        return choice

    picked = shown[int(choice) - 1]
    target = picked["id"]
    if not picked.get("is_member"):
        if client.join_channel(target):
            print(f"  Added Foxy to #{picked.get('name')}")
        else:
            print(f"  Could not join automatically.")
            print(f"  Run this in Slack:  /invite @Foxy   (in #{picked.get('name')})")
    return target


def _write_env(token: str, target: str) -> str:
    """Update .env without discarding anything already in it."""
    env_path = ROOT / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()

    values["SLACK_BOT_TOKEN"] = token
    values["SLACK_TARGET"] = target
    values.setdefault("X_PROVIDER", "free")
    values.setdefault("LINKEDIN_PROVIDER", "free")
    values.setdefault("SCAN_INTERVAL_HOURS", "8")
    values.setdefault("MIN_CONFIDENCE", "0.55")
    values.setdefault("BACKFILL_DAYS", "7")

    body = "\n".join(f"{k}={v}" for k, v in values.items()) + "\n"
    env_path.write_text(body, encoding="utf-8")
    return env_path.name


def run() -> int:
    _banner()

    token = _ask_token()
    if not token:
        return 1

    client = SlackClient(token=token, target="")
    try:
        info = client.auth_test()
    except Exception as exc:  # noqa: BLE001
        print()
        print(f"  Slack rejected that token: {exc}")
        print("  Check you copied the whole thing, and that the app is installed.")
        print()
        return 1

    print()
    print(f"  Connected to {info.get('team')} as @{info.get('user')}")
    print()

    target = _choose_channel(client)
    if not target:
        print()
        print("  No channel chosen - nothing was saved.")
        print()
        return 1

    print()
    print("  Step 3 - sending a test message...")
    try:
        SlackClient(token=token, target=target).post(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":wave: *Foxy is connected.*\n"
                            "New YC and Speedrun companies will appear here, "
                            "and so will founders who announce before YC does."
                        ),
                    },
                }
            ],
            "Foxy is connected.",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  Could not post there: {exc}")
        print()
        print("  If it is a private channel, invite Foxy first:")
        print("     /invite @Foxy")
        print("  then run this setup again.")
        print()
        return 1

    print("  Sent - go and check Slack.")
    print()

    name = _write_env(token, target)
    print(RULE)
    print(f"  Saved to {name}. Setup is done.")
    print()
    print("  Start Foxy:")
    print("     docker compose up -d")
    print("  or, without Docker:")
    print("     uvicorn app.main:app --port 8000")
    print()
    print("  Useful next:")
    print("     python -m app.cli sweep --dry    see what it finds, post nothing")
    print("     python -m app.cli status         per-source health")
    print()
    return 0
