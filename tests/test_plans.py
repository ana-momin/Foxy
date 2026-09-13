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


def test_the_page_quotes_no_price_and_points_at_pond(db, client):
    """Pond sells the plans, enforces the allowance and takes the money.

    A figure repeated here is only somewhere for the two to disagree, and a
    Buy button would be worse: nothing on this side can tell a workspace that
    paid from one that says so.
    """
    from app.config import settings

    page = client.get(f"/app/{_install(db, team_id='T-HONEST')}/upgrade").text

    assert "$" not in page, "no prices on this page"
    assert settings.pond_listing_url in page
    assert "/subscribed" not in page, "nothing may grant a plan it cannot verify"


def test_a_workspace_without_a_cap_is_not_asked_for_anything(db, client):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(
        db, team_id="T-UNCAPPED", plan="pro", plan_until=now + dt.timedelta(days=30)
    )
    page = client.get(f"/app/{install_id}/upgrade").text
    assert "No limit on this workspace" in page
    assert "More alerts" not in page


def test_an_expired_plan_is_asked_again(db, client):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    install_id = _install(
        db, team_id="T-LAPSED", plan="pro", plan_until=now - dt.timedelta(days=1)
    )
    from app.config import settings

    assert settings.pond_listing_url in client.get(f"/app/{install_id}/upgrade").text


def test_pond_still_carries_the_real_plans():
    """The half that works stays. Pond sells to Pond users and enforces the
    allowance itself; removing that would give away the only billing there is.
    """
    from app.main import manifest

    models = {p["pricing_model"] for p in manifest()["metadata"]["pricing_plans"]}
    assert "subscription" in models


# --- the status console -------------------------------------------------------


def test_the_console_counts_delivered_alerts_not_decided_ones(db, admin):
    """The one number this page must never get wrong.

    Foxy once recorded 687 alerts and delivered none. A console that counted
    rows rather than Slack message ids would have shown a healthy wall of green
    throughout.
    """
    from app.admin import gather
    from app.db import Alert, session

    with session() as s:
        s.add(Alert(fingerprint="f1", entity_key="e1", source="x", kind="early",
                    confidence=1.0, ts="1.0", payload={}))
        s.add(Alert(fingerprint="f2", entity_key="e2", source="x", kind="early",
                    confidence=1.0, ts=None, payload={}))

    d = gather()
    assert d["delivered"] == 1, "an alert with no message id was never sent"
    assert d["early"] == 1


def test_a_workspace_at_its_cap_is_surfaced(db, admin):
    """A live user who has gone silent is the thing most worth noticing."""
    from app.admin import gather
    from app.config import settings

    _install(db, team_id="T-CAP", alerts_used=settings.free_alert_quota)
    d = gather()

    assert d["ok"] is False
    assert any("run out of alerts" in p for p in d["problems"]), d["problems"]
    assert d["workspaces"][0]["at_cap"] is True


def test_an_install_without_a_channel_is_surfaced(db, admin):
    """Authorised, then abandoned before choosing a channel - they get nothing
    and will not know why."""
    from app.admin import gather
    from app import installs

    with db.session() as s:
        row = installs.upsert(s, team_id="T-NOCH", team_name="Half", token="xoxb-1")
        row.channel_id = ""

    d = gather()
    assert any("not chosen a channel" in p for p in d["problems"]), d["problems"]


def test_a_quiet_system_reports_itself_as_fine(db, admin):
    from app.admin import gather

    _install(db, team_id="T-FINE", alerts_used=2)
    d = gather()
    assert d["ok"] is True and d["problems"] == []


def test_the_console_renders_the_dashboard(db, admin):
    _install(db, team_id="T-RENDER", alerts_used=5)
    page = admin.get(f"/admin?key={ADMIN}").text

    for section in ("Sources", "Workspaces", "Capacity", "Pond"):
        assert section in page, f"missing the {section} section"
    assert "Test" in page, "the workspace should be listed"


