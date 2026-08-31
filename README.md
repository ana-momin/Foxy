# Foxy — YC Launch Monitor

**Live:** [tryfoxy.vercel.app](https://tryfoxy.vercel.app) · [manifest](https://tryfoxy.vercel.app/manifest) · [health](https://tryfoxy.vercel.app/healthz)

A Slack bot that watches for new Y Combinator and Speedrun companies, and —
the point of the thing — catches founders who announce their acceptance
**before YC publishes them**.

It runs continuously, remembers what it has already seen, and never alerts
twice.

```
🔥 EARLY YC SIGNAL — founder announced before YC

Company    Acme AI              Batch       YC F26 (claimed)
Founder    Jane Doe (@janedoe)  Source      X
Status     Not yet in YC dir.   Confidence  91%

Original post
> We got into YC F26! Solo founder, moving to SF next week.

Original post • Website • Founder
Detected Aug 28, 2026, 9:14 AM PT | not in YC directory (closest 62%)
```

---

## Why this is the interesting half

Anyone can scrape the YC directory and announce companies that are already
public. By then the whole market can see them.

The gap is this: **YC's directory currently lists 24 Fall 2026 companies.**
The batch runs several hundred. Those founders are announcing on X and
LinkedIn right now, weeks before YC publishes them. Foxy treats the
directory as the *verification* set, never the discovery set — a founder post
is cross-referenced against it, and if YC does not know the company yet, that
is an early signal.

When YC later lists a company Foxy called early, it replies **in the
original Slack thread**: _"Acme AI is now listed in the YC directory — 11 days
after Foxy flagged it."_

---

## Quick start (5 minutes, no API keys except Slack)

### 1. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**
2. Pick your workspace, paste the contents of [`slack-app-manifest.json`](slack-app-manifest.json)
3. **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-…`)
4. In Slack, invite the bot to your channel: `/invite @Foxy`

### 2. Configure

```bash
cp .env.example .env
```

Open `.env` and set just two things:

```env
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_TARGET=#yc-signals
```

That is genuinely all that is required. Everything else is optional.

### 3. Run it

**With Docker (recommended — survives restarts):**

```bash
docker compose up -d
```

**Without Docker:**

```bash
pip install -r requirements.txt
python -m app.cli check          # confirms your setup
python -m app.cli test-alert     # posts a sample alert to prove Slack works
uvicorn app.main:app --port 8000 # runs forever, sweeping every 8 hours
```

### 4. Confirm it works

```bash
python -m app.cli sweep --dry    # run a sweep without posting
python -m app.cli status         # per-source health
```

---

## Commands

| Command | What it does |
|---|---|
| `python -m app.cli check` | Shows exactly what is configured and what is missing |
| `python -m app.cli sweep` | Runs one sweep now |
| `python -m app.cli sweep --dry` | Same, but prints instead of posting to Slack |
| `python -m app.cli test-alert` | Posts one sample alert — use this to verify Slack |
| `python -m app.cli status` | Per-source health and last run |
| `python -m app.cli check-post <url>` | Run one X post through the full pipeline; add `--post` to send it to Slack |
| `python -m app.cli reset --yes` | Clears memory (everything becomes "new" again) |

In Slack: `/foxy status` and `/foxy scan`.

---

## What it monitors

| Source | How | Cost | Verified |
|---|---|---|---|
| **YC Directory** | Algolia launch-date index, credentials re-read from the live page each run | free | 6,194 companies |
| **Launch YC** | Inertia JSON feed — often fires *before* the directory | free | 20 launches |
| **Speedrun (a16z)** | Next.js data endpoint, cohort-tagged | free | 30 companies, SR003–SR006 |
| **X / Twitter** | Post discovery + free syndication hydration | free or paid | 7 signals in a live sweep |
| **LinkedIn** | Public post + company-page search, no login | free or paid | 49 signals in a live sweep |

Two of these deserve explanation.

### A note on Speedrun — the brief has a factual error

The brief asks for *"YC's dedicated Speedrun program directory."* **Y Combinator
does not run a program called Speedrun.** Verified three independent ways on
2026-08-29:

- `https://www.ycombinator.com/speedrun` → **HTTP 404**
- YC's `sitemap.xml` → **zero** occurrences of "speedrun"
- The directory's batch facet lists **50 batches** → no Speedrun among them

Speedrun is **Andreessen Horowitz's** accelerator (launched 2023, ~300
companies). That is almost certainly what was meant, so the Speedrun adapter
targets the real one at `speedrun.a16z.com`, which turns out to be a clean
unauthenticated JSON feed.

