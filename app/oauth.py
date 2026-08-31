"""One-click Slack install.

The hard parts of setup were always the same two: copying a bot token out of a
dashboard, and finding a channel ID like C0ABC123DEF. This removes both. The
user clicks "Add to Slack", approves, picks a channel from a list, and is handed
the two lines to paste into their `.env`.

A deliberate design decision: **the token is never stored.** It is shown once, on
the user's own screen, and then forgotten. Foxy is a personal, self-hosted
monitor - keeping other people's workspace tokens on this server would make it
something else entirely, and a much bigger liability.

The OAuth `state` is an HMAC signed with the client secret rather than a
server-side session, because this runs on serverless where no memory is shared
between requests.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import settings
from .slack import SlackClient
from .sources.base import client as http

router = APIRouter()

# Matches slack-app-manifest.json. Slack grants exactly what is asked for here.
SCOPES = [
    "chat:write",
    "chat:write.public",
    "im:write",
    "commands",
    "links:write",
    "channels:read",
    "groups:read",
]

STATE_TTL_SECONDS = 600


# ---------------------------------------------------------------------------
# state signing
# ---------------------------------------------------------------------------


def _sign(payload: str) -> str:
    return hmac.new(
        settings.slack_client_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]


def _make_state() -> str:
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def _state_ok(state: str) -> bool:
    try:
        issued, sig = state.split(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(sig, _sign(issued)):
        return False
    return (time.time() - int(issued)) < STATE_TTL_SECONDS


# ---------------------------------------------------------------------------
# page chrome - matches the site, no external assets
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#FFFDFB;--surface:#fff;--ink:#16120E;--ink2:#5B5147;--muted:#948779;
--border:#EFE8E0;--border2:#E3DACE;--accent:#E1590C;--accent2:#FF7A38;--sf:#FFF2E9;
--good:#4E7A16;--goodsf:#F2F8E2;--sh:0 1px 3px rgba(30,20,10,.05),0 8px 24px -12px rgba(30,20,10,.14)}
@media(prefers-color-scheme:dark){:root{--bg:#100C09;--surface:#191411;--ink:#F6F2ED;
--ink2:#B3A89C;--muted:#7E7266;--border:#2A231D;--border2:#362D25;--accent:#FF7E3C;
--accent2:#FF9560;--sf:#2A1509;--good:#A8CF63;--goodsf:#1B2410;--sh:0 8px 24px -12px rgba(0,0,0,.7)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:"Schibsted Grotesk",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
font-size:16px;line-height:1.65;-webkit-font-smoothing:antialiased}
.w{max-width:560px;margin:0 auto;padding:64px 24px 80px}
h1{font-size:30px;letter-spacing:-.03em;margin:0 0 12px;font-weight:600}
h2{font-size:15px;font-weight:600;margin:0 0 10px}
p{color:var(--ink2);margin:0 0 18px}
.mark{width:56px;height:56px;border-radius:15px;margin-bottom:26px;display:block}
.card{background:var(--surface);border:1px solid var(--border);border-radius:13px;padding:22px;box-shadow:var(--sh);margin-bottom:16px}
.btn{display:inline-flex;align-items:center;gap:9px;background:var(--accent);color:#fff;
border:0;border-radius:10px;padding:13px 22px;font-size:15px;font-weight:500;cursor:pointer;
text-decoration:none;font-family:inherit}
.btn:hover{background:var(--accent2)}
.ok{display:inline-flex;align-items:center;gap:7px;background:var(--goodsf);color:var(--good);
border-radius:999px;padding:5px 12px;font-size:12.5px;margin-bottom:20px}
code,pre{font-family:"JetBrains Mono",ui-monospace,monospace}
pre{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:15px 17px;font-size:12.5px;line-height:1.8;overflow-x:auto;margin:0;position:relative}
.copy{position:absolute;top:9px;right:9px;background:var(--bg);border:1px solid var(--border2);
color:var(--muted);border-radius:6px;padding:4px 9px;font-size:9.5px;letter-spacing:.09em;
text-transform:uppercase;cursor:pointer;font-family:"JetBrains Mono",monospace}
.copy:hover{color:var(--accent);border-color:var(--accent)}
.wrap{position:relative}
select{width:100%;padding:11px 13px;border-radius:9px;border:1px solid var(--border2);
background:var(--surface);color:var(--ink);font-family:inherit;font-size:15px;margin-bottom:14px}
.warn{border-left:3px solid var(--accent);background:var(--sf);padding:14px 18px;
border-radius:0 10px 10px 0;font-size:14px;color:var(--ink2);margin-bottom:18px}
.muted{color:var(--muted);font-size:13.5px}
a{color:var(--accent)}
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — Foxy</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>%F0%9F%A6%8A</text></svg>">
<style>{_CSS}</style></head><body><div class="w">{body}</div>
<script>
document.querySelectorAll(".copy").forEach(function(b){{
  b.addEventListener("click",function(){{
    var t=b.parentElement.querySelector("pre").innerText;
    navigator.clipboard.writeText(t).then(function(){{
      b.textContent="Copied";setTimeout(function(){{b.textContent="Copy";}},1500);
    }});
  }});
}});
</script></body></html>""",
        status_code=200,
    )


