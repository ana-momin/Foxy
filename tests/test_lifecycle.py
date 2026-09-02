"""The whole journey a workspace goes through, end to end.

Both bugs that reached production today lived in this gap. Every unit passed;
nothing walked a workspace from install to second sweep and asserted what it
actually received:

  * a brand-new workspace got nothing at all on its first sweep, because the
    backfill guard suppressed every signal
  * a workspace installed after any earlier sweep got everything at once, 263
    messages, because the first-run guard read a single global counter

Both are obvious the moment you look at delivery over time rather than at one
function. This runs against a throwaway SQLite file with stub sources, so it
needs no network and no credentials.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import tempfile

import pytest


@pytest.fixture()
def world(monkeypatch):
    """A fresh database, stub sources, and a Slack that records instead of posts."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "lifecycle.db"
    url = f"sqlite:///{tmp}"

    from app.config import settings

    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "min_confidence", 0.5)
    monkeypatch.setattr(settings, "backfill_days", 7)

    import app.db as db

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_schema_ready", False)

    from app import installs  # noqa: F401  (registers the installs table)

    db.init_db()

    from app.models import Signal

    now = dt.datetime.now(dt.timezone.utc)

    def company(i: int, days_ago: float) -> Signal:
        return Signal(
            source="yc_directory",
            external_id=f"c{i}",
            title=f"Company {i}",
            url=f"https://www.ycombinator.com/companies/c{i}",
            description="Does a thing.",
            company_name=f"Company {i}",
            batch="Fall 2026",
            posted_at=now - dt.timedelta(days=days_ago),
            confirmed=True,
        )

    # A realistic spread: a few recent, plenty of history.
    catalogue = [company(i, i * 3) for i in range(1, 26)]

    posted: list[str] = []

    class RecordingSlack:
        """Stands in for a configured client. `usable` matters: delivery is
        gated on it, and a stub without it silently records nothing."""

        usable = True

        def __init__(self, *a, **k):
            pass

        def post(self, blocks, text, **kw):
            posted.append(text)
            return {"ok": True, "channel": "C1", "ts": f"{len(posted)}.0"}

        def join_channel(self, *a, **k):
            return True

        def list_channels(self):
            return []

    import app.engine as engine_mod

    monkeypatch.setattr(engine_mod, "SlackClient", RecordingSlack)

    return {
        "db": db,
        "signals": {"yc_directory": catalogue},
        "posted": posted,
        "Slack": RecordingSlack,
    }


def _sweep(world, namespace: str):
    from app.engine import Engine

    posted_before = len(world["posted"])
    engine = Engine(slack=world["Slack"](), namespace=namespace)
    result = engine.sweep(prefetched={"yc_directory": world["signals"]["yc_directory"]})
    return result, len(world["posted"]) - posted_before


# --- the journey ------------------------------------------------------------


def test_a_new_workspace_hears_something_on_its_first_sweep(world):
    """The bug: it heard nothing, and the channel stayed empty until some
    company happened to appear, which could be a day later."""
    result, posted = _sweep(world, "i:new:")
    assert posted > 0, "a first sweep must introduce something"
    assert len(result.alerts) > 0


def test_a_first_sweep_does_not_dump_history(world):
    """The other half: 25 companies exist, most of them old."""
    from app.config import settings

    result, posted = _sweep(world, "i:new:")
    assert posted <= settings.first_run_alerts, "a first sweep must stay small"


def test_the_second_sweep_is_silent(world):
    """Nothing may be reported twice."""
    _sweep(world, "i:new:")
    result, posted = _sweep(world, "i:new:")
    assert posted == 0, f"second sweep posted {posted}"
    assert result.alerts == []


def test_only_genuinely_new_items_arrive_later(world):
    """After the introduction, a company added since must come through."""
    from app.models import Signal

    _sweep(world, "i:new:")

    fresh = Signal(
        source="yc_directory",
        external_id="brand-new",
        title="Fresh Co",
        url="https://www.ycombinator.com/companies/fresh",
        description="Just listed.",
        company_name="Fresh Co",
        batch="Fall 2026",
        posted_at=dt.datetime.now(dt.timezone.utc),
        confirmed=True,
    )
    world["signals"]["yc_directory"] = world["signals"]["yc_directory"] + [fresh]

    result, posted = _sweep(world, "i:new:")
    assert posted == 1, f"expected exactly the new company, got {posted}"
    assert result.alerts[0].company_name == "Fresh Co"


