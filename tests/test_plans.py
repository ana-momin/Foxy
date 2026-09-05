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



def _disabled_buttons(page: str) -> int:
    """How many <button> tags actually carry the attribute.

    Counting the word across the whole document also catches the stylesheet,
    which is how the first version of this test passed for the wrong reason.
    """
    import re

    return sum(
        1 for tag in re.findall(r"<button[^>]*>", page) if "disabled" in tag
    )


# --- proving a customer paid -------------------------------------------------


def test_a_claim_code_is_stable_and_does_not_leak_the_install_id(db):
    """Pond tells the agent nothing about who is calling, so a subscription
    bought there cannot be matched to a Slack workspace on its own. The code is
    what a customer quotes to join the two.

    It must not be the install id: that id is the secret guarding the settings
    page, and this gets emailed around.
    """
    from app import installs

    install_id = _install(db, team_id="T-CLAIM")
    with db.session() as s:
        row = installs.get(s, install_id)
        code, again = row.claim_code, row.claim_code

    assert code == again, "quoting it twice must give the same answer"
    assert install_id not in code
    assert code.startswith("FOXY-")


def test_two_workspaces_never_share_a_claim_code(db):
    from app import installs

    codes = set()
    for n in range(6):
        with db.session() as s:
            codes.add(installs.get(s, _install(db, team_id=f"T-C{n}")).claim_code)
    assert len(codes) == 6


def test_a_plan_can_be_activated_from_the_quoted_code(db):
    """The whole point: what the customer sends is what switches Pro on."""
    import argparse

    from app import installs
    from app.cli import cmd_set_plan

    install_id = _install(db, team_id="T-ACTIVATE")
    with db.session() as s:
        code = installs.get(s, install_id).claim_code

    assert cmd_set_plan(argparse.Namespace(workspace=code, plan="pro", months=1)) == 0
    assert _read(db, install_id)["plan_active"] is True


def test_an_unknown_code_activates_nothing(db):
    import argparse

    from app.cli import cmd_set_plan

    install_id = _install(db, team_id="T-SAFE")
    assert cmd_set_plan(
        argparse.Namespace(workspace="FOXY-DEAD-BEEF", plan="pro", months=1)
    ) == 1
    assert _read(db, install_id)["plan_active"] is False



# --- the operator console ----------------------------------------------------


ADMIN = "admin-key-for-the-suite"


@pytest.fixture()
def admin(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "admin_key", ADMIN)
    return client


def test_the_console_is_shut_without_the_key(db, admin):
    for url in ("/admin", "/admin?key=", "/admin?key=wrong"):
        r = admin.get(url)
        assert r.status_code == 200
        assert "Workspaces" not in r.text, f"{url} exposed the console"


def test_the_console_is_shut_when_no_key_is_configured(db, admin, monkeypatch):
    """An unset key must close the door, not leave it open.

    Defaulting to reachable is how a deployment ends up with a billing console
    anyone can find.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "admin_key", "")
    assert "Workspaces" not in admin.get("/admin?key=").text
    assert "Workspaces" not in admin.get("/admin").text


def test_the_console_lists_workspaces(db, admin):
    _install(db, team_id="T-LIST")
    r = admin.get(f"/admin?key={ADMIN}")
    assert "Workspaces" in r.text
    assert "Test" in r.text


def test_a_plan_is_switched_on_with_one_press(db, admin):
    """The whole point: no terminal, for anybody."""
    install_id = _install(db, team_id="T-PRESS")
    assert _read(db, install_id)["plan_active"] is False

    r = admin.post(
        "/admin/plan",
        data={"key": ADMIN, "install_id": install_id, "months": 12},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _read(db, install_id)["plan_active"] is True


def test_a_plan_cannot_be_changed_without_the_key(db, admin):
    install_id = _install(db, team_id="T-NOKEY")
    admin.post(
        "/admin/plan",
        data={"key": "wrong", "install_id": install_id, "months": 12},
        follow_redirects=False,
    )
    assert _read(db, install_id)["plan_active"] is False, "billing changed unauthorised"


def test_downgrading_is_one_press_too(db, admin):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(
        db, team_id="T-DOWN", plan="pro", plan_until=now + dt.timedelta(days=30)
    )
    admin.post(
        "/admin/plan",
        data={"key": ADMIN, "install_id": install_id, "months": 0},
        follow_redirects=False,
    )
    assert _read(db, install_id)["plan_active"] is False




def test_changing_a_plan_needs_a_post(db, admin):
    """A GET that changes billing would fire on anything that follows links."""
    install_id = _install(db, team_id="T-GET")
    r = admin.get(f"/admin/plan?key={ADMIN}&install_id={install_id}&months=12")
    assert r.status_code == 405
    assert _read(db, install_id)["plan_active"] is False


# --- what the page may and may not promise -----------------------------------


def test_the_page_does_not_sell_what_cannot_be_verified(db, client):
    """A Slack workspace has no way to prove it paid.

    Pond bills Pond users and enforces their allowance before the agent is
    called, which works and needs nothing from us. A Slack install is not a
    Pond user, and the protocol carries no subscriber identity - so a price and
    a Buy button here would take money on a promise nobody could check. That is
    worse than not selling at all, and it is what this asserts stays absent.
    """
    page = client.get(f"/app/{_install(db, team_id='T-HONEST')}/upgrade").text

    assert "$3" not in page and "per month" not in page
    assert "Subscribe" not in page
    assert "I have subscribed" not in page
    assert "/subscribed" not in page, "a button that grants nothing it can verify"


def test_the_page_offers_a_way_to_ask_for_more(db, client):
    """Hitting the cap with no remedy is its own kind of broken."""
    from app.config import settings

    page = client.get(f"/app/{_install(db, team_id='T-ASK')}/upgrade").text
    assert "Need more alerts?" in page
    assert settings.support_email in page


def test_a_workspace_without_a_cap_is_not_asked_for_anything(db, client):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(
        db, team_id="T-UNCAPPED", plan="pro", plan_until=now + dt.timedelta(days=30)
    )
    page = client.get(f"/app/{install_id}/upgrade").text
    assert "No limit on this workspace" in page
    assert "Need more alerts?" not in page


def test_an_expired_plan_is_asked_again(db, client):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(
        db, team_id="T-LAPSED", plan="pro", plan_until=now - dt.timedelta(days=1)
    )
    assert "Need more alerts?" in client.get(f"/app/{install_id}/upgrade").text


def test_pond_still_carries_the_real_plans():
    """The half that works stays. Pond sells to Pond users and enforces the
    allowance itself; removing that would give away the only billing there is.
    """
    from app.main import manifest

    models = {p["pricing_model"] for p in manifest()["metadata"]["pricing_plans"]}
    assert "subscription" in models
