<div align="center">

<img src="foxy.png" alt="Foxy" width="120" height="120">

# Foxy

**Know about new YC startups first.**

Foxy watches Y Combinator, Speedrun, X and LinkedIn, then messages your Slack the
moment a new company appears, or a founder announces before YC does.

[**Website**](https://tryfoxy.vercel.app) · [Manifest](https://tryfoxy.vercel.app/manifest) · [Health](https://tryfoxy.vercel.app/healthz)

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Slack](https://img.shields.io/badge/Slack-app-4A154B?logo=slack&logoColor=white)
![Pond Protocol](https://img.shields.io/badge/Pond%20Protocol-v1.0-E1590C)
![License](https://img.shields.io/badge/license-MIT-green)

</div>

---

## The problem

Everyone scraping YC's directory finds the same companies on the same day.

But **the directory runs late.** It currently lists **24** Fall 2026 companies against
a batch of several hundred. The founders who aren't listed yet are already posting
about it on X and LinkedIn.

Foxy reads those posts *first*, then checks the directory. If YC hasn't published
them, it says so, and that's the window where an outreach email still lands first.

---

## What an alert looks like

```
🔥 EARLY YC SIGNAL · founder announced before YC

  Company     Adalat AI            Source      X
  Status      Not in YC directory  Confidence  100%

  "Adalat AI is now backed by Y Combinator. We're the first nonprofit…"

  Original post · Website
  Detected Aug 31, 2026, 9:14 AM PT  |  not in YC directory (no similar names)
```

When YC later lists a company Foxy called early, it replies **in that same thread**:
*"now listed in the YC directory, 11 days after Foxy flagged it."*

---

## Getting started

There are two ways to run Foxy. Pick whichever suits you.

### Hosted: click, choose a channel, done

**[Add to Slack](https://tryfoxy.vercel.app)** &rarr; approve &rarr; pick a channel from a
dropdown. That is the whole setup. Nothing to install, nothing to run, no terminal.

**Your first alerts arrive on that page, not eight hours later.** Foxy reads the two
YC feeds while the confirmation page is open — about half a minute — and tells you
what landed: *"12 alerts just landed in your channel."* An empty channel is
indistinguishable from a broken bot, and it is the first thing anyone sees.

After that, each source introduces itself with a handful of its newest entries the
first time it is read, then reports only what is genuinely new. A typical first day
is around twenty messages; a quiet day afterwards is none.

You land on a settings page where you can also paste an optional API key or change
the alert threshold. Keep that link if you want to adjust things later or stop alerts.

### Self-hosted: run it yourself

Clone it and let the wizard do the rest.

```bash
git clone https://github.com/ana-momin/Foxy.git
cd Foxy
pip install -r requirements.txt
python -m app.cli init
```

`init` checks your token, lists your channels so you never hunt for a channel ID,
joins the one you pick, sends a test message, and writes `.env`.

```
  Connected to Acme Inc as @Foxy

  Where should alerts go?
     1. #yc-signals   (already added)
     2. #general

  Enter 1-2: 1

  Sent, go and check Slack.
  Saved to .env. Setup is done.
```

Then start it:

```bash
docker compose up -d          # or: uvicorn app.main:app --port 8000
```

<details>
<summary>Configuring by hand instead</summary>

```bash
cp .env.example .env
```

```env
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_TARGET=C0ABC123DEF
```

`python -m app.cli channels` prints every channel with its ID.
</details>

Verify it remembers:

```bash
python -m app.cli sweep --dry    # run once, do not post
python -m app.cli sweep --dry    # second run should say "0 new"
```

That **0 new** is the persistence working. Foxy never alerts on the same company twice.

---

## Hosting it for others

Foxy can serve several Slack workspaces from one deployment. The five sources return
the same public data for everyone, so a sweep fetches them **once** and fans the
results out. A second workspace costs one extra `chat.postMessage`, not another set
of scrapes.

Turn it on by pointing `DATABASE_URL` at a real database. It stays off on SQLite,
which on serverless is a filesystem that vanishes between requests.

```env
DATABASE_URL=postgresql+psycopg://...   # neon.tech and supabase.com are free
ENCRYPTION_KEY=...                      # encrypts stored Slack tokens at rest
SWEEP_KEY=...                           # the scheduler presents this
SLACK_CLIENT_ID=...                     # from Basic Information in your Slack app
SLACK_CLIENT_SECRET=...
PUBLIC_BASE_URL=https://your-domain
```

Generate the two secrets with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add `<your-domain>/slack/oauth/callback` to **OAuth &amp; Permissions &rarr; Redirect URLs**
in your Slack app, then set `FOXY_BASE_URL` and `SWEEP_KEY` as repository secrets so
[`hosted-sweep.yml`](.github/workflows/hosted-sweep.yml) can drive the schedule.

> **The web app and the scheduler are two environments, and they must hold the same
> `ENCRYPTION_KEY`.** The web app writes Slack tokens; the scheduler reads them. When
> the two values differ, every token decrypts to nothing, the sweep finds no usable
> client, and it takes the dry-run path — recording alerts, charging quota, and
> sending nothing. Set it in both places from one generated value, and set
> `SERPER_API_KEY` and `ANTHROPIC_API_KEY` as repository secrets too: a key that lives
> only in your local `.env` is a key the scheduler does not have.
>
> Every stored token carries a fingerprint of the key that wrote it, so a mismatch is
> reported rather than guessed at. `python -m app.cli hosted-doctor` checks the whole
> chain against Slack itself, and `/healthz` names any workspace whose token this
> deployment cannot read.

**What is kept per workspace, and why**

| | |
|---|---|
| Seen-set | A shared one would mean the second workspace to install is told about nothing, because the first already consumed every company |
| API keys | So one workspace's spend never lands on another's |
| Alert threshold | Different people want different noise levels |

**Tokens are encrypted at rest** with an HMAC-SHA256 keystream, a fresh nonce per
value and an authentication tag. That is not a substitute for keeping the database
private, but a leaked dump is not immediately a set of live Slack credentials.

Re-installing a workspace updates it in place rather than creating a duplicate that
would double every alert. Note that Slack will not add a newly requested scope to a
token it has already issued, so a workspace installed before a scope change has to
reinstall to pick it up.

**An item counts as seen only once Slack has acknowledged it.** Marking it when the
alert is *decided* rather than *delivered* means an outage does not delay alerts, it
deletes them: the company is recorded as reported and never reconsidered. Anything
Slack does not acknowledge is forgotten at the end of the sweep and offered again on
the next one.

---

## Sources

| Source | What it catches | Cost |
|---|---|---|
| **YC Directory** | Every newly listed company, newest first | Free |
| **Launch YC** | Founder launch posts, often ahead of the directory | Free |
| **Speedrun** | All 251 a16z companies, cohorts SR001–SR007 | Free |
| **X** | Founder announcement posts | Free / better with a key |
| **LinkedIn** | Public launch posts and new company pages | Free / better with a key |

### A note on Speedrun

The brief this was built for asked for *"YC's Speedrun directory."* **Y Combinator does
not run a programme called Speedrun.** Verified three ways on 2026-08-29:

- `ycombinator.com/speedrun` → **404**
- YC's `sitemap.xml` → **zero** occurrences
- The directory's batch facet lists **50 batches** → no Speedrun

Speedrun is **a16z's** accelerator. Foxy monitors the real one at `speedrun.a16z.com`,
and a lightweight watcher checks YC's sitemap every sweep in case that ever changes.

---

## How detection works

```
  founder post
       │
       ▼
  ① keyword pack ──── "got into YC", batch codes auto-generated from today
       │
       ▼
  ② hard vetoes ───── "congrats to", "how I got into YC", "I invested early"
       │
       ▼
  ③ rule scoring ──── phrase weight + batch + first-person voice  (free)
       │
       ▼
  ④ LLM classify ──── optional; catches phrasings no regex will
       │
       ▼
  ⑤ CROSS-REFERENCE ─ against the directory that governs the programme
       │
   ┌───┴────┬──────────────┐
   ▼        ▼              ▼
not listed  listed    no company found
   │        │              │
 EARLY 🔥  confirmed ✅   unverified ⚠️
                        (routed to digest)
```

**First-person voice is the strongest signal.** A post with no *I* / *we* / *our*, and
no announcement opener, is someone reporting on another company, so it is rejected
outright rather than scored.

**"Unverified" is a real third state.** If Foxy cannot identify a company, it does *not*
claim the company is missing from YC, it doesn't know. Conflating *"I couldn't check"*
with *"it isn't there"* is how a monitor starts crying wolf.

### Accuracy

Measured against **15 real X posts** collected live and labelled by hand, 8 genuine
founder announcements, 7 lookalikes.

| Metric | Result |
|---|---|
| Precision | **100%** |
| Recall | **88%** |
| False positives | **0** |

Every lookalike contains a textbook announcement phrase and would fool a keyword search:

> *"8 startups I referred got into YC"*, a referrer
> *"The guy behind the $Rosie coin just got accepted"*, third party
> *"exactly one year ago today, we got into Y Combinator"*, anniversary
> *"I invested early into a startup that was recently accepted"*, investor

---

## Configuration

Only the Slack pair is required. Everything else widens coverage.

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` |, | **required** |
| `SLACK_TARGET` |, | **required**, channel ID or user ID for DMs |
| `DATABASE_URL` | local SQLite | set to Postgres on ephemeral hosts |
| `SCAN_INTERVAL_HOURS` | `8` | sweep cadence |
| `MIN_CONFIDENCE` | `0.55` | alert threshold; below this goes to the digest |
| `BACKFILL_DAYS` | `7` | first-run quiet window |
| `DRY_RUN` | `0` | log alerts instead of posting |
| `SERPER_API_KEY` |, | Google-quality search, **recommended**, 2,500 free |
| `ANTHROPIC_API_KEY` |, | enables the LLM classifier |
| `X_PROVIDER` | `free` | `free` / `twitterapi` / `none` |
| `LINKEDIN_PROVIDER` | `free` | `free` / `apify` / `none` |
| `POND_ACCESS_KEY` |, | Pond agent auth |
| `PUBLIC_BASE_URL` |, | your public HTTPS URL |

Keywords, vetoes, batch codes and thresholds live in
[`config.yaml`](config.yaml), adding a phrase is a config edit, never a code change.

> **On free X/LinkedIn coverage.** X removed its free tier in February 2026 and full
> archive search sits behind a $42k/mo contract. Foxy's free mode discovers posts
> through search engines, which works but is rate-limited and partial. A free
> [serper.dev](https://serper.dev) key (2,500 queries, no card) fixes this entirely and
> is the single highest-value addition.

---

## Commands

| Command | What it does |
|---|---|
| `python -m app.cli init` | **Interactive setup, start here** |
| `python -m app.cli channels` | Lists your channels and their IDs |
| `python -m app.cli check` | Shows what is configured and what is missing |
| `python -m app.cli sweep` | Runs one sweep now |
| `python -m app.cli sweep --dry` | Same, without posting to Slack |
| `python -m app.cli test-alert` | Posts a sample alert, verifies Slack |
| `python -m app.cli check-post <url>` | Runs one X post through the full pipeline |
| `python -m app.cli status` | Per-source health |
| `python -m app.cli reset --yes` | Clears memory |

Hosted deployments have three more:

| Command | What it does |
|---|---|
| `python -m app.cli hosted-sweep` | One shared fetch, delivered to every workspace |
| `python -m app.cli hosted-doctor` | Checks each workspace against Slack: token decrypts, token is live, channel exists, bot is a member. `--post` sends a probe and reads it back out of `conversations.history` |
| `python -m app.cli hosted-repair` | Parks workspaces whose token this environment cannot read, drops alert rows carrying no Slack message id, and recounts quota from what Slack acknowledged |
| `python -m app.cli audit-early [--fix]` | Re-checks stored early signals against the founder-announcement rule and demotes the ones that fail |

The doctor exists because every hosted delivery failure so far was invisible from our
own side: the sweep reported alerts, the database agreed, and the channel was empty.
It asks Slack instead.

In Slack: `/foxy status` and `/foxy scan`.

---

## Running persistently

| Option | Notes |
|---|---|
| **Docker** | `docker compose up -d`. Restarts itself, keeps state on a volume. |
| **GitHub Actions** | [`monitor.yml`](.github/workflows/monitor.yml) runs every 8h free. **Requires `DATABASE_URL`**, since each run starts on a clean machine. |
| **Hosted** | [`hosted-sweep.yml`](.github/workflows/hosted-sweep.yml) drives `/internal/sweep` for every installed workspace. |
| **Vercel** | Hosts the Pond agent endpoints. Pair with Actions for the schedule. |

Free hosting in 2026, honestly: Render free services spin down after 15 minutes, Fly has
no permanent free tier, Railway is a one-time credit. For a genuinely always-on
container budget **$5–7/month**, or use Oracle Cloud's free ARM VM.

---

## Pond agent

Foxy implements **Pond Protocol V1** in the same process as the monitor, so Pond's
manifest revalidation doubles as the health check.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /manifest` | none *(per spec)* | discovery |
| `POST /runs` | `Bearer` + `X-Agent-Protocol-Version: 1.0` | execution |
| `GET /tasks/{id}` | same | polling for long scans |
| `GET /healthz` | none | uptime, and hosted-mode state |
| `GET /slack/install` | none | one-click install |
| `POST /internal/sweep` | `X-Sweep-Key` | the scheduled hosted sweep |

**Actions:** `scan_now` · `search_early_signals` · `lookup_company` ·
`recent_detections` · `health_check`

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"  # your Access Key
```

Set `POND_ACCESS_KEY` and `PUBLIC_BASE_URL`, deploy, then register at
[joinpond.ai/agent/create](https://joinpond.ai/agent/create).

Implemented per spec: idempotency via `Idempotency-Key`, cumulative `usage` on every
terminal response, and the full error-code table.

**How a long scan runs.** `scan_now` returns `202` with a task id, and the polls
themselves do the work. Each poll takes a lease, scans one source inside a time
budget that fits comfortably in a request, and writes what it finished to the
database. The next poll resumes on whichever instance answers it.

This matters on serverless, where the obvious approach does not survive contact:
a background task stops when the instance is frozen after responding, and an
in-memory task store is invisible to the next instance. A source is attempted at
most three times before it is written off, so a scan always terminates.

**Scope and validation.** `sources` is honoured exactly. With no scope given, only
the fast feeds are read, because a default scan should answer promptly rather than
spend minutes in paced search engines. Parameters are validated against the
schemas the manifest advertises, so an unknown field, a value outside an enum or a
wrong type comes back as `422` naming the field.

**Before submitting**, drive the deployment the way Pond does:

```bash
python tools/pond_conformance.py --base https://your-domain --key "$POND_ACCESS_KEY"
```

Fifteen checks - manifest against the published schema, every action, the async
scan through its polls, idempotency replay, bad input, and whether the early
signals are actually founder announcements. It exits non-zero, so it can gate a
release.

**Latency note.** Pin the deployment region to the one your database is in
(`regions` in `vercel.json`). Compute in Virginia and Postgres in Singapore put a
Pacific crossing on every round trip, which was most of a three-second health
check.

---

## Architecture

```
app/
├── main.py           FastAPI: Pond agent, scheduler and the site, one process
├── engine.py         the sweep: fetch, dedupe, verify, alert
├── classify.py       rules plus an optional LLM pass
├── crossref.py       "does the official directory know this company?"
├── slack.py          Block Kit alerts, channel listing
├── db.py             persistent state (SQLite or Postgres)
├── cli.py            command line entry point
├── setup_wizard.py   `init`: the guided self-hosted setup
│
├── oauth.py          one-click Slack install
├── installs.py       hosted mode: workspaces, encrypted tokens
├── dashboard.py      hosted mode: the settings page
├── hosted.py         hosted mode: fetch once, deliver to every workspace
│
├── sources/          one file per monitored surface
│   ├── yc_directory.py   ├── yc_launches.py   ├── speedrun.py
│   └── x_social.py       └── linkedin_social.py
└── providers/        swappable implementations per source
    ├── websearch.py      ├── x_provider.py    └── linkedin_provider.py
```

### Adding a platform

Every source implements one interface. To add Bluesky:

1. Create `app/sources/bluesky.py` with a class exposing `fetch() -> list[Signal]`
2. Register it in `build_sources()` in [`app/engine.py`](app/engine.py)
3. Add its queries to `config.yaml`

Dedupe, scoring, cross-reference, Slack formatting, health tracking and the Pond
actions all work automatically, nothing downstream knows where a signal came from.

### Design decisions worth knowing

**Credentials are never hardcoded.** YC rotates its public Algolia key, the one copied
into older open-source projects is already dead (403). Foxy re-reads it from the live
page every run, so rotation heals itself. Same for YC's Inertia version hash and a16z's
Next.js build ID.

**One failing source never aborts a sweep.** Each is isolated and its health recorded.
If a source fails twice running, Foxy **posts to Slack about itself**, silent failure
is the only unrecoverable bug in a monitoring product.

**The first run does not spam.** Without a guard, day one would fire hundreds of
backfill alerts. `BACKFILL_DAYS` controls the quiet window.

---

## Legal

Public data only. No authenticated scraping, no login automation, no cookie reuse, no
captcha solving, no republication. Alerts link back to the original source so founders
get the traffic and the credit.

LinkedIn sued Proxycurl out of existence in July 2026, Foxy never logs in, and its
LinkedIn providers read only public, already-indexed material.

If you operate in the EU, treat post authors as data subjects under GDPR.

---

## License

[MIT](LICENSE)