def test_a_workspace_joining_later_gets_its_own_introduction(world):
    """The 263-message bug: the first-run guard was global, so the second
    workspace to install was treated as an existing one and received the entire
    catalogue at once."""
    from app.config import settings

    _sweep(world, "i:first:")          # an earlier workspace has already swept
    result, posted = _sweep(world, "i:second:")

    assert posted > 0, "a later workspace must still get an introduction"
    assert posted <= settings.first_run_alerts, f"it received {posted} at once"


def test_workspaces_do_not_consume_each_others_detections(world):
    a_result, a_posted = _sweep(world, "i:a:")
    b_result, b_posted = _sweep(world, "i:b:")
    assert a_posted > 0 and b_posted > 0
    a_names = {s.company_name for s in a_result.alerts}
    b_names = {s.company_name for s in b_result.alerts}
    assert a_names == b_names, "both should see the same newest companies"


def test_one_sweep_can_never_exceed_the_ceiling(world, monkeypatch):
    """Whatever else goes wrong, a channel must not be filled."""
    from app.config import settings
    from app.engine import Engine

    monkeypatch.setattr(settings, "max_alerts_per_sweep", 4)

    # Not a first run, and nothing seen, so every signal is eligible.
    from app.db import Seen, session

    with session() as s:
        s.add(
            Seen(
                fingerprint="i:cap:seed",
                source="yc_directory",
                external_id="seed",
                entity_key="i:cap:seed",
            )
        )

    before = len(world["posted"])
    engine = Engine(slack=world["Slack"](), namespace="i:cap:")
    result = engine.sweep(prefetched={"yc_directory": world["signals"]["yc_directory"]})

    # Four alerts, and the overflow arrives as a single digest message rather
    # than as twenty-one more notifications.
    assert len(result.alerts) <= 4
    assert len(world["posted"]) - before <= 5
    assert len(result.digest) > 0, "the overflow must still be recorded"


# --- delivery actually happening --------------------------------------------


def test_alerts_are_delivered_not_merely_recorded(world):
    """687 alerts were recorded and none were ever sent.

    Delivery was gated on the global SLACK_BOT_TOKEN. In hosted mode there is
    no global token, since every workspace carries its own, so every sweep took
    the dry-run path: it logged, wrote a row, and posted nothing. The sweep
    result still said "15 alerts", because that counts decisions rather than
    deliveries.
    """
    from app.db import Alert, session
    from sqlalchemy import select

    result, posted = _sweep(world, "i:deliver:")
    assert posted == len(result.alerts), "every alert must reach Slack"

    with session() as s:
        stamps = [r.ts for r in s.execute(select(Alert)).scalars().all()]
    sent = [t for t in stamps if t]
    assert len(sent) == len(result.alerts), "a recorded alert must carry its message id"


def test_an_unusable_client_is_not_treated_as_delivery(world, monkeypatch):
    """The inverse: with nowhere to post, nothing may be recorded as sent."""
    from app.db import Alert, session
    from app.engine import Engine
    from sqlalchemy import select

    class NoTarget:
        usable = False

        def post(self, *a, **k):
            raise AssertionError("must not attempt to post")

    engine = Engine(slack=NoTarget(), namespace="i:notarget:")
    engine.sweep(prefetched={"yc_directory": world["signals"]["yc_directory"]})

    with session() as s:
        # Read the values inside the session; a detached row cannot be lazily
        # refreshed once it closes.
        stamps = [r.ts for r in s.execute(select(Alert)).scalars().all()]
    assert all(t is None for t in stamps), "nothing may claim to have been sent"


def test_delivery_is_decided_by_the_client_not_global_settings(world, monkeypatch):
    """Hosted mode has no global token. Reading one was the whole bug."""
    import inspect

    from app import engine as engine_mod

    src = inspect.getsource(engine_mod.Engine._deliver)
    assert "slack_configured" not in src, "delivery must not consult global settings"
    assert "self.slack.usable" in src


