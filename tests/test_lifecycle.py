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
