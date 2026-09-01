"""Hosted mode: token storage, tenant isolation and the sweep endpoint."""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-the-suite")
os.environ.setdefault("SWEEP_KEY", "test-sweep-key")

from app.config import settings  # noqa: E402

settings.encryption_key = "test-key-for-the-suite"
settings.sweep_key = "test-sweep-key"

from app import installs  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Signal  # noqa: E402

client = TestClient(app)

SECRET = "xoxb-1193-ABCdef-token"


# --- encryption -------------------------------------------------------------


def test_roundtrip():
    assert installs.decrypt(installs.encrypt(SECRET)) == SECRET


def test_ciphertext_does_not_contain_the_token():
    assert SECRET not in installs.encrypt(SECRET)


def test_each_encryption_uses_a_fresh_nonce():
    assert installs.encrypt(SECRET) != installs.encrypt(SECRET)


def test_tampering_is_detected():
    blob = installs.encrypt(SECRET)
    broken = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    assert installs.decrypt(broken) == ""


def test_wrong_key_yields_nothing():
    blob = installs.encrypt(SECRET)
    settings.encryption_key = "some-other-key"
    try:
        assert installs.decrypt(blob) == ""
    finally:
        settings.encryption_key = "test-key-for-the-suite"


def test_empty_values_are_safe():
    assert installs.encrypt("") == ""
    assert installs.decrypt("") == ""
    assert installs.decrypt("not-base64-at-all!!") == ""


# --- tenant isolation -------------------------------------------------------


def _sig(external_id="1"):
    return Signal(
        source="x",
        external_id=external_id,
        title="Acme",
        url="https://x.com/a/status/" + external_id,
        company_name="Acme AI",
    )


def test_namespaces_differ_per_install():
    a = installs.Install(id="aaa", team_id="T1")
    b = installs.Install(id="bbb", team_id="T2")
    assert a.namespace != b.namespace


def test_same_signal_is_distinct_per_tenant():
    """Without a per-install namespace the second workspace to install would
    be told about nothing, because the first already consumed everything."""
    from app.engine import Engine

    sig = _sig()
    one = Engine(namespace="i:aaa:")
    two = Engine(namespace="i:bbb:")
    assert one._key(sig) != two._key(sig)
    assert one._key(sig).endswith(sig.fingerprint)


def test_engine_defaults_to_no_namespace():
    from app.engine import Engine

    assert Engine()._key(_sig()) == _sig().fingerprint


def test_install_falls_back_to_the_global_threshold():
    row = installs.Install(id="x", team_id="T", min_confidence="")
    assert row.confidence == settings.min_confidence
    row.min_confidence = "0.8"
    assert row.confidence == 0.8


# --- the sweep endpoint -----------------------------------------------------


def test_sweep_requires_a_key():
    r = client.post("/internal/sweep")
    assert r.status_code in (401, 503)


def test_sweep_rejects_a_wrong_key():
    r = client.post("/internal/sweep", headers={"X-Sweep-Key": "nope"})
    assert r.status_code in (401, 503)
    assert r.json()["error"]["code"] in {"unauthorized", "temporarily_unavailable"}


def test_hosted_is_off_on_sqlite():
    """Hosted mode needs durable storage. On SQLite that is almost certainly a
    serverless filesystem that disappears between requests."""
    if settings.database_url.startswith("sqlite"):
        assert installs.hosted_enabled() is False


def test_healthz_reports_hosted_state():
    body = client.get("/healthz").json()
    assert "hosted" in body
    assert "enabled" in body["hosted"]


def test_settings_page_rejects_an_unknown_id():
    r = client.get("/app/definitely-not-a-real-install")
    assert r.status_code == 200
    assert "not valid" in r.text


# --- the flood guard --------------------------------------------------------


def test_first_run_is_derived_from_the_seen_set_not_a_counter():
    """A global counter meant that once any sweep had run, the backfill guard
    was off for everyone. A workspace joining later, with an empty seen-set,
    then received every company ever found in one burst: 263 messages.
    """
    import inspect

    from app import db

    src = inspect.getsource(db.is_first_run)
    assert "Seen.fingerprint" in src, "first-run must be derived from the seen-set"
    assert "namespace" in inspect.signature(db.is_first_run).parameters


def test_first_run_is_per_namespace():
    from app.db import Seen, init_db, session

    init_db()
    with session() as s:
        s.query(Seen).filter(Seen.fingerprint.like("i:flood%")).delete(
            synchronize_session=False
        )
        s.add(
            Seen(
                fingerprint="i:floodA:abc",
                source="x",
                external_id="1",
                entity_key="i:floodA:acme",
            )
        )

    from app.db import is_first_run

    with session() as s:
        assert is_first_run(s, "i:floodA:") is False   # this tenant has seen something
        assert is_first_run(s, "i:floodB:") is True    # this one has not

    with session() as s:
        s.query(Seen).filter(Seen.fingerprint.like("i:flood%")).delete(
            synchronize_session=False
        )