def test_a_failed_post_is_not_counted_as_an_alert(world, monkeypatch):
    """The workspace saw "6 of 50 alerts used" and an empty channel.

    chat.postMessage failed, the exception was logged and swallowed, and the
    sweep still reported the alerts it had decided on. Quota was charged for
    messages nobody received.
    """
    from app.engine import Engine

    class FailingSlack:
        usable = True

        def post(self, *a, **k):
            raise RuntimeError("Slack chat.postMessage failed: not_in_channel")

    engine = Engine(slack=FailingSlack(), namespace="i:fail:")
    result = engine.sweep(prefetched={"yc_directory": world["signals"]["yc_directory"]})

    assert result.alerts, "the sweep should still have decided on some"
    assert result.delivered == [], "nothing was delivered"
    assert result.delivery_errors, "and the failure must be reported, not swallowed"
    assert not result.ok, "a sweep that delivered nothing is not a success"


def test_delivery_is_counted_from_slacks_own_reply(world):
    """A message id from Slack is the only evidence of delivery."""
    from app.engine import Engine

    class NoTimestamp:
        """Answers ok, but never returns a ts. Nothing was really posted."""

        usable = True

        def post(self, *a, **k):
            return {"ok": True, "channel": "C1"}

    engine = Engine(slack=NoTimestamp(), namespace="i:nots:")
    result = engine.sweep(prefetched={"yc_directory": world["signals"]["yc_directory"]})
    assert result.delivered == []
    assert result.undelivered == len(result.alerts)


def test_an_unusable_client_reports_why(world):
    """Silence is what hid this for a whole day."""
    from app.engine import Engine

    class NoTarget:
        usable = False
        token = ""
        target = ""

        def post(self, *a, **k):
            raise AssertionError("must not attempt to post")

    engine = Engine(slack=NoTarget(), namespace="i:silent:")
    result = engine.sweep(prefetched={"yc_directory": world["signals"]["yc_directory"]})
    assert result.delivery_errors, "an undeliverable sweep must say so"
    assert not result.ok


def test_every_row_a_sweep_writes_belongs_to_its_workspace(world):
    """Seen, Entity and Alert are all per-workspace state.

    Alert and Entity were not namespaced. Two consequences, both silent: an
    alert row could not be attributed to the workspace that received it, and
    `confirm_notified` was shared, so the first workspace to sweep marked a
    promotion delivered on behalf of every other one.
    """
    from app.db import Alert, Entity, Seen, session
    from sqlalchemy import select

    _sweep(world, "i:one:")

    with session() as s:
        keys = (
            [r.fingerprint for r in s.execute(select(Seen)).scalars().all()]
            + [r.fingerprint for r in s.execute(select(Alert)).scalars().all()]
            + [r.entity_key for r in s.execute(select(Entity)).scalars().all()]
        )

    assert keys, "the sweep must have written something"
    stray = [k for k in keys if not k.startswith("i:one:")]
    assert not stray, f"rows not attributable to the workspace: {stray[:5]}"


def test_a_promotion_belongs_to_one_workspace_only(world):
    """Workspace A's sweep must not consume B's pending confirmations."""
    from app.db import Entity, session
    from sqlalchemy import select

    _sweep(world, "i:a:")
    _sweep(world, "i:b:")

    with session() as s:
        by_ns = {}
        for e in s.execute(select(Entity)).scalars().all():
            by_ns.setdefault(e.entity_key.split(":")[1], []).append(e)

    assert set(by_ns) == {"a", "b"}, f"expected both workspaces, got {set(by_ns)}"
    assert len(by_ns["a"]) == len(by_ns["b"]), "each keeps its own copy"


def test_an_undelivered_alert_is_offered_again_next_sweep(world, monkeypatch):
    """An outage must delay alerts, not delete them.

    Items were marked seen at decision time, so a sweep that failed to deliver
    still recorded them as reported. The workspace was then permanently silent
    about every company collected during the outage - which is exactly what
    happened: 378 items marked seen, nothing ever sent.
    """
    from app.engine import Engine

    class Broken:
        usable = True

        def post(self, *a, **k):
            raise RuntimeError("not_in_channel")

    broken = Engine(slack=Broken(), namespace="i:retry:")
    failed = broken.sweep(prefetched={"yc_directory": world["signals"]["yc_directory"]})
    assert failed.alerts and not failed.delivered

    # Slack comes back. The same companies must still be waiting.
    result, posted = _sweep(world, "i:retry:")
    assert posted == len(failed.alerts), "what never arrived must be offered again"