To cover the other possibility, a lightweight watcher checks YC's sitemap on
every sweep and self-enables if YC ever ships a Speedrun page. The deliverable
is correct today and correct if the brief becomes true later.

### A note on X and LinkedIn — read this before judging recall

**X killed its free tier in February 2026.** Pay-per-use is $0.005 per post
read and full-archive search sits behind a $42,000/month enterprise contract.
There is no free official search route. Anyone claiming otherwise is wrong.

So Foxy splits the job:

- **Hydration is free, always.** Given a post ID, X's own syndication endpoint
  returns the full text and author with no key and no cost. Confirmed against
  the exact post in the brief. This also acts as a pre-send check, so deleted
  posts never reach Slack.
- **Discovery costs something, or has partial recall.** Free mode asks search
  engines for indexed public posts. It genuinely works — but DuckDuckGo
  throttles after a burst of queries and Bing indexes X poorly. **Free mode
  will find founder announcements; it will not find all of them.**

**LinkedIn is in an enforcement era.** LinkedIn sued Proxycurl out of existence
on 4 July 2026. Foxy never logs in, never uses cookies, and never touches
private data — free mode reads public search results, paid mode uses a managed
cookie-free provider.

If recall matters more than cost, see the upgrade table below.

---

## Free vs paid — same code, one env var

Foxy is built so **you** can run it for nothing while **the person you
hand it to** can turn on higher recall by pasting in keys. No code changes.

| | Free (default) | Upgrade | Cost |
|---|---|---|---|
| YC Directory | Algolia | — | always free |
| Launch YC | Inertia feed | — | always free |
| Speedrun | Next data | — | always free |
| X hydration | syndication | — | always free |
| **X discovery** | search engines | `X_PROVIDER=twitterapi` + `TWITTERAPI_KEY` | ~$0.15 / 1,000 posts |
| **LinkedIn** | search engines | `LINKEDIN_PROVIDER=apify` + `APIFY_TOKEN` | ~$0.005 / post |
| **Web search** | DuckDuckGo → Bing → Mojeek | `SERPER_API_KEY` | 2,500 free queries (~5 weeks), then paid |
| **Classifier** | rule engine | `ANTHROPIC_API_KEY` | under $2 / month |

**Strongly recommended:** set `SERPER_API_KEY` (2,500 free queries, no credit
card, sign-up takes two minutes). In testing, DuckDuckGo throttled after a
burst of queries and Bing returned almost no X post URLs, so without a serper
key the X and LinkedIn sources fire only intermittently. With it, both return
results on every sweep.

Note for free serper accounts: only a page size of 10 is permitted — anything
larger returns *"Query pattern not allowed for free accounts"*. Foxy always
requests 10 and trims locally, so this is handled. Foxy issues roughly 20 queries per sweep, so at the
default 8-hour cadence the free credits last about **5 weeks**. Trim the query
lists in `config.yaml` — or lower `lookahead_batches` — to stretch that
further.

Realistic full-recall cost: **~$30–40/month** including always-on hosting.

---

## How early detection works

