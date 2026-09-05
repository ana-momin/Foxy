"""The settings page a workspace lands on after installing in hosted mode.

One form: pick a channel, optionally paste keys, save. No terminal, no files,
nothing to run. The page is reachable only by its install ID, which is a
random 24-character token issued at install time and never listed anywhere.
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import installs
from .config import settings
from .db import session
from .oauth import _CSS, _err, _page
from .slack import SlackClient

log = logging.getLogger("foxy.dashboard")

router = APIRouter()

_EXTRA_CSS = """
.field{margin-bottom:20px}
.field label{display:block;font-size:14px;font-weight:600;margin-bottom:6px}
.field .hint{font-size:13px;color:var(--muted);margin:0 0 8px}
input[type=text],input[type=password]{width:100%;padding:11px 13px;border-radius:10px;
border:1px solid var(--border2);background:var(--surface);color:var(--ink);
font-family:"JetBrains Mono",monospace;font-size:13.5px}
input:focus{outline:2px solid var(--accent);outline-offset:2px}
.save{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:13px 24px;
font-size:15px;font-weight:500;cursor:pointer;font-family:inherit}
.save:hover{background:var(--accent2)}
.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:8px}
.opt{border-top:1px solid var(--border);margin-top:30px;padding-top:26px}
.opt h2{font-size:15px;font-weight:600;margin:0 0 6px}
details summary{cursor:pointer;font-size:14px;color:var(--ink2);margin-bottom:14px}

.next{background:var(--surface);border:1px solid var(--border);border-radius:13px;
padding:22px 24px;box-shadow:var(--sh);margin-bottom:26px}
.next h2{font-size:14px;font-weight:600;margin:0 0 12px;color:var(--muted);
letter-spacing:.06em;text-transform:uppercase;font-family:"JetBrains Mono",monospace}
.next ul{margin:0;padding:0;list-style:none}
.next li{position:relative;padding-left:22px;margin-bottom:9px;font-size:14.5px;
color:var(--ink2);line-height:1.55}
.next li:last-child{margin-bottom:0}
.next li:before{content:"";position:absolute;left:0;top:8px;width:6px;height:6px;
border-radius:50%;background:var(--accent)}
.next b{color:var(--ink);font-weight:600}

.links{display:grid;grid-template-columns:1fr;gap:10px}
@media(min-width:560px){.links{grid-template-columns:1fr 1fr}}
.lk{display:block;background:var(--surface);border:1px solid var(--border);
border-radius:12px;padding:16px 18px;text-decoration:none;color:inherit;
transition:border-color .18s ease,transform .18s ease,box-shadow .2s ease}
.lk:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:var(--sh)}
.lk b{display:block;font-size:15px;font-weight:600;margin-bottom:3px;color:var(--ink)}
.lk:hover b{color:var(--accent)}
.lk span{font-size:13px;color:var(--muted);line-height:1.45}