def _err(message: str, detail: str = "") -> HTMLResponse:
    extra = f'<p class="muted">{html.escape(detail)}</p>' if detail else ""
    return _page(
        "Something went wrong",
        f"""<h1>That didn't work</h1><p>{html.escape(message)}</p>{extra}
        <p><a href="/slack/install">Try again</a> · <a href="/">Back to Foxy</a></p>""",
    )


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.get("/slack/install", response_model=None)
def install() -> RedirectResponse | HTMLResponse:
    """Send the user to Slack to approve the app."""
    if not settings.slack_client_id or not settings.slack_client_secret:
        return _err(
            "One-click install is not configured on this server.",
            "Set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET, or install manually "
            "with `python -m app.cli init`.",
        )

    query = urlencode(
        {
            "client_id": settings.slack_client_id,
            "scope": ",".join(SCOPES),
            "redirect_uri": f"{settings.public_base_url}/slack/oauth/callback",
            "state": _make_state(),
        }
    )
    return RedirectResponse(f"https://slack.com/oauth/v2/authorize?{query}", 302)


@router.get("/slack/oauth/callback", response_model=None)
def callback(request: Request) -> HTMLResponse:
    """Exchange the code for a token and let the user pick a channel."""
    params = request.query_params

    if params.get("error"):
        return _err(
            "The install was cancelled in Slack.",
            "Nothing was changed in your workspace.",
        )

    code = params.get("code") or ""
    state = params.get("state") or ""
    if not code:
        return _err("Slack did not send an authorisation code.")
    if not _state_ok(state):
        return _err(
            "That install link has expired.",
            "Links are valid for ten minutes, to stop anyone else reusing them.",
        )

    try:
        with http() as c:
            r = c.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": settings.slack_client_id,
                    "client_secret": settings.slack_client_secret,
                    "code": code,
                    "redirect_uri": f"{settings.public_base_url}/slack/oauth/callback",
                },
            )
            data = r.json()
    except Exception:  # noqa: BLE001
        return _err("Could not reach Slack to complete the install.")

    if not data.get("ok"):
        return _err("Slack rejected the install.", str(data.get("error", ""))[:120])

    token = data.get("access_token", "")
    team = (data.get("team") or {}).get("name", "your workspace")

    # List channels so nobody has to hunt for an ID.
    options = ""
    try:
        for ch in sorted(
            SlackClient(token=token, target="").list_channels(),
            key=lambda c: (not c.get("is_member"), c.get("name", "")),
        )[:60]:
            sym = "🔒" if ch.get("is_private") else "#"
            here = " — already added" if ch.get("is_member") else ""
            options += (
                f'<option value="{html.escape(ch["id"])}">'
                f'{sym}{html.escape(ch.get("name", ""))}{here}</option>'
            )
    except Exception:  # noqa: BLE001
        options = ""

    picker = (
        f"""<h2>2 · Choose a channel</h2>
        <select id="ch" onchange="upd()">{options}</select>"""
        if options
        else """<h2>2 · Your channel ID</h2>
        <p class="muted">Right-click a channel in Slack → View channel details →
        the ID is at the bottom.</p>
        <select id="ch" style="display:none"></select>"""
    )

    safe_token = html.escape(token)
    return _page(
        "Installed",
        f"""
<img class="mark" src="/static/foxy.png" alt="" onerror="this.style.display='none'">
<div class="ok">✓ Added to {html.escape(team)}</div>
<h1>Almost there</h1>
<p>Foxy is in your workspace. Two lines left — paste them into the
<code>.env</code> file where you run Foxy.</p>

<div class="card">
  {picker}
  <h2 style="margin-top:18px">3 · Paste this into <code>.env</code></h2>
  <div class="wrap">
    <button class="copy" type="button">Copy</button>
    <pre id="env">SLACK_BOT_TOKEN={safe_token}
SLACK_TARGET=</pre>
  </div>
</div>

<div class="warn">
  <b>This token is shown once and is not stored anywhere.</b> Foxy runs on your own
  machine, so this server keeps nothing. Copy it now — if you lose it, reinstall
  from <a href="/slack/install">here</a>.
</div>

<p class="muted">Then start it:<br>
<code>docker compose up -d</code> &nbsp;or&nbsp; <code>uvicorn app.main:app --port 8000</code></p>
<p><a href="/#/setup">Full setup guide</a></p>

<script>
var TOKEN = {safe_token!r};
function upd() {{
  var sel = document.getElementById("ch");
  var id = sel && sel.value ? sel.value : "";
  document.getElementById("env").textContent =
    "SLACK_BOT_TOKEN=" + TOKEN + "\\nSLACK_TARGET=" + id;
}}
upd();
</script>
""",
    )
