"""That the lifecycle messages are actually sent.

`test_messages.py` proves the messages are well built. This proves they leave
the building - which is a separate question, and the one Foxy has got wrong
before: 687 alerts were once recorded as sent while none were delivered,
because the test checked the decision rather than the delivery.

So every test here asserts on what a fake Slack client actually received.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import tempfile

import pytest

os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-the-suite")

BASE = "https://tryfoxy.example"


class Recording:
    """Stands in for SlackClient and keeps every call."""

    sent: list[dict] = []
    usable = True

    def __init__(self, *a, **kw):
        self.target = kw.get("target", "")
        self.token = kw.get("token", "")

    def post(self, blocks, text, **kw):
        Recording.sent.append(
            {"to": self.target, "blocks": blocks, "text": text, "body": str(blocks)}
        )
        return {"ok": True, "channel": self.target, "ts": f"{len(Recording.sent)}.0"}

    def join_channel(self, *a, **kw):
        return True


@pytest.fixture()
def db(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "encryption_key", "test-key-for-the-suite")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    monkeypatch.setattr(
        settings,
        "database_url",
        f"sqlite:///{pathlib.Path(tempfile.mkdtemp()).as_posix()}/notices.db",
    )
    import app.db as database

    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_schema_ready", False)
    from app import installs  # noqa: F401  registers the table

    database.init_db()

    Recording.sent = []
    for target in ("app.hosted.SlackClient", "app.admin.SlackClient", "app.cli.SlackClient"):
        try:
            monkeypatch.setattr(target, Recording)
        except AttributeError:
            pass
    return database


def _install(**kw):
    from app import installs
    from app.db import session

    with session() as s:
        row = installs.upsert(
            s,
            team_id=kw.pop("team_id", "T1"),
            team_name=kw.pop("team_name", "Acme"),
            token=kw.pop("token", "xoxb-1"),
            installer_id=kw.pop("installer_id", ""),
        )
        row.channel_id = kw.pop("channel_id", "C1")
        for k, v in kw.items():
            setattr(row, k, v)
        return row.id


# ---------------------------------------------------------------------------
# the free allowance running out
# ---------------------------------------------------------------------------


def test_running_out_sends_the_upgrade_message_not_a_bare_line(db):
    from app import hosted, installs
    from app.db import session

    install_id = _install(alerts_used=50)
    with session() as s:
        row = installs.get(s, install_id)
        p = {
            "id": row.id,
            "team": row.team_name,
            "token": row.token,
            "channel": row.channel_id,
            "quota": row.quota,
            "claim_code": row.claim_code,
        }
        code = row.claim_code

    hosted._notify_quota(p)

    assert len(Recording.sent) == 1
    sent = Recording.sent[0]
    assert sent["to"] == "C1"
    assert "$5" in sent["body"], "the price has to be in the message"
    assert code in sent["body"], "and the code that switches Pro on"
    assert [b for b in sent["blocks"] if b["type"] == "actions"], "no way to act on it"


def test_running_out_is_said_once_and_then_never_again(db):
    """A monitor that asks for money every eight hours gets muted."""
    from app import hosted, installs
    from app.db import session

    install_id = _install(alerts_used=50)
    with session() as s:
        row = installs.get(s, install_id)
        p = {
            "id": row.id,
            "team": row.team_name,
            "token": row.token,
            "channel": row.channel_id,
            "quota": row.quota,
            "claim_code": row.claim_code,
        }

    hosted._notify_quota(p)
    with session() as s:
        assert installs.get(s, install_id).quota_notified is True


def test_a_workspace_slack_refuses_is_still_marked_as_told(db, monkeypatch):
    """Otherwise the next sweep tries again, and the one after that."""
    from app import hosted, installs
    from app.db import session

    class Refusing(Recording):
        def post(self, *a, **kw):
            raise RuntimeError("channel_not_found")

    monkeypatch.setattr("app.hosted.SlackClient", Refusing)

    install_id = _install(alerts_used=50)
    with session() as s:
        row = installs.get(s, install_id)
        p = {
            "id": row.id,
            "team": row.team_name,
            "token": row.token,
            "channel": row.channel_id,
            "quota": row.quota,
            "claim_code": row.claim_code,
        }

    hosted._notify_quota(p)  # must not raise
    with session() as s:
        assert installs.get(s, install_id).quota_notified is True


# ---------------------------------------------------------------------------
# plans and grants, from the console
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "admin_key", "k1")
    return TestClient(app)


def test_granting_alerts_tells_the_channel(db, admin):
    install_id = _install(alerts_used=50)

    r = admin.post(
        "/admin/grant",
        data={"key": "k1", "install_id": install_id, "alerts": 50, "ajax": "1"},
    )
    assert r.status_code == 200, r.text

    assert len(Recording.sent) == 1
    body = Recording.sent[0]["body"]
    assert "50" in body
    assert "more alerts" in body.lower()


def test_switching_pro_on_tells_the_channel(db, admin):
    """It used to say nothing at all: someone paid, and the only evidence was
    alerts resuming up to eight hours later."""
    install_id = _install(alerts_used=50)

    r = admin.post(
        "/admin/plan",
        data={"key": "k1", "install_id": install_id, "months": 1, "ajax": "1"},
    )
    assert r.status_code == 200, r.text

    assert len(Recording.sent) == 1
    body = Recording.sent[0]["body"]
    assert "Pro is on" in body
    assert "resumed" in Recording.sent[0]["text"]


def test_removing_pro_tells_the_channel_without_scolding(db, admin):
    install_id = _install(plan="pro")

    r = admin.post(
        "/admin/plan",
        data={"key": "k1", "install_id": install_id, "months": 0, "ajax": "1"},
    )
    assert r.status_code == 200, r.text

    assert len(Recording.sent) == 1
    body = Recording.sent[0]["body"].lower()
    assert "pro has ended" in body
    assert "stays where it is" in body, "nothing they already received is lost"


def test_a_workspace_with_no_channel_is_not_posted_to(db, admin):
    """There is nowhere to post. Trying would raise inside a request handler."""
    install_id = _install(channel_id="")

    r = admin.post(
        "/admin/plan",
        data={"key": "k1", "install_id": install_id, "months": 1, "ajax": "1"},
    )
    assert r.status_code == 200, r.text
    assert Recording.sent == []


# ---------------------------------------------------------------------------
# the install that never chose a channel
# ---------------------------------------------------------------------------


def test_an_unfinished_install_is_found(db):
    from app import installs
    from app.db import session

    _install(team_id="T-DONE", channel_id="C1", installer_id="U1")
    waiting_id = _install(team_id="T-WAIT", channel_id="", installer_id="U2")

    with session() as s:
        found = [r.id for r in installs.unfinished(s)]
    assert found == [waiting_id]


def test_an_install_we_cannot_reach_is_not_chased(db):
    """No installer id means no way to DM them - it was recorded before Foxy
    captured that. Chasing it would just log a failure every sweep."""
    from app import installs
    from app.db import session

    _install(team_id="T-OLD", channel_id="", installer_id="")
    with session() as s:
        assert installs.unfinished(s) == []


def test_the_installer_is_dmed_about_the_missing_channel(db):
    from app import hosted

    install_id = _install(team_id="T-WAIT", team_name="Foxy Land",
                          channel_id="", installer_id="U2")

    hosted._nudge_unfinished()

    assert len(Recording.sent) == 1
    sent = Recording.sent[0]
    assert sent["to"] == "U2", "it has to go to the person, there is no channel"
    assert "Foxy Land" in sent["body"]
    assert install_id in sent["body"], "the link must reach their own settings"


def test_the_nudge_is_sent_once(db):
    from app import hosted

    _install(team_id="T-WAIT", channel_id="", installer_id="U2")

    hosted._nudge_unfinished()
    hosted._nudge_unfinished()
    hosted._nudge_unfinished()

    assert len(Recording.sent) == 1, "a setup reminder every eight hours is a fault"


def test_a_nudge_slack_refuses_is_not_retried_forever(db, monkeypatch):
    from app import hosted, installs
    from app.db import session

    class Refusing(Recording):
        def post(self, *a, **kw):
            raise RuntimeError("cannot_dm_bot")

    monkeypatch.setattr("app.hosted.SlackClient", Refusing)
    install_id = _install(team_id="T-WAIT", channel_id="", installer_id="U2")

    hosted._nudge_unfinished()  # must not raise

    with session() as s:
        assert installs.get(s, install_id).channel_nudged is True


def test_choosing_a_channel_stops_it_being_chased(db):
    from app import installs
    from app.db import session

    install_id = _install(team_id="T-WAIT", channel_id="", installer_id="U2")
    with session() as s:
        installs.get(s, install_id).channel_id = "C9"

    with session() as s:
        assert installs.unfinished(s) == []


def test_reinstalling_does_not_lose_the_only_person_we_can_reach(db):
    """Slack does not always attribute a re-install. Overwriting the stored id
    with an empty one would leave the workspace unreachable."""
    from app import installs
    from app.db import session

    _install(team_id="T-RE", installer_id="U7")
    with session() as s:
        row = installs.upsert(s, team_id="T-RE", team_name="Acme", token="xoxb-2")
        assert row.installer_id == "U7"


# ---------------------------------------------------------------------------
# the deploy itself
# ---------------------------------------------------------------------------


def test_the_new_columns_are_added_to_a_table_that_predates_them(monkeypatch):
    """The production table was created before these two columns existed.

    `create_all` never alters an existing table, so without the reconciliation
    step every query against installs would fail the moment this deploys - not
    for the new feature, for everything. Simulated by building the table as it
    was and then letting init_db catch up.
    """
    from sqlalchemy import inspect, text

    from app.config import settings

    monkeypatch.setattr(
        settings,
        "database_url",
        f"sqlite:///{pathlib.Path(tempfile.mkdtemp()).as_posix()}/old.db",
    )
    import app.db as database

    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_schema_ready", False)
    from app import installs  # noqa: F401

    eng = database.engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE installs ("
                " id VARCHAR(64) PRIMARY KEY,"
                " team_id VARCHAR(64),"
                " team_name VARCHAR(255),"
                " token_enc TEXT,"
                " channel_id VARCHAR(64),"
                " active BOOLEAN"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO installs (id, team_id, team_name, token_enc,"
                " channel_id, active) VALUES ('i1','T1','Old','enc','C1',1)"
            )
        )

    database.init_db(force=True)

    columns = {c["name"] for c in inspect(eng).get_columns("installs")}
    assert "installer_id" in columns, "the DM nudge would never find anyone"
    assert "channel_nudged" in columns, "every install would be nudged every sweep"

    # And the row that was already there still reads, with usable defaults.
    from app.db import session

    with session() as s:
        row = installs.get(s, "i1")
        assert row is not None
        assert not row.installer_id
        assert row.channel_nudged in (False, 0, None)