.usage{background:var(--surface);border:1px solid var(--border);border-radius:12px;
padding:16px 18px;margin-bottom:28px}
.usage-top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
flex-wrap:wrap;margin-bottom:10px}
.usage-top b{font-size:14.5px;font-weight:600}
.usage-top span{font-size:13px;color:var(--muted);font-family:"JetBrains Mono",monospace}
.bar{height:5px;border-radius:3px;background:var(--border);overflow:hidden}
.bar span{display:block;height:100%;background:var(--accent);border-radius:3px}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;
margin:26px 0 30px}
.plan{background:var(--surface);border:1px solid var(--border);border-radius:12px;
padding:20px;display:flex;flex-direction:column;gap:4px}
.plan.best{border-color:var(--accent)}
.plan b{font-size:13px;font-weight:600;letter-spacing:.02em;color:var(--muted)}
.plan .price{font-size:34px;font-weight:600;letter-spacing:-.02em;line-height:1.1}
.plan .per{font-size:13px;color:var(--muted)}
"""


def _shell(body: str, title: str = "Foxy") -> HTMLResponse:
    page = _page(title, body)
    # Slot the form styles in alongside the shared ones.
    return HTMLResponse(page.body.decode().replace("</style>", _EXTRA_CSS + "</style>"))


@router.get("/app/{install_id}", response_model=None)
def dashboard(install_id: str, saved: str = "") -> HTMLResponse:
    with session() as s:
        row = installs.get(s, install_id)
        if row is None or not row.active:
            return _err(
                "That settings link is not valid.",
                "Reinstall from the home page to get a new one.",
            )
        team = row.team_name
        token = row.token
        channel = row.channel_id
        has_serper = bool(row.serper_key_enc)
        has_anthropic = bool(row.anthropic_key_enc)
        conf = row.min_confidence or ""
        plan, used, quota = row.plan_label, row.alerts_used or 0, row.quota
        plan_active = row.plan_active

    options = ""
    try:
        for ch in sorted(
            SlackClient(token=token, target="").list_channels(),
            key=lambda c: (not c.get("is_member"), c.get("name", "")),
        )[:60]:
            sym = "\U0001f512 " if ch.get("is_private") else "#"
            sel = " selected" if ch["id"] == channel else ""
            here = "   (Foxy is already in this one)" if ch.get("is_member") else ""
            options += (
                f'<option value="{html.escape(ch["id"])}"{sel}>'
                f'{sym}{html.escape(ch.get("name", ""))}{here}</option>'
            )
    except Exception:  # noqa: BLE001
        options = ""

    if options:
        picker = f'<select name="channel_id" id="ch">{options}</select>'
    else:
        picker = (
            '<p class="hint">Could not list your channels. Right-click one in Slack '
            "&rsaquo; View channel details &rsaquo; copy the ID from the bottom.</p>"
            f'<input type="text" name="channel_id" value="{html.escape(channel)}" '
            'placeholder="C0ABC123DEF">'
        )

    banner = ""
    if saved == "1":
        banner = '<div class="ok">&#10003; Saved. Foxy is watching.</div>'
    elif saved == "test":
        banner = '<div class="ok">&#10003; Saved, and a test message is in your channel.</div>'

    ph = "•" * 12
    body = f"""
{banner}
<h1>Foxy settings</h1>
<p class="lede">Workspace: <b>{html.escape(team)}</b>. Nothing to install and nothing
to run. Choose a channel and Foxy starts watching.</p>

<div class="usage">
  <div class="usage-top">
    <b>{html.escape(plan)}</b>
    <span>{f"{used} of {quota} alerts used" if quota else f"{used} alerts sent"}</span>
  </div>
  {f'<div class="bar"><span style="width:{min(100, round(used * 100 / quota)) if quota else 0}%"></span></div>' if quota else ""}
  {"" if plan_active else f'<p class="hint"><a href="/app/{html.escape(install_id)}/upgrade">Remove the limit &rarr;</a></p>'}
</div>

<form method="post" action="/app/{html.escape(install_id)}/save">
  <div class="field">
    <label for="ch">Where should alerts go?</label>
    {picker}
  </div>

  <div class="row">
    <button class="save" type="submit" name="action" value="save">Save</button>
    <button class="save" type="submit" name="action" value="test"
            style="background:var(--surface);color:var(--ink);border:1px solid var(--border2)">
      Save and send a test
    </button>
  </div>

  <div class="opt">
    <details>
      <summary>Optional settings</summary>

      <div class="field">
        <label for="serper">Serper API key</label>
        <p class="hint">Free 2,500 searches at serper.dev. Without it, X and
        LinkedIn find noticeably less. The three YC sources are unaffected.</p>
        <input type="password" name="serper" id="serper"
               placeholder="{ph if has_serper else 'not set'}">
      </div>

      <div class="field">
        <label for="anthropic">Anthropic API key</label>
        <p class="hint">Helps Foxy understand unusual phrasings. Under $2 a month.</p>
        <input type="password" name="anthropic" id="anthropic"
               placeholder="{ph if has_anthropic else 'not set'}">
      </div>

      <div class="field">
        <label for="conf">Alert threshold</label>
        <p class="hint">Between 0 and 1. Higher means fewer, surer alerts.
        Anything below it goes to a daily digest instead. Default is
        {settings.min_confidence}.</p>
        <input type="text" name="conf" id="conf" value="{html.escape(conf)}"
               placeholder="{settings.min_confidence}">
      </div>
    </details>
  </div>
</form>

