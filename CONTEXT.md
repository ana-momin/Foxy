# Foxy — everything worth knowing

Written when Foxy was finished and live, so that picking it up again later, or
building the next agent, does not mean re-deriving any of this.

Two audiences: whoever comes back to change Foxy, and whoever builds the next
Pond agent. The second half of this file — the mistakes — is the part worth
reading either way.

---

## 1. What it is

A Slack bot that detects new Y Combinator and a16z Speedrun companies, and —
the point of it — catches founders announcing a YC acceptance **before YC
publishes them**.

Built for a Pond bounty ("Build a YC Launch Monitor Slack Bot", posted by a
Senior GTM at Rho). Shipped, rejected once by Pond review, fixed, resubmitted,
approved.

**Live at:**

| | |
|---|---|
| Site / API | https://tryfoxy.vercel.app |
| Pond listing | https://joinpond.ai/agent/list?agent=043bbd2b-c065-432a-bc0e-b3158ff5873c |
| Repo | https://github.com/ana-momin/Foxy (public, MIT) |
| Admin console | `/admin?key=…` — key is in Vercel env `ADMIN_KEY` |

**State at handover:** 5 Slack workspaces (3 active), 297 alerts delivered, 78
of them early, 2,470 items in the seen-set, 20 Pond tasks, 51 Pond runs, 742 of
2,500 search credits spent. ~8,600 lines of app, ~3,000 of tests, 201 tests
passing, 74 commits.

---

## 2. Shape of the thing

```
Slack workspaces ──┐
                   ├──► Postgres (Neon) ◄── GitHub Actions (the 8-hour clock)
Pond marketplace ──┘            ▲
                                │
                        Vercel (FastAPI)
                     web app + Pond endpoints
```

Three separate runtimes share one database, and **that is the single most
important fact about this system**. Almost every serious bug came from
forgetting it:

* **Vercel** serves the site, the Slack OAuth flow and the Pond endpoints.
  Stateless, frozen after responding, and it may be a different instance on
  every request.
* **GitHub Actions** runs the sweeps. Free on public repos, and it has hours
  where a serverless function has minutes.
* **Neon Postgres** is the only thing that persists. Anything in a module-level
  variable is invisible to the other two and dies when an instance freezes.

### Sources

| Source | How | Notes |
|---|---|---|
| YC Directory | Algolia | App `45BWZJ1SGC`, index `YCCompany_By_Launch_Date_production`. **The API key rotates** — scrape `window.AlgoliaOpts` each run, never hardcode it. |
| Launch YC | Inertia JSON | Send `X-Inertia` + the version hash from `/companies`. Must **not** send `Accept: text/html`. |
| a16z Speedrun | `speedrun-api.a16z.com/api/companies/companies/` | DRF limit/offset. The Next.js `_next/data` endpoint returns only 15 of 251 — do not use it. |
| X | `cdn.syndication.twimg.com/tweet-result` + search | Free, no auth. Hydration only; discovery is via search engines. |
| LinkedIn | search engines | serper.dev primary, then DuckDuckGo / Bing / Mojeek. |

Neither Speedrun nor LinkedIn company pages carry dates. That matters — see the
introduction logic in §4.

### Files

| File | Job |
|---|---|
| `app/engine.py` | The sweep. Fetch → dedupe → classify → cross-reference → deliver. |
| `app/classify.py` | Is this text a founder announcing their own acceptance? |
| `app/crossref.py` | Does YC already list this company? |
| `app/hosted.py` | One fetch, fanned out to every workspace. Plus `welcome()`. |
| `app/installs.py` | Workspaces, encrypted tokens, plans, quota. |
| `app/pond_tasks.py` | Durable, resumable async execution for Pond. |
| `app/pond_schema.py` | Validates params against the manifest's own schema. |
| `app/budget.py` | Counts search credits, warns at 80%. |
| `app/admin.py` | Operator console. |
| `tools/pond_conformance.py` | **Run this before every Pond submission.** |

---

## 3. Pond, specifically

Pond is a marketplace *and* a chat agent. **Pond AI reads your manifest and
calls your endpoints on the user's behalf** — the chat is their UI over your
actions. Users often never see your site at all.

### The protocol

* `GET /manifest` — public, no auth
* `POST /runs` — `Bearer <access key>` + `X-Agent-Protocol-Version: 1.0`
* `GET /tasks/{id}` — polling for long actions
* Every terminal response carries `usage`; Pond meters and bills on it
* `Idempotency-Key` must replay, not re-run

### Things that cost real time to learn

**The manifest must pass Pond's JSON Schema exactly.** Failing it produces the
error *"The manifest, runs, and tasks endpoints could not be found"* — which
sounds like a routing problem and is not. A copy of the schema is vendored at
`tests/data/pond-manifest-schema.json` and a test validates against it. Chasing
that error cost a day: trailing-slash redirects and missing HEAD/OPTIONS were
investigated first, and neither was the cause.

**Pricing lives in the manifest** under `metadata.pricing_plans`, and Pond
imports it. Two constraints come from their schema, not from choice:

