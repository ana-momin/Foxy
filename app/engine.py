"""The sweep.

One pass over every enabled source:

    fetch  ->  dedupe  ->  cross-reference  ->  alert  ->  remember

Design rules that matter:

  * One failing source never aborts a sweep. Each is isolated and its health is
    recorded, because a monitoring product that dies silently is worthless.
  * Nothing alerts twice. Every signal has a fingerprint, and the first thing
    we do is ask the database whether we have seen it.
  * The first run does not spam. Without this, day one would fire hundreds of
    backfill alerts and the user would mute the channel immediately.
  * Early signals are promoted, not repeated. When YC later lists a company we
    called early, we reply in the original thread rather than alerting again.
"""

from __future__ import annotations

import datetime as dt
import logging

from . import crossref
from .config import load_rules, settings
from .db import (
    Alert,
    Entity,
    SourceRun,
    already_seen,
    bump_sweep_counter,
    consecutive_failures,
    init_db,
    is_first_run,
    mark_seen,
    meta_set,
    session,
)
from .models import Signal, SweepResult
from .slack import SlackClient, build_alert, build_digest, build_health, build_promotion
from .sources.base import Source
from .sources.linkedin_social import LinkedInSource
from .sources.speedrun import SpeedrunSource, YCSpeedrunWatcher
from .sources.x_social import XSource
from .sources.yc_directory import YCDirectorySource
from .sources.yc_launches import YCLaunchesSource

log = logging.getLogger("foxy.engine")


def build_sources() -> list[Source]:
    """The registry. Adding a platform later means adding one line here."""
    return [
        YCDirectorySource(),
        YCLaunchesSource(),
        SpeedrunSource(),
        YCSpeedrunWatcher(),
        XSource(),
        LinkedInSource(),
    ]


def source_modes() -> dict[str, str]:
    out: dict[str, str] = {}
    for s in build_sources():
        out[s.name] = s.mode if s.enabled else "disabled"
    return out