def test_the_console_says_a_lot_with_little(db, admin):
    """The page is read at a glance. Prose defeats the point of it.

    Measured in two parts, because a page listing fifty workspaces will always
    carry more words than one listing two. A flat limit would only ever have
    held for a small fixture, which is how the first version of this passed
    while the live page ran to 117 words.
    """
    import re

    for n in range(6):
        _install(db, team_id=f"T-TERSE{n}", alerts_used=n * 7)
    page = admin.get(f"/admin?key={ADMIN}").text

    dash = page[page.index('<div class="adm">') :]
    # Folded panels are opt-in, and the lists grow with the customer count.
    visible = re.sub(r"<details.*?</details>", " ", dash, flags=re.S)
    chrome = re.sub(r'<div class="(rows|att)">.*?(?=<div class="sec"|<div class="foot")',
                    " ", visible, flags=re.S)

    words = [w for w in re.sub(r"<[^>]+>", " ", chrome).split() if w != "&middot;"]
    assert len(words) < 55, f"{len(words)} words of page furniture is an essay"


def test_the_json_and_the_page_agree(db, admin):
    """Two readings of the same world must not tell different stories."""
    from app.admin import gather

    _install(db, team_id="T-JSON", alerts_used=7)
    body = admin.get(f"/admin/status?key={ADMIN}").json()
    assert body["delivered"] == gather()["delivered"]
    assert body["live"] == gather()["live"]


def test_the_json_needs_the_key_too(db, admin):
    assert admin.get("/admin/status?key=wrong").status_code == 401


def test_dead_claim_codes_are_gone_from_the_console(db, admin):
    """They were shown for a flow that no longer exists."""
    _install(db, team_id="T-OLD")
    page = admin.get(f"/admin?key={ADMIN}").text
    assert "FOXY-" not in page


def test_the_json_survives_real_timestamps(db, admin):
    """It returned 500 in production while every test passed.

    The fixtures had no dated rows, so nothing ever handed a datetime to the
    serialiser. Anything with a clock in it belongs in this test.
    """
    import datetime as dt

    from app.db import Alert, PondTask, session

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    _install(db, team_id="T-TIME", alerts_used=3, last_alert_at=now)
    with session() as s:
        s.add(Alert(fingerprint="t1", entity_key="e1", source="x", kind="early",
                    confidence=1.0, ts="1.0", created_at=now, payload={}))
        s.add(PondTask(task_id="tk1", run_id="r1", action_id="scan_now",
                       status="completed", created_at=now, updated_at=now))

    r = admin.get(f"/admin/status?key={ADMIN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_alert"] is not None
    assert body["last_pond"] is not None
    assert body["workspaces"][0]["last_alert"] is not None


def test_taking_a_plan_away_asks_first(db, admin):
    """A stray click cost a live workspace its Pro plan.

    It sat as a bare button beside a harmless one, and nothing announced the
    result: a downgraded workspace looks exactly like one never upgraded.
    """
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    _install(db, team_id="T-CONFIRM", plan="pro", plan_until=now + dt.timedelta(days=300))

    page = admin.get(f"/admin?key={ADMIN}").text
    assert "Remove Pro" in page
    assert 'data-ask="Remove Pro from' in page, "a destructive action must ask"
    # The browser's own confirm() names the hostname and reads like a phishing
    # prompt on the one screen that changes billing. Checked in the markup, not
    # the stylesheet, which explains in a comment why it is gone.
    import re

    markup = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", page, flags=re.S)
    assert "confirm(" not in markup


def test_a_stopped_workspace_is_not_offered_a_plan(db, admin):
    """It receives nothing either way, so the button is noise."""
    _install(db, team_id="T-STOPPED", active=False)
    page = admin.get(f"/admin?key={ADMIN}").text
    assert "stopped" in page
    assert "Give Pro" not in page


# --- superaccess --------------------------------------------------------------