def test_a_sweep_does_not_query_the_database_once_per_signal(world):
    """The welcome sweep took 164 seconds and the first attempt was killed at
    the five-minute serverless limit.

    Fetching was never the problem - all sources together answer in about
    nineteen seconds. The engine asked the database about each signal three
    times over: had it been seen, did its entity exist, and again inside
    mark_seen. Against a hosted Postgres each round trip costs tens of
    milliseconds, and a sweep looks at several hundred signals.
    """
    from sqlalchemy import event

    from app.db import engine as get_engine

    queries: list[str] = []

    eng = get_engine()

    def count(conn, cursor, statement, *a):
        queries.append(statement)

    event.listen(eng, "before_cursor_execute", count)
    try:
        _sweep(world, "i:count:")
    finally:
        event.remove(eng, "before_cursor_execute", count)

    signals = len(world["signals"]["yc_directory"])
    selects = [q for q in queries if q.lstrip().upper().startswith("SELECT")]
    import collections

    tally = collections.Counter(" ".join(q.split())[:70] for q in selects)
    breakdown = "\n".join(f"  {n:>3}x {q}" for q, n in tally.most_common(6))
    assert len(selects) < signals, (
        f"{len(selects)} SELECTs for {signals} signals - "
        f"the per-signal lookups are back\n{breakdown}"
    )


def test_a_source_met_later_introduces_itself_rather_than_flooding(world, monkeypatch):
    """What a new workspace actually experiences, in order.

    The welcome sweep reads only the two YC feeds, so Speedrun, X and LinkedIn
    are first read on the next scheduled sweep. Treating them as established
    then meant the workspace got the alert ceiling plus a digest of hundreds -
    Foxy Land went 6 alerts to 31 overnight.
    """
    import datetime as dt

    from app.config import settings
    from app.models import Signal

    now = dt.datetime.now(dt.timezone.utc)
    speedrun = [
        Signal(
            source="speedrun",
            external_id=f"sr{i}",
            title=f"Speed Co {i}",
            url=f"https://speedrun.a16z.com/{i}",
            description="Fast.",
            company_name=f"Speed Co {i}",
            program="Speedrun",
            posted_at=now - dt.timedelta(days=i),
            confirmed=True,
        )
        for i in range(1, 200)
    ]

    # 1. the welcome: the YC feed only
    _, welcomed = _sweep(world, "i:later:")
    assert 0 < welcomed <= settings.first_run_alerts

    # 2. the first full sweep, where a new source appears
    from app.engine import Engine

    before = len(world["posted"])
    engine = Engine(slack=world["Slack"](), namespace="i:later:")
    result = engine.sweep(
        prefetched={
            "yc_directory": world["signals"]["yc_directory"],
            "speedrun": speedrun,
        }
    )
    posted = len(world["posted"]) - before

    assert posted > 0, "a newly met source must introduce itself"
    assert posted <= settings.first_run_alerts + 1, (
        f"199 companies produced {posted} messages - that is a flood, not an "
        "introduction"
    )
    assert not result.digest, "seed the rest quietly rather than summarising them"


def test_a_source_without_dates_can_still_introduce_itself(world):
    """Speedrun and LinkedIn company pages carry no timestamps at all.

    The introduction picked the newest few by posted_at, so a source with no
    dates contributed nothing to it - a new workspace saw none of the 258
    Speedrun companies waiting for it, and Speedrun is named in the brief as a
    source to monitor.
    """
    from app.config import settings
    from app.engine import Engine
    from app.models import Signal

    undated = [
        Signal(
            source="speedrun",
            external_id=f"u{i}",
            title=f"Undated Co {i}",
            url=f"https://speedrun.a16z.com/{i}",
            description="No timestamp anywhere.",
            company_name=f"Undated Co {i}",
            program="Speedrun",
            posted_at=None,
            confirmed=True,
        )
        for i in range(1, 60)
    ]

    before = len(world["posted"])
    engine = Engine(slack=world["Slack"](), namespace="i:undated:")
    engine.sweep(prefetched={"speedrun": undated})
    posted = len(world["posted"]) - before

    assert posted > 0, "a dateless source must still introduce itself"
    assert posted <= settings.first_run_alerts, "but only a handful of them"