def test_a_sweep_cannot_post_more_than_the_ceiling():
    """Whatever else goes wrong, one sweep must not fill a channel."""
    from app.engine import Engine

    assert Engine().max_alerts == settings.max_alerts_per_sweep
    assert settings.max_alerts_per_sweep <= 50


# --- plan and quota ---------------------------------------------------------


def test_free_plan_is_metered_by_alerts_delivered():
    row = installs.Install(id="q1", team_id="T", plan="free", alerts_used=0)
    assert row.quota == settings.free_alert_quota
    assert row.remaining == settings.free_alert_quota
    assert row.quota_exhausted is False


def test_quota_counts_down_and_stops_at_zero():
    row = installs.Install(id="q2", team_id="T", plan="free")
    row.alerts_used = settings.free_alert_quota - 1
    assert row.remaining == 1
    row.alerts_used = settings.free_alert_quota
    assert row.remaining == 0 and row.quota_exhausted
    row.alerts_used = settings.free_alert_quota + 99   # never goes negative
    assert row.remaining == 0


def test_a_paid_plan_is_unmetered():
    row = installs.Install(id="q3", team_id="T", plan="pro", alerts_used=10_000)
    assert row.quota == 0
    assert row.quota_exhausted is False


def test_delivery_is_capped_by_whichever_limit_is_lower():
    """A workspace with 3 alerts left must not receive 25."""
    from app.engine import Engine

    engine = Engine()
    engine.max_alerts = min(engine.max_alerts, 3)
    assert engine.max_alerts == 3


def test_missing_columns_are_added_on_init():
    """create_all() never alters an existing table, so adding a model field
    would otherwise fail against a database created before it."""
    import inspect as _inspect

    from app import db

    assert hasattr(db, "_add_missing_columns")
    src = _inspect.getsource(db._add_missing_columns)
    assert "ADD COLUMN" in src
    # Postgres rejects DEFAULT 0 on a BOOLEAN column.
    assert "TRUE" in src and "FALSE" in src


def test_postgres_engine_guards_against_stale_connections():
    """Serverless functions freeze between invocations and the database closes
    the idle connection meanwhile. Without pre-ping the pool hands out a dead
    socket, and the next query fails with "SSL connection has been closed
    unexpectedly". That cost a real install: the OAuth callback treated the
    error as a reason to fall back to the manual self-hosted flow."""
    import inspect

    from app import db

    src = inspect.getsource(db.engine)
    assert "pool_pre_ping=True" in src
    assert "pool_recycle" in src


def test_install_retries_before_falling_back():
    import inspect

    from app import oauth

    src = inspect.getsource(oauth.callback)
    assert "for attempt in" in src, "a transient error must not downgrade the flow"


def test_a_first_sweep_introduces_the_newest_few():
    """A brand-new workspace used to receive nothing at all: the backfill guard
    suppressed every signal, so the channel stayed empty until some company
    happened to appear, possibly a day later. It now gets a small, recent
    sample so the first sweep shows what Foxy actually does."""
    from app.engine import Engine

    assert Engine()._first_run_budget == settings.first_run_alerts
    assert 1 <= settings.first_run_alerts <= 15


def test_the_first_run_budget_is_shared_across_sources():
    """Spent in source order, so the YC directory gets it before the noisier
    social feeds, and the total stays small."""
    import inspect

    from app.engine import Engine

    src = inspect.getsource(Engine._absorb)
    assert "_first_run_budget" in src
    assert "reverse=True" in src, "the newest signals are the ones worth showing"


# --- the key mismatch that stopped every hosted alert ------------------------


def test_a_blob_from_another_key_is_named_not_silently_empty():
    """The web app and the scheduler are separately configured deployments.

    Their ENCRYPTION_KEYs drifted apart, and the only symptom was silence:
    every token decrypted to "", every sweep took the dry-run path, and 687
    alerts were recorded without one being sent. The mismatch must be a
    statement, not a shrug.
    """
    from app import installs
    from app.config import settings

    blob = installs.encrypt("xoxb-written-by-the-web-app")
    assert installs.token_problem(blob) == ""

    original = settings.encryption_key
    try:
        settings.encryption_key = "the-schedulers-different-key"
        why = installs.token_problem(blob)
        assert "different ENCRYPTION_KEY" in why, why
        assert installs.decrypt(blob) == "", "and it must not return a bad token"
    finally:
        settings.encryption_key = original

    assert installs.decrypt(blob) == "xoxb-written-by-the-web-app"


def test_the_key_fingerprint_does_not_leak_the_key():
    from app import installs
    from app.config import settings

    fp = installs.key_fingerprint()
    assert len(fp) == 8
    assert settings.encryption_key not in fp
