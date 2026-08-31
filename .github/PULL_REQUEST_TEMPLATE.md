## What this changes

<!-- One or two sentences. -->

## Why

<!-- What was wrong before? -->

## If this touches detection

Run a few real posts through the pipeline and paste the verdicts:

```
python -m app.cli check-post <url>
```

- Posts it should now catch:
- Lookalikes it should still reject:

## Checklist

- [ ] `python -m app.cli check` still passes
- [ ] `python -m app.cli sweep --dry` runs twice, second reports `0 new`
- [ ] No credential is hardcoded (scraped keys are re-read from the live page)
- [ ] A failing source degrades instead of aborting the sweep
- [ ] Public data only — no logins, no cookies
