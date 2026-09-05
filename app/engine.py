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
import re
import logging

from . import crossref
from .config import load_rules, settings
from .db import (
    Alert,
    Entity,
    Seen,
    SourceRun,
    bump_sweep_counter,
    consecutive_failures,
    init_db,
    is_first_run,
    mark_seen,
    entities_for,
    meta_set,
    seen_fingerprints,
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


def _batch_is_current(batch: str) -> bool:
    """Is this cohort one YC is announcing around now?

    An old batch means the company was accepted years ago, so its absence from
    the directory says something about the directory, not about YC being slow
    to publish. Grubwithus announced YC W2011 and was reported as an early
    signal on exactly that confusion.

    Batches are written both ways - "Winter 2011" and "YC W11" - so both are
    read. An unrecognisable string is treated as current, because guessing
    wrongly here silences real signals.
    """
    text = (batch or "").upper()
    now = dt.date.today().year

    if (full := re.search(r"(20\d{2})", text)) is not None:
        return abs(int(full.group(1)) - now) <= 1

    # "YC W11", "S26", "F26", "X26" - a season letter and two digits.
    if (short := re.search(r"\b[WSFX](\d{2})\b", text)) is not None:
        return abs((2000 + int(short.group(1))) - now) <= 1

    return True


class Engine:
    def __init__(
        self,
        slack: SlackClient | None = None,
        *,
        namespace: str = "",
        min_confidence: float | None = None,
        dry_run: bool | None = None,
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
        # Whether to actually post. Carried on the engine rather than read from
        # global settings at each decision: a caller that asked not to post is
        # making a statement about its own run, and two runs sharing a process
        # would otherwise flip the global under one another - one of them
        # posting to Slack after being told not to.
        self._dry_run = dry_run
        self.max_alerts = settings.max_alerts_per_sweep
        # Budget for a first sweep. Spent across sources in the order they run,
        # so the YC directory gets it before the noisier social feeds.
        self._first_run_budget = settings.first_run_alerts
        # Entities for the source being processed, loaded in one query.
        self._entities: dict[str, Entity] = {}
        self._entities_known: set[str] = set()

    @property
    def dry_run(self) -> bool:
        """Settings supply the default; an explicit argument overrides it."""
        return settings.dry_run if self._dry_run is None else self._dry_run

    def _key(self, sig: Signal) -> str:
        return f"{self.namespace}{sig.fingerprint}"

    # -- main entry point --------------------------------------------------

    def sweep(
        self,
        *,
        force_alerts: bool = False,
        prefetched: dict[str, list[Signal]] | None = None,
        only: tuple[str, ...] | None = None,
    ) -> SweepResult:
        """Run one pass.

        `force_alerts` ignores the first-run backfill guard, used by the
        `test-alert` command. `only` restricts the pass to the named sources -
        a caller that asked for the YC directory should get the YC directory
        and nothing else, and a caller working to a deadline needs to be able
        to leave out the paced social searches.
        """
        result = SweepResult(started_at=dt.datetime.now(dt.timezone.utc))
        rules = load_rules()

        with session() as s:
            sweep_no = bump_sweep_counter(s, self.namespace)
        self._force_alerts = force_alerts

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=settings.backfill_days)

        for source in self.sources:
            if only is not None and source.name not in only:
                continue
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
                first_run=False,  # decided per source, inside
                cutoff=cutoff,
                prefetched=prefetched,
            )

        # Promote anything YC has now confirmed, then deliver.
        self._promote_confirmations()
        self._deliver(result)
        self._forget_undelivered(result)
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

        # Is this the first time this workspace has looked at this source? The
        # welcome sweep reads only the two YC feeds, so Speedrun, X and
        # LinkedIn are each met later, and each deserves an introduction rather
        # than being treated as long-established and dumped in full.
        with session() as s:
            first_run = (
                is_first_run(s, self.namespace, source.name)
                and not self._force_alerts
            )
        # Each source gets its own introduction, capped overall by max_alerts.
        budget = settings.first_run_alerts if first_run else 0

        # On a first sweep, let the newest few through and seed the rest
        # quietly. Suppressing everything meant a new workspace saw nothing at
        # all until the next company happened to appear, which could be a day.
        introduce: set[int] = set()
        if first_run and budget > 0:
            dated = sorted(
                (x for x in signals if x.posted_at is not None),
                key=lambda x: x.posted_at,
                reverse=True,
            )
            # Not every source dates its entries: the Speedrun directory and
            # LinkedIn company pages carry none at all. Picking only from dated
            # signals meant those two could never introduce themselves, so a
            # new workspace saw nothing from them however many were waiting.
            # Fall back to the source's own order, which leads with the newest.
            chosen = dated[:budget]
            if len(chosen) < budget:
                have = {id(x) for x in chosen}
                for sig in signals:
                    if sig.posted_at is None and id(sig) not in have:
                        chosen.append(sig)
                        if len(chosen) >= budget:
                            break
            for sig in chosen:
                introduce.add(id(sig))

        with session() as s:
            # Ask once what this workspace has seen, rather than once per
            # signal. Several hundred round trips to a hosted database is what
            # made the first sweep too slow to finish inside a web request.
            seen = seen_fingerprints(s, self.namespace, source.name)
            fresh = [x for x in signals if self._key(x) not in seen]
            # Same again for entities: one query for all of them, rather than a
            # lookup per signal.
            keys = [self._entity_key(x) for x in fresh]
            self._entities = entities_for(s, keys)
            # A key we asked about and did not get back does not exist. Without
            # remembering that, every new company still costs its own lookup -
            # which on a first sweep is every single one of them.
            self._entities_known = set(keys)
            for sig in fresh:
                seen.add(self._key(sig))

                # Remember it immediately. Even if we decide not to alert, we
                # must never reconsider the same item on the next sweep.
                mark_seen(
                    s,
                    fingerprint=self._key(sig),
                    source=sig.source,
                    external_id=sig.external_id,
                    entity_key=self._entity_key(sig),
                    known_new=True,  # the batched seen-set already said so
                )
                new_count += 1

                # First run: introduce the newest few, seed the rest quietly
                # rather than firing months of history.
                if first_run and id(sig) not in introduce:
                    self._upsert_entity(s, sig, alerted=False)
                    continue
                if first_run and id(sig) in introduce:
                    budget -= 1

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
        sig.match_reason = match.reason

        # "Early" is a claim about a founder announcing before YC published,
        # so it needs both halves. Reading only the directory made anything YC
        # had not listed an early founder signal - including LinkedIn company
        # pages, which announce nothing, and which is why three of five early
        # results did not match founder-announcement semantics.
        sig.is_early = match.is_early and sig.is_announcement
        if match.is_early and not sig.is_announcement:
            sig.add_note("not a founder announcement - reported as a lead")

        # An announcement about a batch that closed years ago is not news.
        # Grubwithus announced YC W2011; the directory simply no longer lists
        # it, which is not the same as YC not having got there yet.
        if sig.is_early and sig.batch and not _batch_is_current(sig.batch):
            sig.is_early = False
            sig.confidence *= 0.5
            sig.add_note(f"batch {sig.batch} is not a current cohort")

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

    def _entity_key(self, sig: Signal) -> str:
        """Entities are per-workspace too.

        `confirm_notified` records that *this* channel has been told a company
        was confirmed. Shared across workspaces, the first one to sweep would
        silence the promotion for everybody else.
        """
        return f"{self.namespace}{sig.entity_key}"

    def _upsert_entity(self, s, sig: Signal, *, alerted: bool) -> None:
        key = self._entity_key(sig)
        ent = self._entities.get(key)
        if ent is None and key not in self._entities_known:
            ent = s.get(Entity, key)
        now = dt.datetime.now(dt.timezone.utc)
        if ent is None:
            row = Entity(
                entity_key=key,
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
            s.add(row)
            # Two signals in one sweep can be the same company.
            self._entities[key] = row
            self._entities_known.add(key)
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
                    # This workspace's entities only. Unscoped, the first
                    # workspace to sweep would take everyone's pending
                    # promotions, try to post them into channels its token
                    # cannot reach, and mark them notified either way.
                    Entity.entity_key.like(f"{self.namespace}%"),
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
                    if not self.dry_run and self.slack.usable:
                        self.slack.post(
                            blocks, text, thread_ts=original.ts, channel=original.channel
                        )
                    ent.confirm_notified = True
                    s.add(
                        Alert(
                            fingerprint=f"{self.namespace}promo:{ent.entity_key}",
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

        if result.alerts and not (self.dry_run or self.slack.usable):
            # Nowhere to post. Say so loudly: a sweep that decides on alerts it
            # cannot deliver is a failure, not a quiet success.
            # getattr, because the only thing the engine requires of a client
            # is `usable` and `post`; the rest is detail for the message.
            result.delivery_errors.append(
                "no usable Slack client: "
                f"token={'set' if getattr(self.slack, 'token', '') else 'missing'}, "
                f"channel={getattr(self.slack, 'target', '') or 'missing'}"
            )

        for sig in result.alerts:
            blocks, text = build_alert(sig)
            if self.dry_run or not self.slack.usable:
                log.info("[dry-run] %s", text)
                self._record_alert(sig, channel=None, ts=None)
                continue
            try:
                resp = self.slack.post(blocks, text)
                self._record_alert(sig, channel=resp.get("channel"), ts=resp.get("ts"))
                # Slack answers with a message id. Only that counts as sent.
                if resp.get("ts"):
                    result.delivered.append(sig)
                else:
                    result.delivery_errors.append(
                        f"{sig.title}: Slack accepted the call but returned no message id"
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to post alert for %s", sig.title)
                result.delivery_errors.append(f"{sig.title}: {exc}"[:200])

        if result.digest:
            blocks, text = build_digest(result.digest)
            if self.dry_run or not self.slack.usable:
                log.info("[dry-run] %s", text)
            else:
                try:
                    self.slack.post(blocks, text)
                except Exception:  # noqa: BLE001
                    log.exception("failed to post digest")

    def _forget_undelivered(self, result: SweepResult) -> None:
        """An item is only "seen" once the workspace has actually been told.

        Marking it at decision time is right for suppressing duplicates and
        wrong for everything else: when delivery failed, the item was recorded
        as reported and never reconsidered, so an outage did not delay alerts,
        it deleted them. Whatever did not reach Slack is forgotten here and
        offered again on the next sweep.
        """
        missed = [s for s in result.alerts if s not in result.delivered]
        if not missed:
            return
        keys = [self._key(s) for s in missed]
        with session() as s:
            s.query(Seen).filter(Seen.fingerprint.in_(keys)).delete(
                synchronize_session=False
            )
        log.warning("%d alert(s) went undelivered and will be retried", len(missed))

    def _record_alert(self, sig: Signal, *, channel: str | None, ts: str | None) -> None:
        with session() as s:
            s.add(
                Alert(
                    # Namespaced like the seen-set. Without this an alert row
                    # belongs to no particular workspace, so in hosted mode
                    # there is no way to ask what a given one actually received.
                    fingerprint=self._key(sig),
                    entity_key=self._entity_key(sig),
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
                        # Recorded so the stored history can be audited on the
                        # same rule the live path uses.
                        "is_announcement": bool(sig.is_announcement),
                        "author": sig.author_name or sig.author_handle or "",
                    },
                )
            )

    # -- self-monitoring ----------------------------------------------------

    def _warn_if_search_budget_low(self) -> None:
        """Say something before the search allowance runs out.

        When serper credits go, X and LinkedIn fall back to engines that mostly
        return nothing: early detection degrades and every health check still
        reads "ok". A warning once, at the point it can still be acted on.
        """
        from . import budget

        if not budget.should_warn():
            return
        snap = budget.snapshot()
        text = (
            f"Foxy has used {snap.get('used')} of its {snap.get('allowance')} "
            "search credits. When they run out, X and LinkedIn early detection "
            "gets much weaker; the YC and Speedrun sources are unaffected."
        )
        headline = ":warning: *Search credits running low.*"
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": headline + "\n" + text},
            }
        ]
        if self.dry_run or not self.slack.usable:
            log.warning(text)
            return
        try:
            self.slack.post(blocks, "Foxy: search credits running low")
        except Exception:  # noqa: BLE001 - a warning is not worth failing over
            log.warning("could not deliver the search-budget warning")

    def _report_degraded(self) -> None:
        """Tell Slack when a source has failed twice running."""
        self._warn_if_search_budget_low()

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
        if self.dry_run or not self.slack.usable:
            log.warning("[dry-run] %s: %s", text, failures)
            return
        try:
            self.slack.post(blocks, text)
        except Exception:  # noqa: BLE001
            log.exception("failed to post health warning")
