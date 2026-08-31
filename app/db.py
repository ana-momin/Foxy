"""Persistent state.

This is what makes Foxy a monitor rather than a script. Four tables:

  seen        - every fingerprint we have ever processed (dedupe)
  entities    - one row per company, tracking early -> confirmed promotion
  alerts      - what we actually posted, and where, so we can thread replies
  runs        - per-source health history, surfaced in /healthz and Slack

SQLite by default (a single file, zero setup). Set DATABASE_URL to a Postgres
URL when the host has an ephemeral disk.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .config import settings


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Seen(Base):
    """Every signal fingerprint we have processed. The duplicate guard."""

    __tablename__ = "seen"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    entity_key: Mapped[str] = mapped_column(String(255), index=True)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class Entity(Base):
    """One company, tracked across sources and over time."""

    __tablename__ = "entities"

    entity_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    program: Mapped[str] = mapped_column(String(32), default="YC")
    batch: Mapped[str | None] = mapped_column(String(64), nullable=True)
    company_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # The heart of the early-detection story.
    first_signal_source: Mapped[str] = mapped_column(String(32))
    first_signal_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    was_early: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    confirm_notified: Mapped[bool] = mapped_column(Boolean, default=False)

    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    @property
    def lead_time_days(self) -> int | None:
        """How many days we beat YC's own announcement by."""
        if not (self.confirmed and self.confirmed_at and self.first_signal_at):
            return None
        return max(0, (self.confirmed_at - self.first_signal_at).days)


class Alert(Base):
    """A message we posted. `ts` lets us thread the confirmation reply."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    entity_key: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))  # early | confirmed | promotion
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ts: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class SourceRun(Base):
    """Per-source health. Two consecutive failures triggers a Slack warning."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    found: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ran_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class Meta(Base):
    """Small key/value store: sweep counter, first-run flag, cursors."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


_engine = None


def engine():
    global _engine
    if _engine is None:
        url = settings.database_url
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
    return _engine


_schema_ready = False


def init_db() -> None:
    global _schema_ready
    Base.metadata.create_all(engine())
    _schema_ready = True


def _ensure_schema() -> None:
    """Create the tables on first use.

    Reads can arrive before any sweep has run - /healthz on a fresh deploy, or
    Pond calling health_check straight after registering the agent. Without
    this the very first request against an empty database fails with
    "no such table". Cheap: create_all is a no-op once the tables exist, and
    the flag means we only try once per process.
    """
    if not _schema_ready:
        init_db()


@contextmanager
def session() -> Iterator[Session]:
    _ensure_schema()
    s = Session(engine(), future=True)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Convenience helpers used by the engine
# ---------------------------------------------------------------------------


def meta_get(s: Session, key: str, default: str = "") -> str:
    row = s.get(Meta, key)
    return row.value if row else default


def meta_set(s: Session, key: str, value: str) -> None:
    row = s.get(Meta, key)
    if row:
        row.value = value
    else:
        s.add(Meta(key=key, value=value))


def is_first_run(s: Session) -> bool:
    return meta_get(s, "sweeps_completed", "0") == "0"


def bump_sweep_counter(s: Session) -> int:
    n = int(meta_get(s, "sweeps_completed", "0") or 0) + 1
    meta_set(s, "sweeps_completed", str(n))
    return n


def already_seen(s: Session, fingerprint: str) -> bool:
    return s.get(Seen, fingerprint) is not None


def mark_seen(s: Session, *, fingerprint: str, source: str, external_id: str, entity_key: str) -> None:
    if s.get(Seen, fingerprint) is None:
        s.add(
            Seen(
                fingerprint=fingerprint,
                source=source,
                external_id=external_id,
                entity_key=entity_key,
            )
        )


def consecutive_failures(s: Session, source: str, limit: int = 2) -> int:
    """How many of the most recent runs for this source failed in a row."""
    rows = (
        s.execute(
            select(SourceRun)
            .where(SourceRun.source == source)
            .order_by(SourceRun.ran_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    count = 0
    for r in rows:
        if r.ok:
            break
        count += 1
    return count


def recent_alerts(s: Session, limit: int = 20) -> list[Alert]:
    return (
        s.execute(select(Alert).order_by(Alert.created_at.desc()).limit(limit))
        .scalars()
        .all()
    )


def health_snapshot(s: Session) -> dict[str, Any]:
    """Used by /healthz, the Pond manifest and `/foxy status`."""
    out: dict[str, Any] = {
        "sweeps_completed": int(meta_get(s, "sweeps_completed", "0") or 0),
        "last_sweep_at": meta_get(s, "last_sweep_at", "") or None,
        "sources": {},
    }
    seen_sources = s.execute(select(SourceRun.source).distinct()).scalars().all()
    for src in seen_sources:
        last = (
            s.execute(
                select(SourceRun)
                .where(SourceRun.source == src)
                .order_by(SourceRun.ran_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if last:
            out["sources"][src] = {
                "ok": last.ok,
                "found": last.found,
                "new": last.new,
                "error": last.error,
                "ran_at": last.ran_at.isoformat(),
            }
    return out


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)
