# Security

## Reporting

Found something? Open a [private security advisory](https://github.com/ana-momin/Foxy/security/advisories/new)
rather than a public issue.

## Handling secrets

Foxy reads every credential from the environment. Nothing is committed.

- `.env` is gitignored — keep it that way.
- `SLACK_BOT_TOKEN` can post as your bot to any channel it is in. Treat it as a
  password; rotate it in the Slack app dashboard if it is ever exposed.
- `POND_ACCESS_KEY` guards `/runs` and `/tasks`. Generate it with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `GET /manifest` is intentionally public and unauthenticated, as Pond Protocol
  V1 requires. It contains no secrets — never add one to it.

## What Foxy accesses

Public, unauthenticated pages only. It does not log in anywhere, use session
cookies, solve captchas, or read private data. The credentials it scrapes from
YC and a16z are the public client-side search keys those sites hand to every
visitor's browser.
