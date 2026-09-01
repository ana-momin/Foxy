"""One-click Slack install.

The two things people got stuck on were copying a bot token out of a dashboard
and finding a channel ID like C0ABC123DEF. This removes both: the user clicks
"Add to Slack", approves, picks a channel from a list, and is handed a single
command that clones Foxy, configures it and starts it.

A deliberate design decision: **the token is never stored.** It is shown once,
on the user's own screen, and then forgotten. Foxy is a personal, self-hosted
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
import json
import logging
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import installs
from .config import settings
from .db import session
from .slack import SlackClient
from .sources.base import client as http

log = logging.getLogger("foxy.oauth")

router = APIRouter()

REPO = "https://github.com/ana-momin/Foxy.git"

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
# page chrome
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#FFFDFB;--surface:#fff;--ink:#16120E;--ink2:#5B5147;--muted:#948779;
--border:#EFE8E0;--border2:#E3DACE;--accent:#E1590C;--accent2:#FF7A38;--sf:#FFF2E9;
--good:#4E7A16;--goodsf:#F2F8E2;
--sh:0 1px 3px rgba(30,20,10,.05),0 8px 24px -12px rgba(30,20,10,.14)}
@media(prefers-color-scheme:dark){:root{--bg:#100C09;--surface:#191411;--ink:#F6F2ED;
--ink2:#B3A89C;--muted:#7E7266;--border:#2A231D;--border2:#362D25;--accent:#FF7E3C;
--accent2:#FF9560;--sf:#2A1509;--good:#A8CF63;--goodsf:#1B2410;
--sh:0 8px 24px -12px rgba(0,0,0,.7)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:"Schibsted Grotesk",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased}
.w{max-width:600px;margin:0 auto;padding:56px 24px 90px}
h1{font-size:31px;letter-spacing:-.032em;margin:0 0 10px;font-weight:600;line-height:1.1}
.lede{color:var(--ink2);margin:0 0 34px;font-size:16.5px}
.ok{display:inline-flex;align-items:center;gap:7px;background:var(--goodsf);color:var(--good);
border-radius:999px;padding:5px 13px;font-size:12.5px;font-weight:500;margin-bottom:22px}

.step{display:grid;grid-template-columns:28px 1fr;gap:16px;margin-bottom:30px}
.sn{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;
background:var(--accent);color:#fff;font-size:12px;font-weight:600;
font-family:"JetBrains Mono",monospace}
.sh{font-size:16px;font-weight:600;margin:2px 0 12px}
.sb{min-width:0}

select,.manual{width:100%;padding:12px 13px;border-radius:10px;
border:1px solid var(--border2);background:var(--surface);color:var(--ink);
font-family:inherit;font-size:15px;box-shadow:var(--sh)}
select{appearance:none;
background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),
linear-gradient(135deg,var(--muted) 50%,transparent 50%);
background-position:calc(100% - 19px) 21px,calc(100% - 14px) 21px;
background-size:5px 5px,5px 5px;background-repeat:no-repeat}
select:focus,.manual:focus{outline:2px solid var(--accent);outline-offset:2px}
.manual{font-family:"JetBrains Mono",monospace;font-size:14px}

.tabs{display:inline-flex;gap:3px;padding:3px;margin:0 0 10px;background:var(--surface);
border:1px solid var(--border);border-radius:10px}
.tabb{border:0;background:transparent;cursor:pointer;color:var(--ink2);font-family:inherit;
font-size:13px;font-weight:500;padding:7px 14px;border-radius:7px}
.tabb:hover{color:var(--ink)}
.tabb.on{background:var(--bg);color:var(--ink);box-shadow:var(--sh)}

.term{border-radius:12px;overflow:hidden;border:1px solid var(--border);
box-shadow:var(--sh);background:#14100C}
.term-bar{display:flex;align-items:center;gap:7px;padding:10px 13px;
background:rgba(255,255,255,.04);border-bottom:1px solid rgba(255,255,255,.07)}
.tl-d{width:9px;height:9px;border-radius:50%;background:rgba(255,255,255,.16)}
.term-t{margin-left:6px;font-family:"JetBrains Mono",monospace;font-size:10.5px;
letter-spacing:.06em;color:#8A7E71;flex:1}
.copy{background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.17);
color:#CFC6BA;border-radius:6px;padding:4px 10px;font-size:9.5px;letter-spacing:.09em;
text-transform:uppercase;cursor:pointer;font-family:"JetBrains Mono",monospace}
.copy:hover{background:rgba(255,255,255,.18);color:#fff}
.copy.done{background:var(--good);border-color:var(--good);color:#fff}
pre{margin:0;background:transparent;color:#EFE7DC;padding:16px 17px;
font-family:"JetBrains Mono",ui-monospace,monospace;font-size:12.5px;line-height:1.9;
white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
pre .c{color:#7E7266}

.does{margin:14px 0 0;padding:0;list-style:none;font-size:13.5px;color:var(--muted)}
.does li{padding-left:18px;position:relative;margin-bottom:3px}
.does li:before{content:"\\2192";position:absolute;left:0;color:var(--accent)}

.warn{border-left:3px solid var(--accent);background:var(--sf);padding:15px 18px;
border-radius:0 10px 10px 0;font-size:14px;color:var(--ink2);margin:32px 0 20px}
.warn b{color:var(--ink)}
.muted{color:var(--muted);font-size:13.5px}
a{color:var(--accent)}
code{font-family:"JetBrains Mono",monospace;font-size:.87em;background:var(--surface);
border:1px solid var(--border);padding:1px 5px;border-radius:5px}
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} &middot; Foxy</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>%F0%9F%A6%8A</text></svg>">
<style>{_CSS}</style></head><body><div class="w">{body}</div></body></html>"""
    )


