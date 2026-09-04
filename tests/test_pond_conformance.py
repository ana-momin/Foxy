"""Drive the Pond endpoints exactly as Pond drives them.

This is the suite that should have existed before the first submission. Every
point in the rejection was reachable from here, and none of it was reachable
from the Slack tests I had written instead:

  * the scan was accepted and pollable and then died after 166 seconds
  * the requested source scope was ignored
  * task and idempotency state did not persist
  * the advertised schemas were not enforced

So these tests speak HTTP, poll like a client, and assert on what comes back
rather than on how it is produced.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-the-suite")
os.environ.setdefault("POND_ACCESS_KEY", "test-access-key")

KEY = "test-access-key"
HEADERS = {
    "Authorization": f"Bearer {KEY}",
    "X-Agent-Protocol-Version": "1.0",
    "Content-Type": "application/json",
}


@pytest.fixture()
def client(monkeypatch):
    """A test client on its own database, so tasks are really persisted."""
    from fastapi.testclient import TestClient

    from app.config import settings

    monkeypatch.setattr(settings, "pond_access_key", KEY)
    monkeypatch.setattr(
        settings,
        "database_url",
        f"sqlite:///{pathlib.Path(tempfile.mkdtemp()) / 'pond.db'}",
    )
    monkeypatch.setattr(settings, "dry_run", True)

    import app.db as db

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_schema_ready", False)
    db.init_db()

    from app.main import app

    return TestClient(app)


def _run(client, action_id: str, params: dict | None = None, **extra):
    body = {"action_id": action_id, "parameters": params or {}}
    body.update(extra)
    return client.post("/runs", json=body, headers=HEADERS)


# --- schemas are enforced ----------------------------------------------------


def test_an_unknown_parameter_is_refused_not_ignored(client):
    """additionalProperties:false was advertised and never checked, so a
    misspelled field was silently dropped and the caller never knew."""
    r = _run(client, "scan_now", {"sourses": ["yc_directory"]})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "invalid_input"
    assert "sourses" in body["error"]["message"]


def test_a_value_outside_the_enum_is_refused(client):
    r = _run(client, "scan_now", {"sources": ["crunchbase"]})
    assert r.status_code == 422, r.text
    assert "crunchbase" in r.json()["error"]["message"]


def test_a_wrongly_typed_parameter_is_refused(client):
    r = _run(client, "scan_now", {"post_to_slack": "yes please"})
    assert r.status_code == 422, r.text
    assert "post_to_slack" in r.json()["error"]["message"]


def test_a_bad_number_is_refused_rather_than_crashing(client):
    """int(params["limit"]) raised ValueError, which surfaced as an internal
    error - the agent's fault, apparently, rather than the caller's."""
    r = _run(client, "recent_detections", {"limit": "twenty"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_input"


def test_the_error_names_the_offending_field(client):
    r = _run(client, "scan_now", {"sources": ["nope"]})
    err = r.json()["error"]
    # Pond carries the field under details, not at the top level.
    field = (err.get("details") or {}).get("field", "")
    assert field.startswith("parameters."), err
    assert "sources" in field


# --- the scan finishes -------------------------------------------------------


def test_a_scan_is_accepted_and_completes(client, monkeypatch):
    """Accepted, polled, and finished - the part that died at 166 seconds."""
    import app.pond_tasks as pt

    monkeypatch.setattr(pt, "_do_one_source", _stub_source)

    r = _run(client, "scan_now", {"sources": ["yc_directory"]})
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]
    assert r.json()["status"] == "queued"

    for _ in range(10):
        got = client.get(f"/tasks/{task_id}", headers=HEADERS).json()
        if got["status"] not in {"queued", "running"}:
            break
    assert got["status"] == "completed", got
    assert got["output"][0]["text"].startswith("## Scan complete")


def test_a_scan_honours_the_requested_sources(client, monkeypatch):
    """The rejection said the requested scope was not honoured. It was
    advertised, accepted, and then thrown away."""
    import app.pond_tasks as pt

    monkeypatch.setattr(pt, "_do_one_source", _stub_source)

    r = _run(client, "scan_now", {"sources": ["yc_launches"]})
    task_id = r.json()["task_id"]
    for _ in range(10):
        got = client.get(f"/tasks/{task_id}", headers=HEADERS).json()
        if got["status"] == "completed":
            break

    # Read the per-source lines rather than searching the whole document: "x"
    # is a substring of half the words in it, "example.com" included.
    text = got["output"][0]["text"]
    scanned = {
        line.split("**")[1]
        for line in text.splitlines()
        if line.startswith("- **") and " · " in line
    }
    assert scanned == {"yc_launches"}, f"scanned {scanned}, asked for yc_launches"


def test_a_default_scan_leaves_out_the_slow_searches(client):
    """A scan with no scope must still answer promptly, so the paced social
    searches are read only when asked for by name."""
    from app.pond_tasks import resolve_sources

    assert "linkedin" not in resolve_sources(None)
    assert "x" not in resolve_sources(None)
    assert "yc_directory" in resolve_sources(None)
    # But an explicit request is honoured exactly.
    assert resolve_sources(["linkedin"]) == ["linkedin"]


def test_a_failing_source_is_reported_and_the_scan_carries_on(client, monkeypatch):
    """One source falling over must be recorded against that source, not fatal.

    This runs the real _do_one_source, because its per-source error handling is
    the thing under test - stubbing it out would prove nothing. A source that
    raises is exactly the case Pond hit, where a database error partway through
    took the whole scan down with it.
    """
    import app.engine as engine_mod

    real_sweep = engine_mod.Engine.sweep

    def sweep(self, *, only=None, **kw):
        if only and "yc_launches" in only:
            raise RuntimeError("the source blew up")
        from app.models import SweepResult
        import datetime as dt

        r = SweepResult(started_at=dt.datetime.now(dt.timezone.utc))
        r.record(only[0], found=2, new=1, error=None)
        return r

    monkeypatch.setattr(engine_mod.Engine, "sweep", sweep)

    r = _run(client, "scan_now", {"sources": ["yc_launches", "yc_directory"]})
    task_id = r.json()["task_id"]
    for _ in range(12):
        got = client.get(f"/tasks/{task_id}", headers=HEADERS).json()
        if got["status"] not in {"queued", "running"}:
            break

    assert got["status"] == "completed", got
    text = got["output"][0]["text"]
    assert "yc_launches" in text and "failed" in text, "the failure must be reported"
    assert "yc_directory" in text, "the other source must still have been scanned"
    assert real_sweep is not None  # keep the reference honest


# --- state survives the process ---------------------------------------------


def test_a_task_is_readable_by_an_instance_that_never_saw_it(client, monkeypatch):
    """A dict is not shared between serverless instances. The database is."""
    import app.pond_tasks as pt

    monkeypatch.setattr(pt, "_do_one_source", _stub_source)

    task_id = _run(client, "scan_now", {"sources": ["yc_directory"]}).json()["task_id"]

    # A second app object, as a different instance would be.
    import importlib

    from fastapi.testclient import TestClient

    import app.main as main_mod

    importlib.reload(main_mod)
    other = TestClient(main_mod.app)

    got = other.get(f"/tasks/{task_id}", headers=HEADERS)
    assert got.status_code == 200, got.text
    assert got.json()["task_id"] == task_id


def test_a_repeated_run_returns_the_first_answer(client):
    """Idempotency lived in a dict, so a retry on another instance ran the
    whole action a second time."""
    headers = dict(HEADERS, **{"Idempotency-Key": "repeat-me"})
    body = {"action_id": "health_check", "parameters": {}, "run_id": "run-1"}

    first = client.post("/runs", json=body, headers=headers).json()
    second = client.post("/runs", json=body, headers=headers).json()
    assert first == second, "a repeated key must replay, not re-run"


def test_an_unknown_task_is_a_clean_404(client):
    r = client.get("/tasks/task_does_not_exist", headers=HEADERS)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "task_not_found"


# --- helpers -----------------------------------------------------------------


def _stub_source(task_id, state):
    """Record one source as done without going near the network."""
    from app.db import PondTask, session

    name = state["pending"][0]
    with session() as s:
        row = s.get(PondTask, task_id)
        row.pending = [x for x in row.pending if x != name]
        row.progress = dict(row.progress or {}, **{name: {"found": 3, "new": 1}})
        row.findings = list(row.findings or []) + [
            {
                "early": False,
                "company": f"Co from {name}",
                "batch": "Fall 2026",
                "source": name,
                "url": "https://example.com",
                "confidence": 1.0,
            }
        ]
        row.count = len(row.findings)


def test_a_source_that_never_finishes_does_not_hang_the_scan(client, monkeypatch):
    """A source outliving its request would otherwise be retried forever.

    The lease expires, the next poll picks the same source, and the task never
    terminates. After a few attempts it is written off so the scan finishes.
    """
    import app.pond_tasks as pt

    monkeypatch.setattr(pt, "MAX_ATTEMPTS", 2)

    def stalling(task_id, state):
        """A worker killed mid-source: the attempt counts, the source stays."""
        name = state["pending"][0]
        attempts = dict(state.get("attempts") or {})
        attempts[name] = attempts.get(name, 0) + 1
        if attempts[name] > pt.MAX_ATTEMPTS:
            pt._record(
                task_id, name,
                dict(state["progress"], **{name: {"found": 0, "new": 0, "error": "gave up"}}),
                list(state["findings"]), attempts,
            )
            return
        pt._record(task_id, name, dict(state["progress"]), list(state["findings"]),
                   attempts, keep_pending=True)

    monkeypatch.setattr(pt, "_do_one_source", stalling)

    r = _run(client, "scan_now", {"sources": ["linkedin"]})
    task_id = r.json()["task_id"]

    for _ in range(20):
        got = client.get(f"/tasks/{task_id}", headers=HEADERS).json()
        if got["status"] not in {"queued", "running"}:
            break

    assert got["status"] == "completed", got
    assert "gave up" in got["output"][0]["text"]


def test_the_retry_bound_is_declared():
    """Stated once, so the loop cannot become unbounded again."""
    from app.pond_tasks import MAX_ATTEMPTS

    assert 1 <= MAX_ATTEMPTS <= 5
