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


def test_the_upgrade_page_quotes_the_price_from_the_manifest(db, client, monkeypatch):
    """The page and the plan Pond charges for must come from one place."""
    from app.config import settings

    monkeypatch.setattr(settings, "price_monthly_minor", 300)
    monkeypatch.setattr(settings, "pro_included_results", 2000)

    r = client.get(f"/app/{_install(db, team_id='T-UP')}/upgrade")
    assert r.status_code == 200
    assert "$3" in r.text
    assert "2,000" in r.text
    assert settings.pond_listing_url in r.text, "there must be a way to subscribe"


def test_a_workspace_on_pro_is_not_asked_to_upgrade(db, client):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(
        db, team_id="T-PRO", plan="pro", plan_until=now + dt.timedelta(days=30)
    )
    r = client.get(f"/app/{install_id}/upgrade")
    assert r.status_code == 200
    assert "You are on Foxy Pro" in r.text
    assert "Subscribe on Pond" not in r.text, "no upsell to someone already paying"


def test_foxy_does_not_take_payment_itself(db, client):
    """Pond sells and collects. Foxy declares the plans and meters usage."""
    from app.config import settings

    r = client.get(f"/app/{_install(db, team_id='T-NOPAY')}/upgrade")
    assert r.status_code == 200
    assert "billed by Pond" in r.text
    assert settings.pond_listing_url in r.text


def test_an_expired_plan_sees_the_upgrade_page_again(db, client):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(
        db, team_id="T-EXP", plan="pro", plan_until=now - dt.timedelta(days=1)
    )
    r = client.get(f"/app/{install_id}/upgrade")
    assert "Subscribe on Pond" in r.text, "an expired plan is the free plan"


# --- the plans Pond imports and bills ----------------------------------------


def test_the_manifest_declares_plans_pond_can_import():
    from app.main import manifest

    plans = manifest()["metadata"]["pricing_plans"]
    assert len(plans) >= 2
    by_model = {p["pricing_model"] for p in plans}
    assert {"free", "subscription"} <= by_model


def test_every_plan_states_an_allowance():
    """Pond's schema requires included_units and it must be positive, so
    "unlimited" is not something a plan can say. Each one names a number."""
    from app.main import manifest

    for plan in manifest()["metadata"]["pricing_plans"]:
        if plan["pricing_model"] in {"free", "subscription"}:
            assert plan.get("included_units", 0) >= 1, plan["name"]


def test_the_subscription_is_monthly_because_pond_allows_nothing_else():
    """billing_interval is a const in the schema. A yearly plan cannot be
    expressed, so nothing in the codebase should imply one exists."""
    import json
    import pathlib

    schema = json.loads(
        pathlib.Path("tests/data/pond-manifest-schema.json").read_text(encoding="utf-8")
    )
    interval = schema["$defs"]["importablePricingPlan"]["properties"]["billing_interval"]
    assert interval.get("const") == "month"

    from app.main import manifest

    for plan in manifest()["metadata"]["pricing_plans"]:
        if plan["pricing_model"] == "subscription":
            assert plan["billing_interval"] == "month"


def test_the_billed_unit_matches_what_the_agent_reports():
    """Pond meters on the usage every terminal response carries. If the plan
    counted something else, the customer would be charged for a unit the agent
    never reports."""
    from app.main import _usage, manifest

    reported = _usage(1)["unit_of_measurement"]
    for plan in manifest()["metadata"]["pricing_plans"]:
        assert plan["usage_unit"] == reported, plan["name"]


def test_the_plans_validate_against_ponds_schema():
    """The whole manifest, checked the way Pond checks it."""
    import json
    import pathlib

    jsonschema = pytest.importorskip("jsonschema")

    from app.main import manifest

    schema = json.loads(
        pathlib.Path("tests/data/pond-manifest-schema.json").read_text(encoding="utf-8")
    )
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(manifest()))
    assert not errors, [e.message for e in errors[:3]]


# --- what the settings page does when the allowance is gone -------------------


def test_the_save_buttons_are_disabled_once_the_allowance_is_gone(db, client):
    """A control that looks live and quietly changes nothing is worse than one
    that is plainly unavailable."""
    from app.config import settings

    install_id = _install(db, team_id="T-FULL", alerts_used=settings.free_alert_quota)
    r = client.get(f"/app/{install_id}")

    assert r.status_code == 200
    assert "used up" in r.text, "it has to say why"
    assert _disabled_buttons(r.text) == 2, "both save controls must be switched off"


def test_a_workspace_under_the_cap_can_still_save(db, client):
    install_id = _install(db, team_id="T-ROOM", alerts_used=3)
    r = client.get(f"/app/{install_id}")
    assert _disabled_buttons(r.text) == 0
    assert "used up" not in r.text


def test_no_underlined_text_link_is_used_as_a_button(db, client):
    """Bare links reading as calls to action look unfinished. Anything that
    asks to be clicked is styled as a button."""
    from app.config import settings

    for used in (0, settings.free_alert_quota):
        install_id = _install(db, team_id=f"T-LINKS-{used}", alerts_used=used)
        for path in (f"/app/{install_id}", f"/app/{install_id}/upgrade"):
            text = client.get(path).text
            for phrase in ("Upgrade to Pro", "See Foxy Pro", "Back to settings",
                           "Subscribe on Pond"):
                if phrase in text:
                    before = text[: text.index(phrase)]
                    tag = before[before.rindex("<a ") : ]
                    assert 'class="btn"' in tag or 'class="ghost"' in tag, (
                        f"{phrase!r} on {path} is a bare link, not a button"
                    )


def test_the_upgrade_page_says_what_you_get_not_just_what_it_costs(db, client):
    r = client.get(f"/app/{_install(db, team_id='T-PERKS')}/upgrade")
    text = r.text
    assert "Foxy Pro" in text
    assert "Remove the limit" not in text, "that heading described a cap, not a product"
    # The reason the tool exists should be the first thing on the list.
    assert "before YC publishes" in text
    assert text.count("<li>") >= 4, "a price with no perks is not a plan"

    # ...and stated briefly. A paragraph beside a price is an essay, not a plan.
    import re

    perks = re.findall(r"<li>(.*?)</li>", text, re.S)
    longest = max(len(re.sub(r"<[^>]+>", "", x).strip()) for x in perks)
    assert longest <= 70, f"the longest perk runs to {longest} characters"


def _disabled_buttons(page: str) -> int:
    """How many <button> tags actually carry the attribute.

    Counting the word across the whole document also catches the stylesheet,
    which is how the first version of this test passed for the wrong reason.
    """
    import re

    return sum(
        1 for tag in re.findall(r"<button[^>]*>", page) if "disabled" in tag
    )