class Engine:
    def __init__(
        self,
        slack: SlackClient | None = None,
        *,
        namespace: str = "",
        min_confidence: float | None = None,
    ):
        init_db()
        self.slack = slack or SlackClient()
        self.sources = build_sources()
        # In hosted mode each workspace gets its own seen-set. Without this the
        # second install would be told about nothing, because the first already
        # consumed every company.
        self.namespace = namespace
        self.min_confidence = (
            settings.min_confidence if min_confidence is None else min_confidence
        )
        # Nobody wants 200 messages at once. Anything past this lands in the
        # digest instead, so a bad sweep is noisy in one message, not hundreds.
        self.max_alerts = settings.max_alerts_per_sweep
        # Budget for a first sweep. Spent across sources in the order they run,
        # so the YC directory gets it before the noisier social feeds.
        self._first_run_budget = settings.first_run_alerts

    def _key(self, sig: Signal) -> str:
        return f"{self.namespace}{sig.fingerprint}"

    # -- main entry point --------------------------------------------------

    def sweep(
        self,
        *,
        force_alerts: bool = False,
        prefetched: dict[str, list[Signal]] | None = None,
    ) -> SweepResult:
        """Run one full pass. `force_alerts` ignores the first-run backfill
        guard, used by the `test-alert` command."""
        result = SweepResult(started_at=dt.datetime.now(dt.timezone.utc))
        rules = load_rules()

        with session() as s:
            first_run = is_first_run(s, self.namespace) and not force_alerts
            sweep_no = bump_sweep_counter(s, self.namespace)

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=settings.backfill_days)

        for source in self.sources:
            if not rules.source_enabled(source.name) and source.name != "yc_speedrun_watch":
                result.record(source.name, found=0, new=0, error=None)
                continue
            if not source.enabled:
                result.record(source.name, found=0, new=0, error=None)
                continue
            if sweep_no % max(1, rules.every_n(source.name)) != 0:
                continue

            self._run_source(
                source,
                result,
                first_run=first_run,
                cutoff=cutoff,
                prefetched=prefetched,
            )

        # Promote anything YC has now confirmed, then deliver.
        self._promote_confirmations()
        self._deliver(result)
        self._report_degraded()

        result.finished_at = dt.datetime.now(dt.timezone.utc)
        with session() as s:
            meta_set(s, "last_sweep_at", result.finished_at.isoformat())
        return result

    # -- per source --------------------------------------------------------

    def _run_source(
        self,
        source: Source,
        result: SweepResult,
        *,
        first_run: bool,
        cutoff: dt.datetime,
        prefetched: dict[str, list[Signal]] | None = None,
    ) -> None:
        # Hosted mode fetches each source once and passes the results to every
        # workspace, so the scraping cost does not multiply by install count.
        if prefetched is not None:
            signals = prefetched.get(source.name, [])
            self._absorb(source, signals, result, first_run=first_run, cutoff=cutoff)
            return
        try:
            signals = source.fetch()
        except Exception as exc:  # noqa: BLE001 - isolate every source
            log.exception("source %s failed", source.name)
            result.record(source.name, found=0, new=0, error=f"{type(exc).__name__}: {exc}")
            with session() as s:
                s.add(SourceRun(source=source.name, ok=False, error=str(exc)[:500]))
            return

        self._absorb(source, signals, result, first_run=first_run, cutoff=cutoff)

    def _absorb(
        self,
        source: Source,
        signals: list[Signal],
        result: SweepResult,
        *,
        first_run: bool,
        cutoff: dt.datetime,
    ) -> None:
        """Dedupe, evaluate and record one source's signals for this tenant."""
        new_count = 0

        # On a first sweep, let the newest few through and seed the rest
        # quietly. Suppressing everything meant a new workspace saw nothing at
        # all until the next company happened to appear, which could be a day.
        introduce: set[int] = set()
        if first_run and self._first_run_budget > 0:
            dated = sorted(
                (x for x in signals if x.posted_at is not None),
                key=lambda x: x.posted_at,
                reverse=True,
            )
            for sig in dated[: self._first_run_budget]:
                introduce.add(id(sig))

        with session() as s:
            for sig in signals:
                if already_seen(s, self._key(sig)):
                    continue

                # Remember it immediately. Even if we decide not to alert, we
                # must never reconsider the same item on the next sweep.
                mark_seen(
                    s,
                    fingerprint=self._key(sig),
                    source=sig.source,
                    external_id=sig.external_id,
                    entity_key=f"{self.namespace}{sig.entity_key}",
                )
                new_count += 1

                # First run: introduce the newest few, seed the rest quietly
                # rather than firing months of history.
                if first_run and id(sig) not in introduce:
                    self._upsert_entity(s, sig, alerted=False)
                    continue
                if first_run and id(sig) in introduce:
                    self._first_run_budget -= 1

                self._evaluate(s, sig, result)

        result.record(source.name, found=len(signals), new=new_count, error=None)
        with session() as s:
            s.add(SourceRun(source=source.name, ok=True, found=len(signals), new=new_count))

    # -- decide -------------------------------------------------------------

    def _evaluate(self, s, sig: Signal, result: SweepResult) -> None:
        """Work out whether this is an early signal, and whether to alert."""
        if sig.is_official:
            # YC/Speedrun published it themselves: confirmed by definition.
            sig.confirmed = True
            sig.is_early = False
            self._upsert_entity(s, sig, alerted=True)
            # The ceiling applies here too. It did not, and that is the path
            # the 263-message flood came down: Speedrun is an official source,
            # so every one of its companies went straight to Slack while the
            # cap sat on the other branch doing nothing.
            if len(result.alerts) < self.max_alerts:
                result.alerts.append(sig)
            else:
                result.digest.append(sig)
            return

        # Social signal. The whole question is whether YC already knows.
        # Verify against the directory that governs this company's program:
        # a Speedrun company is never in YC's directory, so checking it there
        # would mark every one of them "early" forever.
        match = crossref.lookup_for_program(
            sig.program, sig.company_name, sig.company_url, text=sig.description
        )
        sig.confirmed = match.found
        sig.is_early = match.is_early
        sig.match_reason = match.reason

        if match.found and match.company:
            # Useful enrichment: the founder's post rarely names the batch, the
            # directory always does.
            sig.batch = sig.batch or match.company.get("batch")
            sig.company_url = sig.company_url or match.company.get("website")

        if sig.is_early:
            sig.confidence = min(
                1.0, sig.confidence + load_rules().score_cfg("early_bonus", 0.15)
            )
        elif match.unknown:
            # We could not verify against the directory, so we must not claim
            # this is early. Discount it and let the digest catch it.
            sig.confidence *= load_rules().score_cfg("unverified_penalty", 0.6)
            sig.add_note("unverified - no company name to check against YC")

        self._upsert_entity(s, sig, alerted=sig.confidence >= self.min_confidence)

        if sig.confidence >= self.min_confidence and len(result.alerts) < self.max_alerts:
            result.alerts.append(sig)
        else:
            result.digest.append(sig)

    def _upsert_entity(self, s, sig: Signal, *, alerted: bool) -> None:
        ent = s.get(Entity, sig.entity_key)
        now = dt.datetime.now(dt.timezone.utc)
        if ent is None:
            s.add(
                Entity(
                    entity_key=sig.entity_key,
                    name=sig.company_name or sig.title,
                    program=sig.program,
                    batch=sig.batch,
                    company_url=sig.company_url,
                    first_signal_source=sig.source,
                    first_signal_at=sig.posted_at or now,
                    was_early=sig.is_early,
                    confirmed=sig.confirmed,
                    confirmed_at=now if sig.confirmed else None,
                    # An entity first seen via an official source was never
                    # "early", so there is nothing to promote later.
                    confirm_notified=sig.is_official,
                    meta={"alerted": alerted},
                )
            )
            return

        # Existing entity. The interesting transition is early -> confirmed.
        if sig.confirmed and not ent.confirmed:
            ent.confirmed = True
            ent.confirmed_at = now
        ent.batch = ent.batch or sig.batch
        ent.company_url = ent.company_url or sig.company_url

    # -- promotion ----------------------------------------------------------

    def _promote_confirmations(self) -> None:
        """Reply in-thread to early alerts that YC has since confirmed."""
        with session() as s:
            pending = (
                s.query(Entity)
                .filter(
                    Entity.was_early.is_(True),
                    Entity.confirmed.is_(True),
                    Entity.confirm_notified.is_(False),
                )
                .all()
            )
            for ent in pending:
                original = (
                    s.query(Alert)
                    .filter(Alert.entity_key == ent.entity_key, Alert.kind == "early")
                    .order_by(Alert.created_at.asc())
                    .first()
                )
                if not original or not original.ts:
                    ent.confirm_notified = True
                    continue

                url = ent.company_url or "https://www.ycombinator.com/companies"
                blocks, text = build_promotion(ent.name, ent.lead_time_days, url)
                try:
                    if not settings.dry_run and settings.slack_configured():
                        self.slack.post(
                            blocks, text, thread_ts=original.ts, channel=original.channel
                        )
                    ent.confirm_notified = True
                    s.add(
                        Alert(
                            fingerprint=f"promo:{ent.entity_key}",
                            entity_key=ent.entity_key,
                            source="yc_directory",
                            kind="promotion",
                            channel=original.channel,
                            ts=original.ts,
                            confidence=1.0,
                            payload={"days": ent.lead_time_days},
                        )
                    )
                except Exception:  # noqa: BLE001 - retry next sweep
                    log.exception("promotion post failed for %s", ent.name)

    # -- delivery -----------------------------------------------------------

    def _deliver(self, result: SweepResult) -> None:
        # Highest-value first: early signals lead, then official listings.
        result.alerts.sort(key=lambda s: (not s.is_early, -s.confidence))

        for sig in result.alerts:
            blocks, text = build_alert(sig)
            if settings.dry_run or not settings.slack_configured():
                log.info("[dry-run] %s", text)
                self._record_alert(sig, channel=None, ts=None)
                continue
            try:
                resp = self.slack.post(blocks, text)
                self._record_alert(sig, channel=resp.get("channel"), ts=resp.get("ts"))
            except Exception:  # noqa: BLE001
                log.exception("failed to post alert for %s", sig.title)

        if result.digest:
            blocks, text = build_digest(result.digest)
            if settings.dry_run or not settings.slack_configured():
                log.info("[dry-run] %s", text)
            else:
                try:
                    self.slack.post(blocks, text)
                except Exception:  # noqa: BLE001
                    log.exception("failed to post digest")

    def _record_alert(self, sig: Signal, *, channel: str | None, ts: str | None) -> None:
        with session() as s:
            s.add(
                Alert(
                    fingerprint=sig.fingerprint,
                    entity_key=sig.entity_key,
                    source=sig.source,
                    kind="early" if sig.is_early else "confirmed",
                    channel=channel,
                    ts=ts,
                    confidence=sig.confidence,
                    payload={
                        "title": sig.title,
                        "company": sig.company_name,
                        "batch": sig.batch,
                        "url": sig.url,
                        "match_reason": sig.match_reason,
                    },
                )
            )

    # -- self-monitoring ----------------------------------------------------

    def _report_degraded(self) -> None:
        """Tell Slack when a source has failed twice running."""
        failures: dict[str, str] = {}
        with session() as s:
            for source in self.sources:
                if consecutive_failures(s, source.name) >= 2:
                    last = (
                        s.query(SourceRun)
                        .filter(SourceRun.source == source.name)
                        .order_by(SourceRun.ran_at.desc())
                        .first()
                    )
                    failures[source.name] = (last.error or "unknown error")[:180]

        if not failures:
            return
        blocks, text = build_health(failures)
        if settings.dry_run or not settings.slack_configured():
            log.warning("[dry-run] %s: %s", text, failures)
            return
        try:
            self.slack.post(blocks, text)
        except Exception:  # noqa: BLE001
            log.exception("failed to post health warning")