def _err(message: str, detail: str = "") -> HTMLResponse:
    extra = f'<p class="muted">{html.escape(detail)}</p>' if detail else ""
    return _page(
        "Something went wrong",
        f"""<h1>That didn't work</h1><p class="lede">{html.escape(message)}</p>{extra}
        <p><a href="/slack/install">Try again</a> &middot; <a href="/">Back to Foxy</a></p>""",
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


def _build_page(token: str, team: str, options: str) -> HTMLResponse:
    """Render the success page. Split out so it can be tested without Slack."""
    if options:
        chooser = f'<select id="ch">{options}</select>'
    else:
        chooser = (
            '<p class="muted" style="margin:0 0 10px">Right-click a channel in Slack '
            "&rsaquo; View channel details &rsaquo; the ID is at the bottom.</p>"
            '<select id="ch" hidden></select>'
            '<input class="manual" id="chman" placeholder="C0ABC123DEF">'
        )

    # Values reach the script as a JSON literal rather than being concatenated
    # into JS source, so a stray quote in a token can never break the page.
    boot = json.dumps({"token": token, "repo": REPO})

    body = f"""
<div class="ok">&#10003; Added to {html.escape(team)}</div>
<h1>One command left</h1>
<p class="lede">Choose where alerts should go, then paste the command into a
terminal. It sets up everything for you.</p>

<div class="step">
  <div class="sn">1</div>
  <div class="sb">
    <div class="sh">Choose a channel</div>
    {chooser}
  </div>
</div>

<div class="step">
  <div class="sn">2</div>
  <div class="sb">
    <div class="sh">Paste this into a terminal</div>
    <div class="tabs">
      <button class="tabb on" data-os="unix" type="button">macOS &middot; Linux</button>
      <button class="tabb" data-os="win" type="button">Windows</button>
    </div>
    <div class="term" data-os-panel="unix">
      <div class="term-bar">
        <span class="tl-d"></span><span class="tl-d"></span><span class="tl-d"></span>
        <span class="term-t">Terminal</span>
        <button class="copy" type="button">Copy</button>
      </div>
      <pre id="cmd-unix"></pre>
    </div>
    <div class="term" data-os-panel="win" hidden>
      <div class="term-bar">
        <span class="tl-d"></span><span class="tl-d"></span><span class="tl-d"></span>
        <span class="term-t">PowerShell</span>
        <button class="copy" type="button">Copy</button>
      </div>
      <pre id="cmd-win"></pre>
    </div>
    <ul class="does">
      <li>Downloads Foxy</li>
      <li>Saves your token and channel</li>
      <li>Starts it, checking every 8 hours</li>
    </ul>
  </div>
</div>

<div class="warn">
  <b>Your token is inside that command.</b> It is shown once and is not stored on
  this server. Foxy runs on your machine, not ours. Keep it private; if you
  lose it, <a href="/slack/install">reinstall</a> for a new one.
</div>

<p class="muted">Needs <a href="https://www.docker.com/products/docker-desktop/">Docker</a>,
a normal app you install once. Prefer step by step? See the
<a href="/#/setup">setup guide</a>.</p>

<script id="boot" type="application/json">{boot}</script>
<script>
(function () {{
  var cfg = JSON.parse(document.getElementById("boot").textContent);
  var NL = String.fromCharCode(92) + "n";      // a literal \\n for printf
  var CONT = " " + String.fromCharCode(92);    // trailing \\ to continue a line

  function channel() {{
    var man = document.getElementById("chman");
    if (man && man.value.trim()) return man.value.trim();
    var sel = document.getElementById("ch");
    if (sel && sel.value) return sel.value;
    return "YOUR_CHANNEL_ID";
  }}

  function render() {{
    var ch = channel();
    document.getElementById("cmd-unix").textContent =
      "git clone " + cfg.repo + " && cd Foxy &&" + CONT + "\\n" +
      "printf 'SLACK_BOT_TOKEN=" + cfg.token + NL + "SLACK_TARGET=" + ch + NL + "' > .env &&" + CONT + "\\n" +
      "docker compose up -d";
    document.getElementById("cmd-win").textContent =
      "git clone " + cfg.repo + "; cd Foxy; " +
      "Set-Content .env -Encoding ascii -Value \\"SLACK_BOT_TOKEN=" + cfg.token +
      "`nSLACK_TARGET=" + ch + "\\"; docker compose up -d";
  }}

  var sel = document.getElementById("ch");
  if (sel) sel.addEventListener("change", render);
  var man = document.getElementById("chman");
  if (man) man.addEventListener("input", render);

  document.querySelectorAll(".tabb").forEach(function (b) {{
    b.addEventListener("click", function () {{
      document.querySelectorAll(".tabb").forEach(function (x) {{ x.classList.remove("on"); }});
      b.classList.add("on");
      document.querySelectorAll("[data-os-panel]").forEach(function (p) {{
        p.hidden = p.dataset.osPanel !== b.dataset.os;
      }});
    }});
  }});

  document.querySelectorAll(".copy").forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      var pre = btn.closest(".term").querySelector("pre");
      var done = function () {{
        btn.textContent = "Copied";
        btn.classList.add("done");
        setTimeout(function () {{
          btn.textContent = "Copy";
          btn.classList.remove("done");
        }}, 1600);
      }};
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(pre.textContent).then(done, function () {{}});
      }} else {{
        var ta = document.createElement("textarea");
        ta.value = pre.textContent;
        document.body.appendChild(ta);
        ta.select();
        try {{ document.execCommand("copy"); done(); }} catch (e) {{}}
        document.body.removeChild(ta);
      }}
    }});
  }});

  render();
}})();
</script>
"""
    return _page("Installed", body)


@router.get("/slack/oauth/callback", response_model=None)
def callback(request: Request) -> HTMLResponse:
    """Exchange the code for a token and hand back one runnable command."""
    params = request.query_params

    if params.get("error"):
        return _err(
            "The install was cancelled in Slack.",
            "Nothing was changed in your workspace.",
        )

    code = params.get("code") or ""
    if not code:
        return _err("Slack did not send an authorisation code.")
    if not _state_ok(params.get("state") or ""):
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
    team_obj = data.get("team") or {}
    team = team_obj.get("name", "your workspace")
    team_id = team_obj.get("id", "")

    # Hosted mode: keep the install and let them finish in the browser. There is
    # nothing for them to run, so there is no command to show.
    if installs.hosted_enabled() and team_id:
        # Retry once. The first attempt after an idle period can still meet a
        # connection the database closed while this function was frozen, and
        # dropping someone into the self-hosted flow because of a transient
        # socket is a bad way to greet a new install.
        for attempt in (1, 2):
            try:
                with session() as s:
                    row = installs.upsert(
                        s, team_id=team_id, team_name=team, token=token
                    )
                    install_id = row.id
                return RedirectResponse(f"/app/{install_id}", 302)
            except Exception:  # noqa: BLE001
                log.warning("install attempt %d failed", attempt, exc_info=True)
                if attempt == 1:
                    time.sleep(0.4)
        log.error("could not record the install; showing the manual flow")

    options = ""
    try:
        for ch in sorted(
            SlackClient(token=token, target="").list_channels(),
            key=lambda c: (not c.get("is_member"), c.get("name", "")),
        )[:60]:
            sym = "\U0001f512 " if ch.get("is_private") else "#"
            here = "  — already added" if ch.get("is_member") else ""
            options += (
                f'<option value="{html.escape(ch["id"])}">'
                f'{sym}{html.escape(ch.get("name", ""))}{here}</option>'
            )
    except Exception:  # noqa: BLE001
        options = ""

    return _build_page(token, team, options)