* `billing_interval` is `const: "month"` — **there is no yearly plan**, and
  trying to declare one is invalid.
* `included_units` is required and must be ≥ 1 — **"unlimited" cannot be
  expressed**. Every plan states a number.
* `usage_unit` must match what the agent reports in `usage`, or you bill for a
  unit you never send. Foxy reports `result`.

Current plans: Free — 50 results. Pro — $3/month, 1,000 results.

**Pond sends no caller identity.** The run body is `run_id`, `action_id`,
`parameters`, and nothing else. Consequences:

* You never verify payment. Pond enforces the allowance *before* calling you.
* A Pond subscription **cannot be matched to a Slack workspace**. There is no
  automatic upgrade path from a Pond purchase to a Slack install.
* `/runs` logs any unread field or header, so if Pond ever starts sending an
  identity, it will show up in the Vercel logs without anyone having to notice.

**A long action cannot use a background task.** See §5.

---

## 4. Decisions that look odd until you know why

**The polls do the work.** `scan_now` returns 202, and each `GET /tasks/{id}`
takes a lease, scans one source within a 45-second budget, writes progress, and
returns. Any instance can resume. A background `asyncio` task cannot: the
instance is frozen once it has responded.

**A source is attempted at most 3 times.** Without that, a source too slow for
one request would be retried forever by the lease and the task would never end.

**"Seen" means delivered, not decided.** Marking an item seen when the alert is
*decided* means an outage does not delay alerts, it deletes them. Anything Slack
does not acknowledge is forgotten at the end of the sweep and offered again.

**Every row is namespaced per workspace** — `Seen`, `Entity` *and* `Alert`.
`confirm_notified` is a statement about one channel; shared, the first workspace
to sweep silences everybody else's promotion.

**"First run" is per source, not per workspace.** A workspace meets its sources
at different times — the welcome sweep reads only the two YC feeds. Without
this, Speedrun and LinkedIn count as long-established the moment the welcome
finishes, and arrive as a wall.

**The introduction falls back to source order when there are no dates.**
Speedrun and LinkedIn company pages carry no timestamps at all, so picking "the
newest few by date" meant they could never introduce themselves.

**Early requires two things**, not one: the company is absent from the YC
directory **and** the signal is a person announcing their own acceptance. Plus
a recency check on the batch. See §6.

**Plans expire when asked, not by a nightly job.** A plan must not outlive its
payment because some scheduled task failed to run.

**`init_db()` returns immediately once it has succeeded.** It inspects the live
database, which costs seconds against hosted Postgres, and callers treat it as
free.

**The Vercel region is pinned to `sin1`** to sit beside Neon in
`ap-southeast-1`. It defaulted to Virginia; every database round trip crossed
the Pacific.

---

## 5. Every bug that reached production

Ordered by what they teach.

### Hosted alerts were never delivered — 687 recorded, 0 sent

Delivery was gated on `settings.slack_configured()`, which reads the *global*
`SLACK_BOT_TOKEN`. Hosted mode has no global token; each workspace carries its
own. Every sweep silently took the dry-run branch: logged, wrote a row with a
null message id, posted nothing.

Worse than the bug: the sweep reported `len(result.alerts)` — *decisions*, not
deliveries — so it said "15 alerts posted" and I repeated that. **Lesson: never
report success from the same side that produced it.** Ask Slack.

### The two environments held different `ENCRYPTION_KEY`s

The web app wrote Slack tokens; the scheduler could not read them. Decryption
returned `""`, which was indistinguishable from "no token". Every hosted sweep
dry-ran for a day.

Fixes: one key in both places, and every stored blob now carries a fingerprint
of the key that wrote it, so a mismatch is *reported* rather than silent.

### A timed-out welcome sweep silenced a workspace permanently

The first attempt marked several hundred companies seen, then hit the 5-minute
serverless limit before sending anything. The retry found "nothing new".

### The alert ceiling did not apply to official sources

The cap sat on the social branch only, so Speedrun — an official source — sent
263 messages in one go.

### `_do_early` filtered after the limit

`recent_detections(only_early=true)` took the newest N alerts and *then* kept
the early ones — empty whenever the newest few happened to be confirmations,
while `search_early_signals` was listing three. Filter in the query.

### Two schedulers on the same cron

`monitor.yml` (self-hosted, single workspace) ran alongside `hosted-sweep.yml`,
with no Slack token, failing every 8 hours for days. It **doubled the search
spend** and made a real failure indistinguishable from the standing one. Its
schedule is now commented out.

### Smaller ones worth remembering

* `python-multipart` missing from `requirements.txt` → 500 on every form post.
  There is now a test that scans imports against requirements.
* Vercel `rewrites` pass the *destination* path — they broke routing entirely.
* `channels:join` was never requested, so the bot joined nothing and no channel
  could say "Foxy is already in this one".
* Slack's read methods (`conversations.info`) take **query parameters**; posting
  JSON answers `invalid_arguments`, which reads like a bad channel id.
* `%-I` in `strftime` is a glibc extension and raises on Windows.
* serper rejects `num` other than 10 on free accounts.
* Neon: `DEFAULT 0` is invalid on a BOOLEAN column.

