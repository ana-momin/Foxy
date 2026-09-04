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
import logging
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


log = logging.getLogger("foxy.db")


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


class PondTask(Base):
    """One asynchronous Pond run, and everything needed to resume it.

    This lived in a module-level dict. On a serverless host that is barely
    storage at all: each request may land on a different instance, so a poll
    could reach a worker that had never heard of the task, and an instance that
    freezes after responding takes its unfinished work with it. Pond saw the
    consequence as a scan that was accepted, polled, and then died with a
    database error after 166 seconds.

    Keeping the whole state here means any instance can answer any poll, and
    any instance can pick the work up where the last one left off.
    """

    __tablename__ = "pond_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    action_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")

    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Sources still to do, and what the finished ones produced.
    pending: Mapped[list] = mapped_column(JSON, default=list)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    findings: Mapped[list] = mapped_column(JSON, default=list)

    count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Whoever holds the lease is doing a slice of work right now. Without it two
    # polls arriving together would both start on the same source.
    leased_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class PondRun(Base):
    """A completed synchronous run, kept so a repeat returns the same answer.

    Idempotency was a dict too, with the same problem: a retry that landed on
    another instance ran the whole action again.
    """

    __tablename__ = "pond_runs"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


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
        else:
            # Serverless functions freeze between invocations, and the database
            # closes the idle connection while they are asleep. The pool then
            # hands out a dead socket and the next query fails with "SSL
            # connection has been closed unexpectedly" - which cost a real
            # install, because the OAuth callback treated it as a reason to fall
            # back to the manual flow.
            #
            # pre_ping checks a connection before handing it over and
            # transparently replaces a dead one; recycle retires connections
            # well before any idle timeout can reach them.
            kwargs.update(
                pool_pre_ping=True,
                pool_recycle=280,
                pool_size=3,
                max_overflow=5,
                connect_args={"connect_timeout": 15},
            )
        _engine = create_engine(url, **kwargs)
    return _engine


_schema_ready = False


def _add_missing_columns() -> None:
    """Add columns that exist on the models but not yet in the database.

    create_all() creates missing tables but never alters existing ones, so
    adding a field to a model would otherwise fail at runtime against a
    database created before it. This handles the only shape of change this
    project makes: new, nullable-or-defaulted columns. Anything more involved
    wants a real migration tool.
    """
    from sqlalchemy import inspect, text

    eng = engine()
    inspector = inspect(eng)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            ddl = f"{column.name} {column.type.compile(eng.dialect)}"
            default = column.default.arg if column.default is not None else None
            if default is not None and not callable(default):
                # Booleans first: Postgres rejects DEFAULT 0 on a BOOLEAN
                # column, and bool is a subclass of int so the order matters.
                if isinstance(default, bool):
                    literal = "TRUE" if default else "FALSE"
                elif isinstance(default, str):
                    literal = "'" + default.replace("'", "''") + "'"
                elif isinstance(default, (int, float)):
                    literal = str(default)
                else:
                    literal = None
                if literal is not None:
                    ddl += f" DEFAULT {literal}"
            try:
                with eng.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
                log.info("added column %s.%s", table.name, column.name)
            except Exception:  # noqa: BLE001 - one column must not block the rest
                log.exception("could not add %s.%s", table.name, column.name)


def init_db() -> None:
    global _schema_ready
    Base.metadata.create_all(engine())
    try:
        _add_missing_columns()
    except Exception:  # noqa: BLE001 - never let a migration attempt block boot
        log.exception("could not reconcile the schema")
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


def is_first_run(s: Session, namespace: str = "", source: str = "") -> bool:
    """Has this tenant ever seen anything from this source?

    Asked per source, because a workspace meets its sources at different
    times: the welcome sweep reads only the two YC feeds, so without this
    Speedrun, X and LinkedIn would each be treated as long-established the
    first time they are read, and arrive as a wall of alerts plus a digest of
    everything else rather than as an introduction.

    Derived from the seen-set rather than a counter. A counter was global, so
    once any sweep had run the guard was off for everyone: a workspace joining
    later, with an empty seen-set, got every company ever found in one burst.
    A counter also cannot recover from a sweep that died part-way, which is
    exactly how that happened.
    """
    q = select(Seen.fingerprint).where(Seen.fingerprint.like(f"{namespace}%"))
    if source:
        q = q.where(Seen.source == source)
    return s.execute(q.limit(1)).first() is None


def bump_sweep_counter(s: Session, namespace: str = "") -> int:
    key = f"sweeps_completed:{namespace}" if namespace else "sweeps_completed"
    n = int(meta_get(s, key, "0") or 0) + 1
    meta_set(s, key, str(n))
    return n


def already_seen(s: Session, fingerprint: str) -> bool:
    return s.get(Seen, fingerprint) is not None


def entities_for(s: Session, keys: list[str]) -> dict[str, "Entity"]:
    """Load many entities at once, keyed by entity_key.

    Same reasoning as `seen_fingerprints`: one query rather than one per
    signal.
    """
    if not keys:
        return {}
    rows = s.execute(select(Entity).where(Entity.entity_key.in_(keys))).scalars().all()
    return {r.entity_key: r for r in rows}


def seen_fingerprints(s: Session, namespace: str, source: str) -> set[str]:
    """Every fingerprint this workspace has already seen from one source.

    One query instead of one per signal. A sweep looks at several hundred, and
    against a hosted Postgres each round trip costs tens of milliseconds, which
    is the difference between a welcome that lands inside a web request and one
    that is killed by the serverless timeout.
    """
    rows = s.execute(
        select(Seen.fingerprint).where(
            Seen.source == source, Seen.fingerprint.like(f"{namespace}%")
        )
    ).scalars()
    return set(rows)


def mark_seen(
    s: Session,
    *,
    fingerprint: str,
    source: str,
    external_id: str,
    entity_key: str,
    known_new: bool = False,
) -> None:
    """Remember one item.

    `known_new` skips the existence check for a caller that has already
    consulted the batched seen-set. That check is a database round trip, and a
    sweep makes several hundred of them.
    """
    if known_new or s.get(Seen, fingerprint) is None:
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