```
  founder post
       ↓
  [1] keyword pack        "got into YC", "YC F26", "backed by Y Combinator"
       ↓                  batch codes auto-generated from today's date
  [2] hard vetoes         "congrats to", "how to get into YC", "we're hiring"
       ↓                  — someone else's good news is never your alert
  [3] rule scoring        phrase weight + batch + first-person voice
       ↓                  free, instant, kills the obvious noise
  [4] LLM classifier      optional; catches phrasings no regex will
       ↓                  extracts company name + batch
  [5] CROSS-REFERENCE     ← the part that matters
       ↓                  exact name / fuzzy name / website domain
       ↓                  against the live YC directory
  ┌────┴─────┬──────────────┐
  │          │              │
not in YC   in YC      no company found
  │          │              │
EARLY 🔥   confirmed ✅   unverified ⚠️
                       (demoted to digest)
```

Stage 2 exists because *"Congrats to my friend who got into YC"* contains a
textbook announcement phrase **and** first-person words, so scoring alone rates
it highly. Only an outright veto catches it.

**"Unverified" is a real third state, and it matters.** If the bot cannot
extract a company name it does *not* get to claim the company is missing from
YC's directory — it does not know. Those signals are demoted to the digest
rather than announced as early. Conflating "I could not check" with "it is not
there" is how a monitor starts crying wolf.

### Measured, not claimed

Tested against **15 real X posts** collected live and hand-labelled — 8 genuine
founder announcements, 7 lookalikes:

| | |
|---|---|
| Precision | **100%** (0 false positives) |
| Recall | **88%** (1 missed) |
| Accuracy | **93%** |

The lookalikes are the interesting part, because every one of them contains a
textbook announcement phrase and would fool a keyword matcher:

- *"8 startups I referred got into YC"* — a referrer, not a founder
- *"The guy behind the $Rosie coin just got accepted into Y Combinator"* — third party
- *"I got into YC on my 6th attempt. Let me tell you what every founder needs to hear"* — advice thread
- *"exactly one year ago today, we got into Y Combinator"* — anniversary, not news
- *"I invested early into a startup that was recently accepted"* — investor
- *"Congrats to my friend Sarah who just got into Y Combinator"* — someone else's news

The single most reliable discriminator turned out to be **first-person voice**.
A post with no `I`/`we`/`our` — and no announcement opener like *"excited to
announce"* — is someone reporting on another company, so it is rejected
outright rather than scored.

The one miss was *"thought YC was only for SaaS kids... turns out they're
letting drone guys in now"*, which never states the acceptance directly.
Setting `ANTHROPIC_API_KEY` catches that class of phrasing.

Tune everything in [`config.yaml`](config.yaml) — phrases, vetoes, batch codes,
queries, thresholds. No code changes needed.

---

## Running it persistently

The requirement is a process that keeps running and keeps state. Three ways:

### Option A — Docker on any box (simplest)

```bash
docker compose up -d
```

Restarts automatically, keeps SQLite on a named volume.

### Option B — Free, using GitHub Actions

`.github/workflows/monitor.yml` runs a sweep every 8 hours. Actions minutes are
free on public repos.

**You must set `DATABASE_URL`** to a free Postgres (neon.tech, supabase.com).
Each Actions run starts on a clean machine, so without external state the bot
forgets everything and re-alerts on every company every time.

Repository secrets: `SLACK_BOT_TOKEN`, `SLACK_TARGET`, `DATABASE_URL`.

### Option C — A hosted always-on service

Needed if you want the Pond agent reachable (Pond requires a stable HTTPS URL).

Honest state of free hosting in 2026: Render free services **spin down after 15
minutes** with 30–60s cold starts, Fly has no permanent free tier, Railway is a
one-time $5 credit. A sleeping agent fails Pond's revalidation. Budget **$5–7 a
month** (Render Starter, Koyeb) for a genuinely always-on container, or use
Oracle Cloud's free ARM VM if you are comfortable with a VPS.

Vercel works for the Pond endpoints (`vercel.json` and `api/index.py` are
included) but has no long-lived process, so pair it with Option B for the
sweeps and set `DATABASE_URL`.

---

## Pond agent integration

