# Contributing to Foxy

Thanks for taking a look. Foxy is small and deliberately readable — most changes
should be a single file.

## Getting set up

```bash
git clone https://github.com/ana-momin/Foxy.git
cd Foxy
pip install -r requirements.txt
cp .env.example .env          # only SLACK_* is required
python -m app.cli check       # tells you what is and isn't configured
```

You can develop without a Slack token — every command accepts `--dry`, and
`DRY_RUN=1` logs alerts instead of posting them.

## Adding a source

This is the most likely contribution, and it is intentionally easy.

1. Create `app/sources/<platform>.py` with a class that subclasses `Source` and
   implements `fetch() -> list[Signal]`.
2. Register it in `build_sources()` in `app/engine.py`.
3. Add its queries to `config.yaml`.

Nothing downstream needs to change. Dedupe, scoring, cross-reference, Slack
formatting, health tracking and the Pond actions all work off `Signal`.

If your source needs a paid API, put it behind a provider `Protocol` in
`app/providers/` with a free default, following `x_provider.py`. A missing key
must degrade to the free path, never raise.

## House rules

**Never claim more than you checked.** If a company cannot be identified, the
result is `unknown`, not "not in the directory". Treating "I couldn't check" as
"it isn't there" is how a monitor starts crying wolf.

**Never hardcode a scraped credential.** YC rotates its public Algolia key and
its Inertia version; a16z rotates its build ID. Re-read them from the live page
each run.

**One source failing must never abort a sweep.** Catch, record health, continue.

**Public data only.** No logins, no cookies, no captcha solving. If a change
requires authenticating as a user, it does not belong here.

## Testing a change to detection

The classifier is the easiest thing to break. Before opening a PR:

```bash
python -m app.cli check-post https://x.com/<user>/status/<id>
```

That runs one real post through the whole pipeline — classify, extract,
cross-reference — and prints the verdict without posting anything.

Please include, in the PR description, a few real posts your change should
catch and a few lookalikes it should reject.

## Commits

Plain descriptive messages. Say what changed and why it was wrong before.
