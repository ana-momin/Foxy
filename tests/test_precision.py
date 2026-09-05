"""What may be called an early founder signal, and what may not.

Pond's reviewer found that at least three of five early-signal results did not
match founder-announcement semantics. They were right, and the cases are named
here so they cannot come back:

  * **UzCombinator** - a LinkedIn company page whose name merely contains
    "Combinator"
  * **Grubwithus** - a real YC company, from batch **W2011**; the directory not
    listing it says something about the directory, not about YC being slow
  * **DRS**, **Infragrid** - company pages referencing YC, announcing nothing

The root cause was one rule: `is_early = match.is_early`, which asked only
whether YC listed the company. "Early" is a claim about a *founder announcing
before YC published*, and it needs both halves.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.engine import _batch_is_current
from app.models import Signal


def _signal(**kw) -> Signal:
    base = dict(
        source="linkedin",
        external_id="x1",
        title="Something",
        url="https://www.linkedin.com/company/example",
        description="",
        company_name="Example",
    )
    base.update(kw)
    return Signal(**base)


# --- batch recency -----------------------------------------------------------


@pytest.mark.parametrize(
    "batch,current",
    [
        ("Winter 2011", False),   # Grubwithus
        ("Summer 2019", False),
        ("YC W11", False),
        ("Fall 2026", True),
        ("YC F26", True),
        ("YC X26", True),
        ("", True),               # unknown stays eligible
        ("who knows", True),
    ],
)
def test_only_a_recent_batch_can_be_early(batch, current):
    assert _batch_is_current(batch) is current


# --- the announcement requirement -------------------------------------------


def test_a_company_page_is_never_an_early_founder_signal(monkeypatch):
    """A page describing a company announces nothing."""
    import app.crossref as crossref
    from app.engine import Engine

    class NotListed:
        found = False
        is_early = True
        unknown = False
        reason = "not in the YC directory"
        company = None

    monkeypatch.setattr(crossref, "lookup_for_program", lambda *a, **k: NotListed())

    sig = _signal(company_name="UzCombinator", is_announcement=False)
    engine = Engine(slack=_Silent(), namespace="i:prec:")
    from app.models import SweepResult

    result = SweepResult(started_at=dt.datetime.now(dt.timezone.utc))
    engine._evaluate(_NoSession(), sig, result)

    assert sig.is_early is False, "a company page is not a founder announcement"
    assert any("not a founder announcement" in n for n in sig.notes)


def test_a_founder_announcement_absent_from_yc_is_early(monkeypatch):
    """The real case must still work, or the fix has cost us the feature."""
    import app.crossref as crossref
    from app.engine import Engine
    from app.models import SweepResult

    class NotListed:
        found = False
        is_early = True
        unknown = False
        reason = "not in the YC directory"
        company = None

    monkeypatch.setattr(crossref, "lookup_for_program", lambda *a, **k: NotListed())

    sig = _signal(
        source="x",
        company_name="EVO HQ",
        batch="Fall 2026",
        description="We got into Y Combinator!",
        is_announcement=True,
    )
    engine = Engine(slack=_Silent(), namespace="i:prec:")
    result = SweepResult(started_at=dt.datetime.now(dt.timezone.utc))
    engine._evaluate(_NoSession(), sig, result)

    assert sig.is_early is True


def test_an_old_batch_announcement_is_not_early(monkeypatch):
    """Grubwithus, YC W2011. A founder announcement, but not news."""
    import app.crossref as crossref
    from app.engine import Engine
    from app.models import SweepResult

    class NotListed:
        found = False
        is_early = True
        unknown = False
        reason = "not in the YC directory"
        company = None

    monkeypatch.setattr(crossref, "lookup_for_program", lambda *a, **k: NotListed())

    sig = _signal(
        company_name="Grubwithus",
        batch="Winter 2011",
        is_announcement=True,
    )
    engine = Engine(slack=_Silent(), namespace="i:prec:")
    result = SweepResult(started_at=dt.datetime.now(dt.timezone.utc))
    engine._evaluate(_NoSession(), sig, result)

    assert sig.is_early is False
    assert any("not a current cohort" in n for n in sig.notes)


# --- the sources say which is which -----------------------------------------


def test_the_sources_declare_whether_a_signal_announces_anything():
    """The distinction has to be made where the evidence is, not guessed at
    later from a URL."""
    import inspect

    from app.sources import linkedin_social, x_social

    li = inspect.getsource(linkedin_social)
    assert "is_announcement=True" in li, "a post that passed the test is one"
    assert "is_announcement=False" in li, "a company page is not"
    assert "is_announcement=True" in inspect.getsource(x_social)


def test_early_requires_both_halves():
    """The rule itself, stated once so it cannot quietly revert."""
    import inspect

    from app.engine import Engine

    src = inspect.getsource(Engine._evaluate)
    assert "match.is_early and sig.is_announcement" in src


class _Silent:
    usable = False

    def post(self, *a, **k):
        raise AssertionError("must not post")


class _NoSession:
    """_evaluate only passes this to _upsert_entity, which we neutralise."""

    def get(self, *a, **k):
        return None

    def add(self, *a, **k):
        return None


def test_the_same_company_is_listed_once(monkeypatch):
    """Arcline came back twice.

    The same announcement reached us through two searches whose URLs differed
    by a tracking suffix, and the dedupe key paired the name with the URL - so
    two rows, one company. These actions answer which companies were detected,
    and a company is one answer however many posts carried it.
    """
    from app.main import _company_key, _distinct

    rows = [
        {"company": "Arcline", "url": "https://linkedin.com/posts/a_activity-1"},
        {"company": "Arcline", "url": "https://linkedin.com/posts/a_activity-1-XyZ"},
        {"company": "arcline ", "url": "https://x.com/arcline/status/2"},
        {"company": "EVO HQ", "url": "https://linkedin.com/posts/b"},
    ]
    out = _distinct(rows, _company_key)
    assert [r["company"] for r in out] == ["Arcline", "EVO HQ"]


def test_the_company_key_ignores_case_and_spacing():
    from app.main import _company_key

    assert _company_key({"company": "  EVO   HQ "}) == _company_key({"company": "evo hq"})
    assert _company_key({"title": "Fallback Co"}) == "fallback co"
    assert _company_key({}) == ""