def test_the_search_key_can_be_replaced_without_a_deployment(db, admin, monkeypatch):
    """The one credential certain to need swapping one day. Needing a developer
    for that is how an agent quietly stops finding things."""
    import app.admin as admin_mod
    from app import runtime
    from app.config import settings

    monkeypatch.setattr(admin_mod, "_key_works", lambda v: (True, "ok"))
    monkey = settings.serper_api_key
    try:
        settings.serper_api_key = "from-the-environment"
        assert runtime.serper_key() == "from-the-environment"
        assert runtime.serper_source() == "environment"

        r = admin.post(
            "/admin/key", data={"key": ADMIN, "serper": "typed-in-the-console"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert runtime.serper_key() == "typed-in-the-console", "stored beats configured"
        assert runtime.serper_source() == "console"
    finally:
        settings.serper_api_key = monkey
        runtime.set_serper_key("")


def test_a_blank_key_does_not_wipe_the_stored_one(db, admin):
    """An empty box is a slip, not an instruction."""
    from app import runtime

    runtime.set_serper_key("still-here")
    admin.post("/admin/key", data={"key": ADMIN, "serper": "  "}, follow_redirects=False)
    assert runtime.serper_key() == "still-here"
    runtime.set_serper_key("")


def test_the_key_is_never_shown_back(db, admin):
    from app import runtime

    runtime.set_serper_key("secret-key-abcd1234")
    page = admin.get(f"/admin?key={ADMIN}").text
    assert "secret-key-abcd1234" not in page
    assert "…1234" in page, "enough to tell two keys apart"
    runtime.set_serper_key("")


def test_granting_alerts_raises_the_cap_without_rewriting_history(db, admin):
    """Adjusting what a workspace has used would corrupt the only honest count
    there is. The grant sits beside it instead."""
    from app.config import settings

    install_id = _install(db, team_id="T-GRANT", alerts_used=settings.free_alert_quota)
    assert _read(db, install_id)["remaining"] == 0

    admin.post(
        "/admin/grant", data={"key": ADMIN, "install_id": install_id, "alerts": 50},
        follow_redirects=False,
    )

    got = _read(db, install_id)
    assert got["remaining"] == 50
    assert got["quota"] == settings.free_alert_quota + 50

    from app import installs
    with db.session() as s:
        assert installs.get(s, install_id).alerts_used == settings.free_alert_quota


def test_stopping_a_workspace_keeps_its_history(db, admin):
    """Deactivated, not deleted: a reinstall should pick up where it left off."""
    from app import installs

    install_id = _install(db, team_id="T-STOP2", alerts_used=9)
    admin.post(
        "/admin/stop", data={"key": ADMIN, "install_id": install_id},
        follow_redirects=False,
    )

    with db.session() as s:
        row = installs.get(s, install_id)
        assert row.active is False
        assert row.alerts_used == 9, "the record of what it received must survive"


def test_an_announcement_reaches_every_active_channel(db, admin, monkeypatch):
    said = []

    import app.admin as admin_mod

    monkeypatch.setattr(
        admin_mod, "_say",
        lambda tok, ch, txt, blocks=None: said.append((ch, txt, blocks)) is None,
    )

    _install(db, team_id="T-A1")
    _install(db, team_id="T-A2")
    admin.post(
        "/admin/announce", data={"key": ADMIN, "message": "Foxy got faster"},
        follow_redirects=False,
    )
    assert len(said) == 2
    assert all("Foxy got faster" in txt for _, txt, _ in said)
    # Dressed, so it does not read as another detection in a channel of alerts.
    blocks = said[0][2]
    assert blocks and blocks[0]["text"]["text"].endswith("*Announcement*")
    assert blocks[-1]["type"] == "context"


def test_an_announcement_can_carry_a_tone(db, admin, monkeypatch):
    said = []
    import app.admin as admin_mod

    monkeypatch.setattr(
        admin_mod, "_say",
        lambda tok, ch, txt, blocks=None: said.append(blocks) is None,
    )
    _install(db, team_id="T-TONE")
    admin.post(
        "/admin/announce",
        data={"key": ADMIN, "message": "Sources were slow today", "tone": "heads-up"},
        follow_redirects=False,
    )
    assert "Heads up" in said[0][0]["text"]["text"]


def test_an_empty_announcement_says_nothing(db, admin, monkeypatch):
    said = []
    import app.admin as admin_mod

    monkeypatch.setattr(
        admin_mod, "_say", lambda tok, ch, txt, blocks=None: said.append(ch) is None
    )

    _install(db, team_id="T-A3")
    admin.post("/admin/announce", data={"key": ADMIN, "message": "   "},
               follow_redirects=False)
    assert said == []


def test_every_action_needs_the_key(db, admin):
    """All of these change something real."""
    install_id = _install(db, team_id="T-GUARD")
    for path, data in [
        ("/admin/key", {"serper": "x"}),
        ("/admin/grant", {"install_id": install_id, "alerts": 50}),
        ("/admin/stop", {"install_id": install_id}),
        ("/admin/announce", {"message": "hello"}),
    ]:
        admin.post(path, data={"key": "wrong", **data}, follow_redirects=False)

    from app import installs
    with db.session() as s:
        row = installs.get(s, install_id)
        assert row.active is True and (row.bonus_alerts or 0) == 0


def test_the_console_stays_light_on_a_dark_system(db, admin):
    """It rendered as dark text on a dark ground and was unreadable.

    The shared stylesheet flips its palette under prefers-color-scheme, and the
    console set its own colours on an inner element while the page behind it
    stayed dark. The light values have to come after that block, because order
    is what settles a tie in the cascade.
    """
    page = admin.get(f"/admin?key={ADMIN}").text
    css = page[page.index("<style>") : page.index("</style>")]

    dark_at = css.find("prefers-color-scheme:dark")
    light_at = css.rfind(":root{")
    assert dark_at != -1, "the shared sheet still has a dark block to beat"
    assert light_at > dark_at, "the light palette must be declared after it"
    assert "body{background:" in css, "the page behind the cards needs a colour too"


def test_a_workspace_row_is_identifiable_before_it_is_read(db, admin):
    from app.admin import _initials

    assert _initials("YC alert") == "YA"
    assert _initials("Zen") == "ZE"
    assert _initials("all-foxy-land") == "AF"
    assert _initials("") == "?"

    _install(db, team_id="T-AV", alerts_used=1)
    assert 'class="av"' in admin.get(f"/admin?key={ADMIN}").text


def test_the_week_of_delivery_is_shown(db, admin):
    """A running total says nothing about whether it is still working."""
    import datetime as dt

    from app.admin import gather
    from app.db import Alert, session

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    with session() as s:
        for n in range(3):
            s.add(Alert(fingerprint=f"w{n}", entity_key=f"e{n}", source="x",
                        kind="confirmed", confidence=1.0, ts="1.0",
                        created_at=now - dt.timedelta(days=1), payload={}))

    week = gather()["week"]
    assert len(week) == 7
    assert sum(x["count"] for x in week) == 3
    assert "Last 7 days" in admin.get(f"/admin?key={ADMIN}").text


def test_the_headline_reads_like_english(db, admin):
    """"1 install(s) without a channel" is the first line anyone reads."""
    from app.admin import gather
    from app.config import settings

    _install(db, team_id="T-ONE", alerts_used=settings.free_alert_quota)
    one = gather()["problems"]
    assert any("1 workspace has run out" in p for p in one), one
    assert not any("(s)" in p for p in one), one

    _install(db, team_id="T-TWO", alerts_used=settings.free_alert_quota)
    two = gather()["problems"]
    assert any("2 workspaces have run out" in p for p in two), two


def test_the_activity_log_records_what_was_done(db, admin):
    """A workspace lost its plan to a stray click and nothing could say when."""
    install_id = _install(db, team_id="T-LOG", alerts_used=5)
    admin.post("/admin/grant", data={"key": ADMIN, "install_id": install_id, "alerts": 50},
               follow_redirects=False)

    page = admin.get(f"/admin/logs?key={ADMIN}").text
    assert "Activity" in page
    assert "grant" in page
    assert "50 alerts" in page


def test_the_activity_log_needs_the_key(db, admin):
    assert "Activity" not in admin.get("/admin/logs?key=wrong").text


def test_actions_answer_json_when_the_page_asks(db, admin):
    """So pressing a button costs a request, not a page load."""
    install_id = _install(db, team_id="T-AJAX", alerts_used=1)
    r = admin.post(
        "/admin/grant",
        data={"key": ADMIN, "install_id": install_id, "alerts": 50, "ajax": "1"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "50 alerts added" in r.json()["message"]


def test_actions_still_work_without_javascript(db, admin):
    """The console is a set of real forms first; the script is an improvement
    on top, not a requirement."""
    install_id = _install(db, team_id="T-NOJS", alerts_used=1)
    r = admin.post(
        "/admin/grant", data={"key": ADMIN, "install_id": install_id, "alerts": 50},
        follow_redirects=False,
    )
    assert r.status_code == 303, "a plain form post must redirect"


def test_the_site_and_the_manifest_quote_the_same_price():
    """Two places naming a price is two places for it to drift.

    The site is hand-written HTML and the manifest is generated, so nothing
    connects them but this.
    """
    import pathlib
    import re

    from app.config import settings
    from app.main import manifest

    pro = next(
        p for p in manifest()["metadata"]["pricing_plans"]
        if p["pricing_model"] == "subscription"
    )
    dollars = pro["amount_minor"] / 100
    assert dollars == settings.price_monthly_minor / 100

    page = pathlib.Path("app/static/index.html").read_text(encoding="utf-8")
    quoted = set(re.findall(r"\$(\d+)\s*<em>per month</em>", page))
    assert quoted == {f"{dollars:.0f}"}, (
        f"the site says {quoted} and the manifest says ${dollars:.0f}"
    )

    included = set(re.findall(r"([\d,]+) alerts a month", page))
    assert included == {f"{pro['included_units']:,}"}, included


def test_the_pricing_page_is_reachable_and_whole(db):
    """It is a route, a nav entry and a page. Missing any one of the three
    leaves a link that goes nowhere or a page nobody can find."""
    import pathlib
    import re

    page = pathlib.Path("app/static/index.html").read_text(encoding="utf-8")

    assert 'data-r="/pricing"' in page, "no nav entry"
    assert 'id="p-/pricing"' in page, "no page"
    assert '"/pricing":' in page, "the router does not know the route"

    # Every nav entry must have a page, and every page a nav entry.
    nav = set(re.findall(r'data-r="([^"]+)"', page))
    pages = set(re.findall(r'id="p-([^"]+)"', page))
    assert nav == pages, f"nav {nav} does not match pages {pages}"


def test_the_pricing_page_offers_a_way_out_of_the_two_fixed_plans():
    """Somebody will want several channels, or a different set of sources, and
    a page with no third door loses them silently."""
    import pathlib

    from app.config import settings

    page = pathlib.Path("app/static/index.html").read_text(encoding="utf-8")
    assert "Let&#39;s talk" in page or "Let's talk" in page
    assert settings.support_email in page, "the third tier needs a way to reach a person"


def test_one_plan_is_recommended():
    import pathlib

    page = pathlib.Path("app/static/index.html").read_text(encoding="utf-8")
    assert page.count("pr-tag") >= 1, "nothing is marked as the one to pick"
    assert page.count('class="pr-c up"') == 1, "exactly one plan may be recommended"


def test_every_site_route_is_served_by_the_server_too(db, client):
    """The site is one document that swaps sections, so each of its paths must
    return that document. Otherwise a typed URL, a shared link or a reload is a
    404 - which is most of the reason to have readable URLs at all."""
    from app.main import SITE_ROUTES

    for path in ("/",) + SITE_ROUTES:
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'id="p-/pricing"' in r.text, f"{path} did not return the site"


def test_the_site_links_are_real_paths(db):
    """A hash in the address bar reads as unfinished."""
    import pathlib
    import re

    page = pathlib.Path("app/static/index.html").read_text(encoding="utf-8")
    nav = re.findall(r'<a[^>]+data-r="[^"]*"[^>]*>', page)
    assert nav, "no navigation found"
    for link in nav:
        assert 'href="#' not in link, link


def test_the_hero_shows_detections_rather_than_describing_them():
    """The headline can claim "before YC does"; the detections argue it."""
    import pathlib

    page = pathlib.Path("app/static/index.html").read_text(encoding="utf-8")
    hero = page[page.index('class="hero split"') : page.index("<!-- the gap -->")]

    assert 'id="scope"' in hero, "the hero shows nothing"
    assert hero.count('class="sig') >= 3, "one card does not cycle"
    assert "EARLY SIGNAL" in hero
    assert "Not in the YC directory yet" in hero, "the claim needs its evidence"
    # Real companies Foxy actually caught, not invented ones.
    for name in ("EVO HQ", "Arcline", "Adalat AI"):
        assert name in hero, name


def test_the_hero_animation_yields_to_a_visitor_who_asked_for_less():
    """Motion is decoration. Somebody who has switched it off in their system
    should not have to watch it anyway."""
    import pathlib

    page = pathlib.Path("app/static/index.html").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in page
    assert "matchMedia" in page, "the script must check it too, not only the CSS"
    assert "document.hidden" in page, "and stop while the tab is in the background"


def test_a_key_is_tried_before_it_is_trusted(db, admin, monkeypatch):
    """A key stored without checking looks saved and then fails silently on
    the next sweep, hours later, where nobody is watching."""
    import app.admin as admin_mod
    from app import runtime

    runtime.set_serper_key("the-good-one")
    monkeypatch.setattr(admin_mod, "_key_works", lambda v: (False, "serper rejected the key"))

    r = admin.post(
        "/admin/key", data={"key": ADMIN, "serper": "a-dud", "ajax": "1"}
    )
    assert r.json()["ok"] is False
    assert "rejected" in r.json()["message"]
    assert runtime.serper_key() == "the-good-one", "a bad key must not replace a good one"
    runtime.set_serper_key("")


def test_a_verified_key_is_saved(db, admin, monkeypatch):
    import app.admin as admin_mod
    from app import runtime

    monkeypatch.setattr(admin_mod, "_key_works", lambda v: (True, "ok"))
    r = admin.post("/admin/key", data={"key": ADMIN, "serper": "a-good-one", "ajax": "1"})
    assert r.json()["ok"] is True
    assert runtime.serper_key() == "a-good-one"
    runtime.set_serper_key("")


def test_an_announcement_can_go_to_one_channel(db, admin, monkeypatch):
    """Broadcast-only meant telling five workspaces something that concerned
    one of them."""
    said = []
    import app.admin as admin_mod

    monkeypatch.setattr(
        admin_mod, "_say",
        lambda tok, ch, txt, blocks=None: said.append(ch) is None,
    )

    one = _install(db, team_id="T-ONE-CH")
    _install(db, team_id="T-OTHER")

    admin.post(
        "/admin/announce",
        data={"key": ADMIN, "message": "Just for you", "install_id": one},
        follow_redirects=False,
    )
    assert len(said) == 1, f"reached {len(said)} channels"


def test_an_announcement_can_carry_a_button(db, admin, monkeypatch):
    said = []
    import app.admin as admin_mod

    monkeypatch.setattr(
        admin_mod, "_say",
        lambda tok, ch, txt, blocks=None: said.append(blocks) is None,
    )
    _install(db, team_id="T-BTN")
    admin.post(
        "/admin/announce",
        data={"key": ADMIN, "message": "We shipped something",
              "link_label": "See it", "link_url": "https://tryfoxy.vercel.app"},
        follow_redirects=False,
    )
    actions = [b for b in said[0] if b["type"] == "actions"]
    assert actions, "the button never made it into the message"
    assert actions[0]["elements"][0]["url"] == "https://tryfoxy.vercel.app"


def test_a_button_without_a_link_is_left_out(db, admin, monkeypatch):
    """Half a button is a broken message, not a partial one."""
    from app.admin import _announcement

    blocks, _ = _announcement("hello", "news", link_label="Click", link_url="")
    assert not [b for b in blocks if b["type"] == "actions"]


def test_a_custom_heading_replaces_the_default(db, admin):
    from app.admin import _announcement

    blocks, fallback = _announcement("body", "news", title="Scheduled downtime")
    assert "Scheduled downtime" in blocks[0]["text"]["text"]
    assert "Announcement" not in blocks[0]["text"]["text"]
    assert fallback.startswith("Scheduled downtime")


def test_the_console_offers_each_channel_by_name(db, admin):
    from app import installs

    install_id = _install(db, team_id="T-NAMED")
    with db.session() as s:
        installs.get(s, install_id).channel_name = "yc-signals"

    page = admin.get(f"/admin?key={ADMIN}").text
    assert "Every active channel" in page
    assert "yc-signals" in page, "a channel you cannot name is hard to pick"


def test_the_preview_renders_what_slack_will(db, admin):
    """Slack's mrkdwn is not markdown, and guessing at it is how a message
    goes out with a stray asterisk in it."""
    import pathlib

    page = admin.get(f"/admin?key={ADMIN}").text
    assert 'id="a-prev"' in page, "no preview"
    assert "/assets/preview.js" in page

    js = pathlib.Path("app/static/preview.js").read_text(encoding="utf-8")
    assert "<b>$1</b>" in js, "single asterisks are bold in Slack"
    assert "<em>$1</em>" in js, "underscores are italic"