<p class="muted" style="margin-top:30px">Foxy checks every eight hours. Keep this
link if you want to change settings later, or
<a href="/app/{html.escape(install_id)}/stop">stop alerts</a>.</p>
"""
    return _shell(body, "Settings")


@router.post("/app/{install_id}/save", response_model=None)
async def save(install_id: str, request: Request) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    channel = (form.get("channel_id") or "").strip()
    action = form.get("action") or "save"

    with session() as s:
        row = installs.get(s, install_id)
        if row is None or not row.active:
            return _err("That settings link is not valid.")

        row.channel_id = channel
        # Blank means "leave what is already stored", so the placeholder dots
        # never overwrite a real key.
        if (form.get("serper") or "").strip():
            row.serper_key_enc = installs.encrypt(form["serper"].strip())
        if (form.get("anthropic") or "").strip():
            row.anthropic_key_enc = installs.encrypt(form["anthropic"].strip())
        conf = (form.get("conf") or "").strip()
        if conf:
            try:
                value = float(conf)
                if 0 < value <= 1:
                    row.min_confidence = str(value)
            except ValueError:
                pass
        token = row.token
        team = row.team_name

    # Join the channel so the first alert is not silently dropped. Public
    # channels join cleanly; a private one needs an invite, and the success
    # page says so.
    joined = True
    if channel:
        joined = SlackClient(token=token, target=channel).join_channel(channel)

    if action == "test" and channel:
        try:
            SlackClient(token=token, target=channel).post(
                [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                ":wave: *Foxy is connected.*\n"
                                "New YC and Speedrun companies will appear here, and "
                                "so will founders who announce before YC does."
                            ),
                        },
                    }
                ],
                "Foxy is connected.",
            )
            return RedirectResponse(
                f"/app/{install_id}/done?tested=1" + ("" if joined else "&joined=0"), 303
            )
        except Exception as exc:  # noqa: BLE001
            return _err(
                "Saved, but the test message could not be delivered.",
                f"{exc}. If it is a private channel, invite Foxy with /invite @Foxy.",
            )

    return RedirectResponse(
        f"/app/{install_id}/done" + ("" if joined else "?joined=0"), 303
    )


@router.post("/app/{install_id}/welcome")
def welcome(install_id: str) -> dict:
    """First alerts, now, rather than whenever the cron next fires.

    Driven from the success page rather than as a background task, because a
    serverless function is not guaranteed to survive past its response - and a
    welcome that silently does not happen is the failure this is here to fix.
    The install id is already the secret that guards the settings page.
    """
    from .hosted import welcome as run_welcome

    try:
        return run_welcome(install_id)
    except Exception as exc:  # noqa: BLE001 - the page must still render
        log.exception("welcome sweep failed for %s", install_id)
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}


@router.get("/app/{install_id}/upgrade", response_model=None)
def upgrade(install_id: str) -> HTMLResponse:
    """What Pro costs and how to pay for it.

    Payment is a wallet address rather than a card checkout, for a plain
    reason: a card processor takes a flat thirty cents, which is a tenth of a
    three dollar plan, and Stripe does not operate in Pakistan at all. On Base
    the fee is a fraction of a cent, so the price can actually be the price.
    """
    with session() as s:
        row = installs.get(s, install_id)
        if row is None or not row.active:
            return _err("That settings link is not valid.")
        team, active, label = row.team_name, row.plan_active, row.plan_label
        used, quota = row.alerts_used or 0, row.quota

    if active:
        body = f"""
<div class="ok">&#10003; {html.escape(label)}</div>
<h1>You are on Pro</h1>
<p class="lede">Unlimited alerts for <b>{html.escape(team)}</b>. Nothing to do.</p>
<p><a href="/app/{html.escape(install_id)}">Back to settings</a></p>"""
        return _shell(body, "Pro")

    wallet = settings.pay_wallet
    monthly, yearly = settings.price_monthly_usd, settings.price_yearly_usd

    if wallet:
        how = f"""
<div class="field">
  <label>Send {html.escape(settings.pay_asset)} on {html.escape(settings.pay_chain)} to</label>
  <input type="text" value="{html.escape(wallet)}" readonly onclick="this.select()">
  <p class="hint">Then email your workspace name to
  <a href="mailto:pakshaheen5300@gmail.com">pakshaheen5300@gmail.com</a> and Pro is
  switched on, usually within a day.</p>
</div>"""
    else:
        how = ('<p class="hint">Payment is not set up yet. Email '
               '<a href="mailto:pakshaheen5300@gmail.com">pakshaheen5300@gmail.com</a> '
               "and it will be sorted manually.</p>")

    body = f"""
<h1>Remove the limit</h1>
<p class="lede">The free plan covers <b>{quota} alerts</b>; <b>{html.escape(team)}</b>
has used {used}. Pro removes the cap and keeps everything else exactly the same.</p>