---

## 6. The Pond rejection — all five points

The verdict was:

> All four synchronous actions returned valid Pond results with good warm
> latency. The primary scan was accepted and pollable but failed after about
> 166 seconds with an OperationalError. At least three of five early-signal
> results did not match the founder-announcement semantics. Fix scan
> reliability, honor requested source scope, persist task/idempotency state,
> enforce schemas, and improve signal precision.

| # | Finding | Root cause | Fix |
|---|---|---|---|
| 1 | Scan died at 166s | Background `asyncio` task on a frozen instance, dragging its DB connection down | Polls drive the work, one source per slice |
| 2 | Source scope ignored | `sources` was declared in the schema and never read | `Engine.sweep(only=…)` |
| 3 | Task/idempotency state lost | Both were module-level dicts, invisible across instances | Postgres tables |
| 4 | Schemas not enforced | Declared `additionalProperties: false` and checked nothing | `pond_schema.validate` against the manifest's own declarations |
| 5 | Early signals wrong | `is_early = match.is_early` asked only whether YC listed the company | Requires an announcement **and** a current batch |

On #5, the actual failures were **UzCombinator** (a LinkedIn company page whose
name merely contains "Combinator"), **Grubwithus** (real YC company — batch
**W2011**), **DRS**, **Infragrid**, and **TechCrunch**. Auditing the stored
history found **55 of 84 early signals did not qualify — 65%**, against their
"three of five". Production went 84 → 29 with zero failures.

### Why it was rejected at all

**I had never once called `/runs` myself.** Everything was tested through Slack.
All five findings were reachable from a single HTTP session, and none were
reachable from the tests I had written.

That is the whole lesson. `tools/pond_conformance.py` exists because of it — 15
checks against a live deployment, exits non-zero, meant to gate a submission.

---

## 7. Running it

### Secrets

| Where | What |
|---|---|
| Vercel | `DATABASE_URL`, `ENCRYPTION_KEY`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `POND_ACCESS_KEY`, `ADMIN_KEY`, `PUBLIC_BASE_URL`, `POND_LISTING_URL`, `CONTACT_X`, `SUPPORT_EMAIL` |
| GitHub Actions | `DATABASE_URL`, `ENCRYPTION_KEY`, `SERPER_API_KEY` |

`ENCRYPTION_KEY` **must be identical in both**. `SERPER_API_KEY` must be in
Actions — the sweeps run there, and a key that lives only in a local `.env` is a
key the scheduler does not have.

### Commands

```bash
python -m app.cli hosted-sweep     # what the scheduler runs
python -m app.cli hosted-doctor    # check every workspace against Slack itself
python -m app.cli hosted-repair    # park unreadable installs, drop unsent alerts
python -m app.cli audit-early      # re-check stored early signals (--fix to apply)
python -m app.cli budget           # search credits used and left
python -m app.cli set-plan <code|name> pro --months 12
```

The workflow has a `mode` input: `sweep`, `doctor`, `doctor-post`, `repair`,
`audit-early`, `audit-early-fix`.

### Things that will eventually need attention

* **Search credits.** 742 of 2,500 spent. One Slack warning fires at 80%. When
  it runs out, X and LinkedIn degrade quietly; the three YC sources are fine.
* **GitHub disables scheduled workflows after 60 days without a push.** Any
  commit resets it.
* **Vercel Hobby forbids commercial use.** Irrelevant while Pond takes the
  money; it matters if Foxy ever bills directly.
* **`umer`** installed but never picked a channel, so gets nothing. A DM nudge
  on `im:write` would recover that class of drop-off. Never built.

---

## 8. For the next agent

The things that would have saved the most time, in order:

1. **Call the marketplace's own endpoints before submitting.** Not your UI —
   theirs. Write the conformance script first, not after a rejection.
2. **Verify from the other side.** If you post to Slack, ask Slack. If you write
   a row, read it back. A success count produced by the thing under test is not
   evidence. This one bug — 687 alerts recorded and none sent — is the whole
   argument.
3. **Assume serverless will freeze you mid-work.** No background tasks, no
   in-memory state, nothing that outlives a response.
4. **Silence is the enemy.** Every failure here looked like success: empty
   decryption, dry-run delivery, a seen-set built by a run that sent nothing. If
   a failure mode can return an empty string, make it say why instead.
5. **Advertising a contract obliges you to keep it.** A declared schema that is
   not enforced, or a limit that is not checked, is worse than not declaring it.
6. **Read the platform's schema before designing around it.** Monthly-only
   billing and mandatory `included_units` both changed the product, and both
   were discoverable in ten minutes.
7. **Do not sell what you cannot verify.** An "I have subscribed" button nobody
   can check is theatre. Concierge sales are slower and honest.
8. **One number, one meaning.** Decisions are not deliveries. Views are not
   runs — Pond's card said 261 users and 486 plays while the agent had been
   called 65 times, because Pond AI answers many questions from the manifest
   alone.

And the process one: **when the user says "make sure it all works", that is a
request to go and check, not to re-read the code.** Every time this was done
properly, it found something.