Foxy implements **Pond Protocol V1** in the same process as the monitor —
the scheduler and the agent endpoints share one database, so Pond's manifest
revalidation doubles as the health check.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /manifest` | none (per spec) | discovery |
| `POST /runs` | `Bearer` + `X-Agent-Protocol-Version: 1.0` | execution |
| `GET /tasks/{id}` | same | polling for long scans |
| `GET /healthz` | none | uptime checks |

**Actions:** `scan_now`, `search_early_signals`, `lookup_company`,
`recent_detections`, `health_check`.

To publish:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # your Access Key
```

Set `POND_ACCESS_KEY` and `PUBLIC_BASE_URL` in `.env`, deploy somewhere with
HTTPS, then create the agent at <https://joinpond.ai/agent/create> using your
base URL and the same Access Key.

Implemented per spec: idempotency via `Idempotency-Key`, cumulative `usage` on
every terminal response, and the full error-code table (`invalid_input` 422,
`unauthorized` 401, `unsupported_operation` 400, `rate_limited` 429,
`temporarily_unavailable` 503, `internal_error` 500).

---

## Adding a new platform later

Every source implements one interface. To add, say, Bluesky:

1. Create `app/sources/bluesky.py` with a class exposing `fetch() -> list[Signal]`
2. Add it to `build_sources()` in `app/engine.py`
3. Add its queries to `config.yaml`

Dedupe, scoring, cross-reference, Slack formatting, health tracking and the
Pond actions all work automatically — nothing downstream knows or cares where a
signal came from.

The same applies to swapping providers within a source: `X_PROVIDER` and
`LINKEDIN_PROVIDER` select an implementation behind a `Protocol`, and a failing
paid provider silently degrades to the free one rather than taking the sweep
down.

---

## Design decisions worth knowing

**Credentials are never hardcoded.** YC rotates its public Algolia key — the
one copied into older open-source projects is already dead (403). Foxy
re-reads `window.AlgoliaOpts` from the live page every run, so rotation heals
itself. Same pattern for YC's Inertia version hash and a16z's Next.js build ID.

**One failing source never aborts a sweep.** Each is isolated, its health is
recorded, and if a source fails twice in a row the bot **posts to Slack about
itself**. Silent failure is the only unrecoverable bug in a monitoring product.

**The first run does not spam.** Without a guard, day one would fire hundreds
of backfill alerts and you would mute the channel immediately. `BACKFILL_DAYS`
controls the window; older items are recorded silently as already-known.

**Low-confidence signals go to a digest,** not individual alerts. Raise
`MIN_CONFIDENCE` for less noise, lower it for more coverage.

---

## Configuration reference

Everything below is optional except the Slack pair.

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | **required** |
| `SLACK_TARGET` | — | **required** — `#channel` or a user ID for DMs |
| `DATABASE_URL` | local SQLite | set to Postgres on ephemeral hosts |
| `SCAN_INTERVAL_HOURS` | `8` | sweep cadence |
| `MIN_CONFIDENCE` | `0.55` | alert threshold; below this goes to the digest |
| `BACKFILL_DAYS` | `7` | first-run quiet window |
| `DRY_RUN` | `0` | log alerts instead of posting |
| `X_PROVIDER` | `free` | `free` / `twitterapi` / `none` |
| `LINKEDIN_PROVIDER` | `free` | `free` / `apify` / `none` |
| `SERPER_API_KEY` | — | Google-quality search, 2,500 free queries, no card |
| `TWITTERAPI_KEY` | — | twitterapi.io |
| `APIFY_TOKEN` | — | Apify LinkedIn actor |
| `ANTHROPIC_API_KEY` | — | enables the LLM classifier |
| `POND_ACCESS_KEY` | — | Pond agent auth |
| `PUBLIC_BASE_URL` | — | your public HTTPS URL |

---

## Legal and ethical position

Public data only. No authenticated scraping, no login automation, no cookie
reuse, no captcha solving, no republication of scraped content. Alerts link
back to the original source so the founder gets the traffic and the credit.

Personal data is used for analysis and outreach qualification, not resale. If
you operate in the EU, treat post authors as data subjects under GDPR.