<div class="plans">
  <div class="plan">
    <b>Monthly</b>
    <span class="price">${html.escape(monthly)}</span>
    <span class="per">per month</span>
  </div>
  <div class="plan best">
    <b>Yearly</b>
    <span class="price">${html.escape(yearly)}</span>
    <span class="per">per year &middot; best value</span>
  </div>
</div>

{how}

<p class="hint">Nothing changes about how Foxy works. Same sources, same eight-hour
cadence, same channel. The only difference is that alerts stop being counted.</p>
<p><a href="/app/{html.escape(install_id)}">Back to settings</a></p>"""
    return _shell(body, "Upgrade")


@router.get("/app/{install_id}/stop", response_model=None)
def stop(install_id: str) -> HTMLResponse:
    with session() as s:
        installs.deactivate(s, install_id)
    return _shell(
        """<h1>Alerts stopped</h1>
        <p class="lede">Foxy will not post to your workspace again. Remove the app
        from Slack too if you want the bot gone entirely.</p>
        <p><a href="/">Back to Foxy</a></p>""",
        "Stopped",
    )


@router.get("/app/{install_id}/done", response_model=None)
def done(install_id: str, tested: str = "", joined: str = "") -> HTMLResponse:
    """Where saving lands. Confirms what will happen and gets out of the way."""
    with session() as s:
        row = installs.get(s, install_id)
        if row is None or not row.active:
            return _err("That settings link is not valid.")
        team = row.team_name
        token = row.token
        channel_id = row.channel_id

    name = channel_id
    try:
        for ch in SlackClient(token=token, target="").list_channels():
            if ch["id"] == channel_id:
                name = ("#" if not ch.get("is_private") else "\U0001f512 ") + ch.get(
                    "name", channel_id
                )
                break
    except Exception:  # noqa: BLE001 - a nicer label is not worth failing over
        pass

    tested_line = (
        '<li><b>A test message is already there.</b> Go and look.</li>'
        if tested
        else ""
    )
    invite_line = (
        '<li><b>Invite Foxy to that channel.</b> It is private, so run '
        "<code>/invite @Foxy</code> there or alerts will not arrive.</li>"
        if joined == "0"
        else ""
    )

    body = f"""
<div class="ok">&#10003; Foxy is watching</div>
<h1>All set</h1>
<p class="lede">Alerts for <b>{html.escape(team)}</b> will arrive in
<b>{html.escape(name)}</b>.</p>

<div class="next">
  <h2>What happens now</h2>
  <ul>
    {tested_line}
    {invite_line}
    <li id="first">Looking for your first signals now&hellip;</li>
    <li>Foxy checks all five sources every <b>eight hours</b>.</li>
    <li>New YC and Speedrun companies arrive as they appear.</li>
    <li>Founders who announce before YC publishes them are flagged
        <b>early</b>, which is the useful half.</li>
    <li>Nothing is ever reported twice.</li>
  </ul>
</div>

<script>
(function () {{
  var line = document.getElementById("first");
  if (!line) return;
  fetch("/app/{html.escape(install_id)}/welcome", {{ method: "POST" }})
    .then(function (r) {{ return r.json(); }})
    .then(function (d) {{
      if (d.ok && d.alerts > 0) {{
        line.innerHTML = "<b>" + d.alerts + " alert" +
          (d.alerts === 1 ? "" : "s") +
          " just landed in your channel.</b> Go and look.";
      }} else if (d.ok) {{
        line.innerHTML = "Nothing new to report yet — you are caught up. " +
          "The next check runs within eight hours.";
      }} else {{
        line.innerHTML = "Could not send your first alerts: " +
          (d.reason || "unknown") + ".";
      }}
    }})
    .catch(function () {{
      line.innerHTML = "Still working on your first signals. " +
        "They will arrive shortly.";
    }});
}})();
</script>

<div class="links">
  <a class="lk" href="/#/how">
    <b>How it works</b><span>The pipeline, the verdicts, the accuracy</span>
  </a>
  <a class="lk" href="/app/{html.escape(install_id)}">
    <b>Change settings</b><span>Channel, API keys, alert threshold</span>
  </a>
  <a class="lk" href="https://github.com/ana-momin/Foxy">
    <b>Source code</b><span>MIT licensed, self-host it if you prefer</span>
  </a>
  <a class="lk" href="/">
    <b>Back to Foxy</b><span>The overview</span>
  </a>
</div>

<p class="muted" style="margin-top:32px">Bookmark
<a href="/app/{html.escape(install_id)}">this settings link</a>. It is the only way
back in, and it is how you stop alerts later.</p>
"""
    return _shell(body, "All set")
