"""The clock hosted mode runs on.

Foxy is meant to keep working long after anyone last opened this repository,
and the thing standing in the way is a GitHub rule: a scheduled workflow is
switched off after 60 days without a commit. Nothing in the application would
notice - the web app stays up, the console still answers, the sources still
look healthy, and no sweep ever runs again.

So the sweep pushes an empty commit when the repository has gone quiet. These
tests hold that arrangement in place, because losing it fails silently and only
becomes visible two months later.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import re

import pytest

os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-the-suite")

# A declared dependency, so importing it outright rather than skipping on it:
# a test that can quietly turn itself off is no guard at all.
import yaml

SWEEP = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "hosted-sweep.yml"

# GitHub's limit is 60 days. Anything at or above this leaves too little room
# for a sweep to fail a few times in a row before the deadline.
LATEST_SAFE_INTERVAL = 55


def workflow() -> dict:
    return yaml.safe_load(SWEEP.read_text(encoding="utf-8"))


def keepalive() -> dict:
    steps = workflow()["jobs"]["sweep"]["steps"]
    found = [s for s in steps if "--allow-empty" in (s.get("run") or "")]
    assert found, "the sweep no longer keeps its own schedule alive"
    return found[0]


def test_the_sweep_is_still_scheduled():
    # `on` is truthy YAML: PyYAML reads the bare key as the boolean True.
    triggers = workflow().get("on") or workflow().get(True)
    assert triggers.get("schedule"), "hosted mode lost its clock"


def test_the_sweep_keeps_its_own_schedule_alive():
    step = keepalive()
    assert step.get("continue-on-error") is True, (
        "a failed keepalive must not fail a sweep that already did its work"
    )
    assert "schedule" in step.get("if", ""), (
        "the keepalive should answer to the cron, not to a manual run"
    )


def test_the_keepalive_fires_before_github_gives_up():
    # The age comparison, not any number in the step: the step also divides by
    # the seconds in a day, which is a fine number and a terrible threshold.
    threshold = re.search(r'-ge\s+"?(\d+)"?', keepalive()["run"])
    assert threshold, "the keepalive no longer compares the age of the repository"
    days = int(threshold.group(1))
    assert days <= LATEST_SAFE_INTERVAL, (
        f"the keepalive waits {days} days; GitHub stops the schedule at 60"
    )


def test_the_keepalive_can_actually_push():
    assert workflow()["jobs"]["sweep"].get("permissions", {}).get("contents") == "write", (
        "the job cannot push, so the keepalive would fail every time"
    )


def test_the_keepalive_does_not_start_a_test_run():
    assert "[skip ci]" in keepalive()["run"], (
        "an empty commit without [skip ci] mails the owner a CI run every time"
    )


# --- the countdown the console shows ----------------------------------------


def test_the_module_and_the_workflow_agree_on_the_renewal_day():
    """Two places name the same day, so they have to be checked against each
    other. The console would otherwise promise a renewal on a day the sweep has
    no intention of acting."""
    from app import schedule

    threshold = re.search(r'-ge\s+"?(\d+)"?', keepalive()["run"])
    assert int(threshold.group(1)) == schedule.RENEW_DAYS, (
        "the workflow renews on a different day than the console promises"
    )


def test_github_limit_is_not_quietly_edited():
    from app import schedule

    assert schedule.LIMIT_DAYS == 60, "60 days is GitHub's rule, not ours to change"
    assert schedule.RENEW_DAYS < schedule.LIMIT_DAYS


def test_the_sweep_reports_the_commit_it_ended_on():
    """The report has to come after the keepalive, or a renewal that just
    happened is shown as the old date and the console counts down wrongly."""
    steps = workflow()["jobs"]["sweep"]["steps"]
    runs = [s.get("run") or "" for s in steps]
    pushed = next(i for i, r in enumerate(runs) if "--allow-empty" in r)
    noted = next(i for i, r in enumerate(runs) if "note-commit" in r)
    assert noted > pushed, "the console would show the commit before the renewal"

    step = steps[noted]
    assert step.get("continue-on-error") is True
    assert "DATABASE_URL" in (step.get("env") or {}), "it cannot record anything"


@pytest.fixture()
def db(monkeypatch):
    import pathlib as _p
    import tempfile

    from app.config import settings

    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{_p.Path(tempfile.mkdtemp()).as_posix()}/s.db"
    )
    import app.db as database

    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_schema_ready", False)
    from app import installs  # noqa: F401  registers the table

    database.init_db()
    return database


def test_nothing_is_claimed_before_a_sweep_has_reported(db):
    from app import schedule

    st = schedule.status()
    assert st["known"] is False
    assert "waiting" in st["note"]


def test_a_fresh_commit_gives_the_full_window(db):
    from app import schedule

    schedule.record(dt.datetime.now(dt.timezone.utc), "abc1234567")
    st = schedule.status()
    assert st["days_left"] == schedule.LIMIT_DAYS
    assert st["renew_in"] == schedule.RENEW_DAYS
    assert st["overdue"] is False
    assert st["stale"] is False
    assert st["sha"] == "abc1234567"


def test_the_window_shrinks_as_the_repository_goes_quiet(db):
    from app import schedule

    schedule.record(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20))
    st = schedule.status()
    assert st["days_left"] == 40
    assert st["renew_in"] == 25
    assert st["overdue"] is False


def test_past_the_renewal_day_it_says_so(db):
    from app import schedule

    schedule.record(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=47))
    st = schedule.status()
    assert st["overdue"] is True, "the sweep should have pushed by now"
    assert st["days_left"] == 13


def test_it_never_counts_below_zero(db):
    from app import schedule

    schedule.record(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400))
    st = schedule.status()
    assert st["days_left"] == 0
    assert st["renew_in"] == 0
    assert st["share"] == 1.0


def test_a_reading_the_sweep_stopped_refreshing_is_marked_stale(db):
    """If the sweep dies, this stops being updated. Counting down from a number
    nothing is refreshing would be the most misleading thing on the page."""
    from app import schedule
    from app.db import meta_set, session

    schedule.record(dt.datetime.now(dt.timezone.utc))
    with session() as s:
        old = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=3)
        meta_set(s, f"{schedule.AT}:read", old.isoformat())

    assert schedule.status()["stale"] is True


# --- what the console does with it -------------------------------------------


ADMIN = "admin-key-for-the-suite"


@pytest.fixture()
def admin(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "admin_key", ADMIN)
    return TestClient(app)


def test_the_console_shows_the_countdown(db, admin):
    from app import schedule

    schedule.record(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10))
    page = admin.get(f"/admin?key={ADMIN}").text
    assert "50d left" in page
    assert "renews in <b>35d</b>" in page


def test_the_console_stays_quiet_before_a_sweep_has_reported(db, admin):
    """No reading means no countdown. Explaining the absence would cost words
    on a page whose whole point is being readable at a glance."""
    page = admin.get(f"/admin?key={ADMIN}").text
    assert 'class="sched' not in page
    assert "d left" not in page, "it must not invent a countdown it cannot know"


def test_the_countdown_is_six_words(db, admin):
    """The page has a word budget, enforced in test_plans. This panel has to
    live inside it: the detail belongs in the tooltip, not on the page."""
    import re

    from app import schedule

    schedule.record(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10), "abc1234")
    page = admin.get(f"/admin?key={ADMIN}").text
    card = re.search(r'<div class="sched.*?</div></div>', page, re.S).group(0)
    # The tooltip carries the dates and the commit; it is not read at a glance.
    card = re.sub(r'title="[^"]*"', " ", card)
    words = [w for w in re.sub(r"<[^>]+>", " ", card).split() if w != "&middot;"]
    assert len(words) <= 6, f"the schedule panel says {len(words)} words: {words}"


def test_the_status_endpoint_survives_the_dates(db, admin):
    """`renews_on` and `expires_on` are plain dates, and a date is not a
    datetime. The serialiser handled only the latter, which is how this endpoint
    once returned a 500 in production while every test passed."""
    from app import schedule

    schedule.record(dt.datetime.now(dt.timezone.utc))
    r = admin.get(f"/admin/status?key={ADMIN}")
    assert r.status_code == 200
    sched = r.json()["schedule"]
    assert sched["days_left"] == schedule.LIMIT_DAYS
    assert isinstance(sched["renews_on"], str)
    assert isinstance(sched["expires_on"], str)


def test_a_date_is_not_flattened_into_a_day(db):
    """datetime is a subclass of date, so checking date first would truncate
    every timestamp on the page to midnight."""
    from app.admin import _jsonable

    when = dt.datetime(2026, 9, 13, 14, 30, 5)
    assert _jsonable(when) == "2026-09-13T14:30:05"
    assert _jsonable(when.date()) == "2026-09-13"


def test_a_diagnostic_run_does_not_report_itself_as_broken():
    """`doctor` exits 1 when it finds a problem, which is correct for a command
    someone reads and wrong for a job that mails on failure. A workspace that
    never chose a channel is a known fact, not a broken run."""
    steps = workflow()["jobs"]["sweep"]["steps"]
    run = next(s["run"] for s in steps if "hosted-doctor" in (s.get("run") or ""))
    for line in run.splitlines():
        if "hosted-doctor" in line:
            assert "|| true" in line, f"a doctor run would mail a failure: {line.strip()}"


def test_a_real_sweep_failure_still_fails_the_run():
    """The opposite mistake: swallowing the exit code of the thing that matters
    would make every run green whether or not Foxy swept anything."""
    steps = workflow()["jobs"]["sweep"]["steps"]
    run = next(s["run"] for s in steps if "hosted-sweep" in (s.get("run") or ""))
    line = next(ln for ln in run.splitlines() if "app.cli hosted-sweep" in ln)
    assert "|| true" not in line, "a failed sweep must still fail the run"


# --- the alarms that matter over years ---------------------------------------
#
# Everything below is a way Foxy can stop working while the console still looks
# healthy. Each one has to reach the top of the page, because a problem nobody
# is told about is the same as no monitoring at all.


def _swept(db, hours_ago: float, completed: int = 12) -> None:
    from app.db import meta_set, session

    when = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=hours_ago)
    with session() as s:
        meta_set(s, "sweeps_completed", str(completed))
        meta_set(s, "last_sweep_at", when.isoformat())


def test_a_sweep_that_stopped_reaches_the_top_of_the_page(db):
    """The worst failure Foxy has, and the quietest: every other number on the
    page keeps its last good value while nothing is being found any more."""
    from app.admin import gather

    _swept(db, hours_ago=40)
    d = gather()
    assert d["ok"] is False
    assert "no sweep for" in d["problems"][0], d["problems"]


def test_a_sweep_running_normally_raises_nothing(db):
    from app.admin import gather

    _swept(db, hours_ago=3)
    assert gather()["ok"] is True


def test_a_database_that_has_never_swept_is_not_accused(db):
    """A fresh install has no sweeps. That is new, not broken."""
    from app.admin import gather

    assert gather()["ok"] is True


def test_the_sweep_outranks_everything_else(db):
    """Only the first problem is shown. If sweeping has stopped, nothing else
    on the page is worth saying first."""
    from app.admin import gather
    from app.db import session
    from app import installs

    _swept(db, hours_ago=40)
    with session() as s:
        row = installs.upsert(s, team_id="T-BROKE", team_name="Broke", token="xoxb-1")
        row.channel_id = "C1"
        row.last_error = "invalid_auth"

    problems = gather()["problems"]
    assert "no sweep for" in problems[0]
    assert len(problems) > 1, "the other problems should still be listed"


def test_a_workspace_slack_has_stopped_accepting_is_reported(db):
    """Alerts for it are still being decided and then thrown away, which is the
    exact shape of the first bug Foxy ever had."""
    from app.admin import gather
    from app.db import session
    from app import installs

    _swept(db, hours_ago=2)
    with session() as s:
        row = installs.upsert(s, team_id="T-DEAD", team_name="Dead", token="xoxb-1")
        row.channel_id = "C1"
        row.last_error = "invalid_auth"

    d = gather()
    assert d["ok"] is False
    assert "failing to deliver" in " ".join(d["problems"])


def test_an_overdue_schedule_is_reported(db):
    """Reaching this means the automatic renewal is not happening and there is
    a real deadline running."""
    from app import schedule
    from app.admin import gather

    _swept(db, hours_ago=2)
    schedule.record(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=50))

    d = gather()
    assert d["ok"] is False
    assert "schedule expires in 10d" in " ".join(d["problems"])
