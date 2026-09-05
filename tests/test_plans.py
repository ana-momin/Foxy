"""Plans, expiry and the search allowance.

Money makes these worth being strict about. A plan that outlives its payment
costs revenue; a plan that expires early costs a customer; and a search
allowance nobody counts runs out silently, taking early detection with it.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import tempfile

import pytest

os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-the-suite")


@pytest.fixture()
def db(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings,
        "database_url",
        f"sqlite:///{pathlib.Path(tempfile.mkdtemp()) / 'plans.db'}",
    )
    import app.db as database

    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_schema_ready", False)
    from app import installs  # noqa: F401  registers the table

    database.init_db()
    return database


def _install(db, **kw):
    from app import installs

    with db.session() as s:
        row = installs.upsert(
            s, team_id=kw.pop("team_id", "T1"), team_name="Test", token="xoxb-1"
        )
        row.channel_id = "C1"
        for k, v in kw.items():
            setattr(row, k, v)
        return row.id


def _read(db, install_id):
    from app import installs

    with db.session() as s:
        row = installs.get(s, install_id)
        return {
            "plan_active": row.plan_active,
            "quota": row.quota,
            "remaining": row.remaining,
            "label": row.plan_label,
        }


# --- expiry ------------------------------------------------------------------


def test_the_free_plan_is_metered(db):
    from app.config import settings

    got = _read(db, _install(db))
    assert got["plan_active"] is False
    assert got["quota"] == settings.free_alert_quota


def test_a_paid_plan_is_unmetered(db):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    got = _read(db, _install(db, plan="pro", plan_until=now + dt.timedelta(days=30)))
    assert got["plan_active"] is True
    assert got["quota"] == 0, "0 means unlimited"


def test_an_expired_plan_is_the_free_plan_again(db):
    """Checked when asked, not by a nightly job. A plan must not outlive its
    payment because some scheduled task failed to run."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    got = _read(db, _install(db, plan="pro", plan_until=now - dt.timedelta(days=1)))
    assert got["plan_active"] is False
    assert got["quota"] > 0, "metered again"
    assert got["label"] == "Free"


def test_a_plan_granted_without_an_end_date_does_not_expire(db):
    got = _read(db, _install(db, plan="pro", plan_until=None))
    assert got["plan_active"] is True


def test_paying_again_early_keeps_the_remaining_time(db):
    """Extending from today rather than from the existing end date would throw
    away whatever the customer had already paid for."""
    import argparse

    from app.cli import cmd_set_plan

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(db, plan="pro", plan_until=now + dt.timedelta(days=20))

    cmd_set_plan(
        argparse.Namespace(workspace=install_id, plan="pro", months=1)
    )

    from app import installs

    with db.session() as s:
        until = installs.get(s, install_id).plan_until
    # 20 days left plus a further 30, not 30 from today.
    assert (until - now).days >= 49, f"lost time: {(until - now).days} days"


def test_downgrading_clears_the_expiry(db):
    import argparse

    from app.cli import cmd_set_plan

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(db, plan="pro", plan_until=now + dt.timedelta(days=30))
    cmd_set_plan(argparse.Namespace(workspace=install_id, plan="free", months=1))

    got = _read(db, install_id)
    assert got["plan_active"] is False and got["label"] == "Free"


# --- the search allowance ----------------------------------------------------


def test_searches_are_counted(db):
    from app import budget

    budget.reset()
    for _ in range(5):
        budget.record_call()
    assert budget.snapshot()["used"] == 5


def test_the_warning_fires_once_and_only_when_low(db, monkeypatch):
    """A warning on every sweep is a warning nobody reads."""
    from app import budget
    from app.config import settings

    monkeypatch.setattr(settings, "serper_allowance", 100)
    budget.reset()

    budget.record_call(50)
    assert budget.should_warn() is False, "half spent is not low"

    budget.record_call(35)  # 85%
    assert budget.should_warn() is True
    assert budget.should_warn() is False, "it must not repeat"


def test_counting_never_breaks_a_search(db, monkeypatch):
    """Failing to count must not fail the thing being counted."""
    from app import budget

    def broken():
        raise RuntimeError("database is away")

    monkeypatch.setattr(budget, "session", broken)
    budget.record_call()  # must not raise
    assert budget.snapshot() == {"tracked": False}


def test_a_serper_search_is_counted(monkeypatch):
    """The counter has to sit on the call itself, or it drifts from reality."""
    import inspect

    from app.providers import websearch

    src = inspect.getsource(websearch._serper)
    assert "record_call()" in src
    assert src.index("record_call()") < src.index("google.serper.dev")


# --- the upgrade page --------------------------------------------------------


@pytest.fixture()
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_the_upgrade_page_quotes_the_configured_prices(db, client, monkeypatch):
    """One source for the price, so the page, the site and any notice agree."""
    from app.config import settings

    monkeypatch.setattr(settings, "price_monthly_usd", "3")
    monkeypatch.setattr(settings, "price_yearly_usd", "10")
    monkeypatch.setattr(settings, "pay_wallet", "0xabc123")

    r = client.get(f"/app/{_install(db, team_id='T-UP')}/upgrade")
    assert r.status_code == 200
    assert "$3" in r.text and "$10" in r.text
    assert "0xabc123" in r.text, "the address has to be on the page to be paid"


def test_a_workspace_on_pro_is_not_asked_to_upgrade(db, client):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(
        db, team_id="T-PRO", plan="pro", plan_until=now + dt.timedelta(days=30)
    )
    r = client.get(f"/app/{install_id}/upgrade")
    assert r.status_code == 200
    assert "You are on Pro" in r.text
    assert "Remove the limit" not in r.text


def test_the_page_still_works_before_payment_is_configured(db, client, monkeypatch):
    """No wallet set must not mean a broken page."""
    from app.config import settings

    monkeypatch.setattr(settings, "pay_wallet", "")
    r = client.get(f"/app/{_install(db, team_id='T-NOWALLET')}/upgrade")
    assert r.status_code == 200
    assert "not set up yet" in r.text


def test_an_expired_plan_sees_the_upgrade_page_again(db, client):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(
        db, team_id="T-EXP", plan="pro", plan_until=now - dt.timedelta(days=1)
    )
    r = client.get(f"/app/{install_id}/upgrade")
    assert "Remove the limit" in r.text, "an expired plan is the free plan"
