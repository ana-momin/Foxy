"""Command line entry point.

    python -m app.cli init          # interactive setup - start here
    python -m app.cli check         # verify configuration, touch nothing
    python -m app.cli sweep         # run one sweep now
    python -m app.cli sweep --dry   # ...without posting to Slack
    python -m app.cli test-alert    # post one sample alert to prove Slack works
    python -m app.cli status        # per-source health
    python -m app.cli reset         # wipe local state (asks first)
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys

from .config import active_batch_codes, settings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------


def cmd_init(_args) -> int:
    """Interactive setup - validates the token, picks a channel, writes .env."""
    from .setup_wizard import run

    return run()


def cmd_channels(_args) -> int:
    """List channels Foxy can post to, with their IDs."""
    from .slack import SlackClient

    channels = SlackClient().list_channels()
    if not channels:
        print("\n  No channels listed. The app is probably missing the")
        print("  channels:read scope - reinstall it with the current")
        print("  slack-app-manifest.json to add it.\n")
        return 1
    print()
    for c in sorted(channels, key=lambda c: c.get("name", "")):
        mark = "in " if c.get("is_member") else "   "
        sym = "*" if c.get("is_private") else "#"
        print(f"  {mark} {c['id']}  {sym}{c.get('name')}")
    print()
    return 0


def cmd_check(_args) -> int:
    """Print exactly what is and is not configured, and why it matters."""
    from .engine import source_modes
    from .providers.websearch import engine_status

    print("\n  Foxy configuration\n  " + "-" * 52)

    ok = True
    if settings.slack_configured():
        try:
            from .slack import SlackClient

            info = SlackClient().auth_test()
            print(f"  Slack           OK    {info.get('team')} / bot {info.get('user')}")
            print(f"  Target          {settings.slack_target}")
        except Exception as exc:  # noqa: BLE001
            print(f"  Slack           FAIL  {exc}")
            ok = False
    else:
        print("  Slack           MISSING   set SLACK_BOT_TOKEN and SLACK_TARGET")
        print("                            (the bot runs without it in --dry mode)")

    db = settings.database_url
    print(f"  State           {'sqlite file' if db.startswith('sqlite') else 'postgres'}  {db.split('://')[0]}")

    print("\n  Sources")
    for name, mode in source_modes().items():
        flag = "off " if mode == "disabled" else "on  "
        print(f"    {flag} {name:<20} {mode}")

    print("\n  Free search engines")
    for name, state in engine_status().items():
        print(f"         {name:<20} {state}")

    print("\n  Classifier      " + ("Claude " + settings.classifier_model if settings.anthropic_api_key else "rules only (no ANTHROPIC_API_KEY)"))
    print(f"  Cadence         every {settings.scan_interval_hours}h")
    print(f"  Alert threshold {settings.min_confidence:.0%}")
    print(f"  Watching        {', '.join(active_batch_codes())}")

    if settings.pond_access_key and settings.public_base_url:
        print(f"  Pond agent      {settings.public_base_url}/manifest")
    else:
        print("  Pond agent      not configured (optional)")

    print()
    return 0 if ok else 1


def cmd_sweep(args) -> int:
    if args.dry:
        os.environ["DRY_RUN"] = "1"
        settings.dry_run = True

    from .engine import Engine

    started = dt.datetime.now()
    result = Engine().sweep(force_alerts=args.force)
    elapsed = (dt.datetime.now() - started).total_seconds()

    print(f"\n  Sweep finished in {elapsed:.1f}s\n  " + "-" * 52)
    for name, info in result.per_source.items():
        if info.get("error"):
            print(f"    FAIL  {name:<20} {info['error'][:60]}")
        else:
            print(f"    ok    {name:<20} {info['found']:>4} seen  {info['new']:>3} new")

    print(f"\n  Alerts posted   {len(result.alerts)}")
    for sig in result.alerts[:15]:
        kind = "EARLY " if sig.is_early else "listed"
        print(f"    [{kind}] {(sig.company_name or sig.title)[:28]:<28} {str(sig.batch or '-')[:12]:<12} {sig.source_label}")
    if result.digest:
        print(f"  Digest (low confidence)  {len(result.digest)}")
    print()
    return 0 if result.ok else 1


def cmd_test_alert(_args) -> int:
    """Post one realistic alert so you can confirm Slack delivery works."""
    from .models import Signal
    from .slack import SlackClient, build_alert

    sig = Signal(
        source="x",
        external_id="demo-1",
        title="Acme AI",
        url="https://x.com/example/status/123456",
        description=(
            "We got into YC F26! Solo founder, moving to SF next week to start "
            "building. Three years of nights and weekends finally paid off."
        ),
        company_name="Acme AI",
        company_url="https://acme.ai",
        batch="Fall 2026",
        author_name="Jane Doe",
        author_handle="janedoe",
        author_url="https://x.com/janedoe",
        confidence=0.91,
        raw={"likes": 2143, "classifier": "rules"},
    )
    sig.is_early = True
    sig.match_reason = "not in YC directory (closest was 62%)"

    blocks, text = build_alert(sig)
    if not settings.slack_configured():
        print("  SLACK_BOT_TOKEN / SLACK_TARGET not set. Rendered payload:\n")
        import json

        print(json.dumps(blocks, indent=2))
        return 1

    SlackClient().post(blocks, text)
    print(f"  Posted a sample early-signal alert to {settings.slack_target}")
    return 0


def cmd_check_post(args) -> int:
    """Run one real post URL through the whole pipeline.

    Useful on its own - somebody forwards you a tweet and you want to know
    whether it is a genuine early signal - and it is also the honest way to
    demonstrate the pipeline when search-engine discovery is being throttled.
    Nothing here is mocked: the post is fetched live, classified, and
    cross-referenced against the real YC directory.
    """
    import re

    from . import crossref
    from .classify import classify
    from .engine import Engine
    from .models import Signal
    from .providers.x_provider import STATUS_RE, hydrate

    m = STATUS_RE.search(args.url)
    if not m:
        print("  Not an X post URL. Expected https://x.com/<user>/status/<id>")
        return 1

    post = hydrate(m.group(2))
    if not post:
        print("  That post is not publicly available (deleted, private, or removed).")
        return 1

    when = f" — {post.created_at:%Y-%m-%d}" if post.created_at else ""
    print(f"\n  @{post.author_handle}{when}")
    print(f"  {post.text[:200]}\n")

    verdict = classify(post.text, author=post.author_handle)
    print(f"  Announcement : {verdict.is_announcement}  ({verdict.confidence:.0%})")
    print(f"  Company      : {verdict.company_name or 'not extracted'}")
    print(f"  Batch        : {verdict.batch or 'not stated'}")
    print(f"  Reasons      : {'; '.join(verdict.reasons[:3])}")

    if not verdict.is_announcement:
        print("\n  Not a founder announcement — no alert.\n")
        return 0

    match = crossref.lookup(verdict.company_name, None, text=post.text)
    print(f"  YC directory : {match.reason}")

    sig = Signal(
        source="x",
        external_id=post.id,
        title=verdict.company_name or f"@{post.author_handle}",
        url=post.url,
        description=post.text.strip(),
        company_name=verdict.company_name,
        batch=verdict.batch,
        program=verdict.program,
        author_name=post.author_name,
        author_handle=post.author_handle,
        author_url=post.author_url,
        posted_at=post.created_at,
        confidence=verdict.confidence,
        raw={"likes": post.likes, "classifier": "llm" if verdict.used_llm else "rules"},
    )
    sig.confirmed = match.found
    sig.is_early = match.is_early
    sig.match_reason = match.reason

    kind = "EARLY SIGNAL" if sig.is_early else ("confirmed" if match.found else "unverified")
    print(f"  Verdict      : {kind}\n")

    if args.post:
        from .db import init_db
        from .slack import SlackClient, build_alert

        init_db()
        blocks, text = build_alert(sig)
        SlackClient().post(blocks, text)
        print(f"  Posted to {settings.slack_target}\n")
    return 0


def cmd_hosted_sweep(_args) -> int:
    """Run one hosted sweep: fetch the sources once, deliver to every workspace.

    This is what the scheduler runs. It lives here rather than behind an HTTP
    call because a full sweep takes minutes and a serverless function is killed
    long before that; the runner has hours.
    """
    import json

    from .hosted import run_sweep

    result = run_sweep()
    print(json.dumps(result, indent=2, default=str))

    failed = [r for r in result.get("results", []) if r.get("error")]
    if failed:
        print(f"\n  {len(failed)} workspace(s) failed delivery")
        return 1
    return 0


def cmd_hosted_doctor(args) -> int:
    """Ask Slack, not our own logs, whether each workspace can be reached.

    Every hosted delivery bug so far was invisible from this side: the sweep
    reported alerts it had decided on, the database agreed, and the channel was
    empty. So this checks the chain end to end against Slack itself - the token
    decrypts, the token is live, the channel exists, the bot is in it - and with
    --post it sends a probe and reads it back.

    Nothing here prints a token.
    """
    from sqlalchemy import select

    from . import installs
    from .db import init_db, session
    from .slack import SlackClient

    init_db()
    with session() as s:
        rows = [
            {
                "id": i.id,
                "team": i.team_name,
                "channel": i.channel_id,
                "token": i.token,
                "enc": i.token_enc or "",
                "active": i.active,
                "used": i.alerts_used or 0,
                "err": i.last_error,
            }
            for i in s.execute(select(installs.Install)).scalars().all()
        ]

    if not rows:
        print("\n  no installs\n")
        return 0

    problems = 0
    for r in rows:
        print(f"\n  {r['team'] or r['id']}   active={r['active']}  alerts_used={r['used']}")
        if r["err"]:
            print(f"    last error       {r['err'][:120]}")

        problem = installs.token_problem(r["enc"])
        if problem:
            print(f"    token            UNUSABLE - {problem}")
            print("                     the workspace must reinstall Foxy")
            problems += 1
            continue
        print(f"    token            decrypts (key {installs.key_fingerprint()})")

        client = SlackClient(token=r["token"], target=r["channel"])
        try:
            who = client.auth_test()
            print(f"    auth.test        ok, @{who.get('user')} in {who.get('team')}")
        except Exception as exc:  # noqa: BLE001
            print(f"    auth.test        FAILED  {exc}")
            problems += 1
            continue

        if not r["channel"]:
            print("    channel          not configured")
            problems += 1
            continue

        try:
            ch = client.channel_info(r["channel"])
            member = ch.get("is_member")
            print(f"    channel          #{ch.get('name')}  bot_is_member={member}")
            if not member:
                print("                     alerts would fail with not_in_channel")
                problems += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    channel          FAILED  {exc}")
            problems += 1
            continue

        if getattr(args, "post", False):
            try:
                resp = client.post(
                    [{"type": "section",
                      "text": {"type": "mrkdwn", "text": ":wrench: Foxy delivery check."}}],
                    "Foxy delivery check",
                )
                ts = resp.get("ts")
                print(f"    test post        sent, ts={ts}")
                # Read it back. A ts we cannot find is not a delivery.
                # Reading the channel back needs channels:history, which Foxy
                # does not ask for - it posts, it does not read conversations.
                # The message id Slack returned is the acknowledgement.
                try:
                    hist = client._get(
                        "conversations.history", {"channel": r["channel"], "limit": 5}
                    )
                    seen = any(m.get("ts") == ts for m in hist.get("messages") or [])
                    print(
                        f"    read back        "
                        f"{'found in channel' if seen else 'NOT FOUND'}"
                    )
                    if not seen:
                        problems += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"    read back        skipped ({exc})")
            except Exception as exc:  # noqa: BLE001
                print(f"    test post        FAILED  {exc}")
                problems += 1

    print(f"\n  {problems} problem(s)\n")
    return 1 if problems else 0


def cmd_hosted_repair(_args) -> int:
    """Clear up after a delivery outage, using only facts we can check.

    Two kinds of wreckage are left when hosted delivery has been failing:

      * installs whose token this environment cannot read. They look healthy -
        active, configured, sweeping - and can never deliver. They are parked,
        with a reason the settings page can show, so the workspace is told to
        reinstall instead of waiting for alerts that cannot come.
      * alert rows with no Slack message id. Those were never sent, so they are
        not history; keeping them means the next sweep stays silent about
        companies nobody was ever told about.

    Quota is corrected to match: a workspace is only charged for alerts that
    carry a message id.
    """
    from sqlalchemy import delete, func, select

    from . import installs
    from .db import Alert, Entity, Seen, init_db, session

    init_db()
    with session() as s:
        rows = s.execute(select(installs.Install)).scalars().all()
        for row in rows:
            problem = installs.token_problem(row.token_enc or "")
            if problem:
                row.active = False
                row.last_error = f"{problem} - please reinstall Foxy"
                print(f"  {row.team_name:<12} parked: {problem[:70]}")
            else:
                print(f"  {row.team_name:<12} token reads fine")

        phantom = s.execute(
            select(func.count()).select_from(Alert).where(Alert.ts.is_(None))
        ).scalar()
        if phantom:
            s.execute(delete(Alert).where(Alert.ts.is_(None)))
            print(f"\n  {phantom} alert(s) had no Slack message id and were never sent")

        # A seen-set built while nothing could be delivered is not history.
        # Those companies were marked reported and never sent, so the workspace
        # would stay silent about all of them forever.
        for row in rows:
            delivered = s.execute(
                select(func.count())
                .select_from(Alert)
                .where(Alert.ts.isnot(None), Alert.fingerprint.like(f"{row.namespace}%"))
            ).scalar()
            if delivered:
                continue
            seen = s.execute(
                select(func.count())
                .select_from(Seen)
                .where(Seen.fingerprint.like(f"{row.namespace}%"))
            ).scalar()
            if seen:
                s.execute(delete(Seen).where(Seen.fingerprint.like(f"{row.namespace}%")))
                s.execute(
                    delete(Entity).where(Entity.entity_key.like(f"{row.namespace}%"))
                )
                print(
                    f"  {row.team_name:<12} forgot {seen} item(s) marked seen while "
                    "nothing could be sent"
                )

        # Recount from what Slack actually acknowledged.
        for row in rows:
            sent = s.execute(
                select(func.count())
                .select_from(Alert)
                .where(Alert.ts.isnot(None), Alert.fingerprint.like(f"{row.namespace}%"))
            ).scalar()
            if (row.alerts_used or 0) != sent:
                print(f"  {row.team_name:<12} quota {row.alerts_used} -> {sent} (delivered)")
                row.alerts_used = sent
                row.quota_notified = False

    print("")
    return 0


def cmd_status(_args) -> int:
    from .db import health_snapshot, init_db, session
    from .engine import source_modes

    init_db()
    with session() as s:
        snap = health_snapshot(s)

    print(f"\n  Sweeps completed  {snap['sweeps_completed']}")
    print(f"  Last sweep        {snap['last_sweep_at'] or 'never'}\n")
    modes = source_modes()
    for name, mode in modes.items():
        info = snap["sources"].get(name)
        if not info:
            print(f"    -     {name:<20} {mode:<28} not run yet")
        elif info["ok"]:
            print(f"    ok    {name:<20} {mode:<28} {info['found']} seen, {info['new']} new")
        else:
            print(f"    FAIL  {name:<20} {mode:<28} {(info['error'] or '')[:40]}")
    print()
    return 0


def cmd_reset(args) -> int:
    from .db import Base, engine, init_db

    if not args.yes:
        print("  This deletes all remembered companies and alert history.")
        print("  The next sweep will treat everything as new.")
        print("  Re-run with --yes to confirm.")
        return 1
    init_db()
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())
    print("  State cleared.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="foxy", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "init", help="interactive setup - start here"
    ).set_defaults(fn=cmd_init)
    sub.add_parser(
        "channels", help="list channels and their IDs"
    ).set_defaults(fn=cmd_channels)
    sub.add_parser(
        "hosted-sweep", help="run one sweep for every installed workspace"
    ).set_defaults(fn=cmd_hosted_sweep)
    sub.add_parser("check", help="verify configuration").set_defaults(fn=cmd_check)

    p = sub.add_parser("sweep", help="run one sweep now")
    p.add_argument("--dry", action="store_true", help="do not post to Slack")
    p.add_argument("--force", action="store_true", help="ignore the first-run backfill guard")
    p.set_defaults(fn=cmd_sweep)

    sub.add_parser("test-alert", help="post a sample alert").set_defaults(fn=cmd_test_alert)
    p = sub.add_parser("check-post", help="run one X post URL through the pipeline")
    p.add_argument("url")
    p.add_argument("--post", action="store_true", help="also send the alert to Slack")
    p.set_defaults(fn=cmd_check_post)

    p = sub.add_parser(
        "hosted-doctor", help="check every workspace against Slack itself"
    )
    p.add_argument(
        "--post", action="store_true", help="send a probe and read it back"
    )
    p.set_defaults(fn=cmd_hosted_doctor)
    sub.add_parser(
        "hosted-repair", help="park unreadable installs and drop unsent alerts"
    ).set_defaults(fn=cmd_hosted_repair)
    sub.add_parser("status", help="per-source health").set_defaults(fn=cmd_status)

    p = sub.add_parser("reset", help="wipe local state")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_reset)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
