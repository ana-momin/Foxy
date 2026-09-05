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
.btn{display:inline-block;background:var(--accent);color:#fff;border-radius:10px;
padding:13px 24px;font-size:15px;font-weight:500;text-decoration:none}
.btn:hover{background:var(--accent2)}
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
.plans{display:grid;grid-template-columns:1fr;gap:16px;margin:30px 0}
@media(min-width:620px){.plans{grid-template-columns:1fr 1.25fr;align-items:stretch}}

.plan{background:var(--surface);border:1px solid var(--border);border-radius:14px;
padding:24px;display:flex;flex-direction:column}
.plan .tier{font-size:12px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;
color:var(--muted);font-family:"JetBrains Mono",monospace;margin-bottom:14px}
.plan .price{font-size:40px;font-weight:600;letter-spacing:-.03em;line-height:1;
display:flex;align-items:baseline;gap:7px}
.plan .price em{font-size:15px;font-weight:400;font-style:normal;color:var(--muted);
letter-spacing:0}
.plan .cap{font-size:13.5px;color:var(--muted);margin-top:9px}

.plan.pro{border-color:var(--accent);position:relative;box-shadow:var(--sh)}
.plan.pro .tier{color:var(--accent)}

.perks{list-style:none;margin:20px 0 0;padding:0}
.perks li{position:relative;padding-left:26px;margin-bottom:11px;font-size:14.5px;
color:var(--ink2);line-height:1.5}
.perks li:last-child{margin-bottom:0}
.perks li:before{content:"";position:absolute;left:2px;top:6px;width:9px;height:5px;
border-left:2px solid var(--accent);border-bottom:2px solid var(--accent);
transform:rotate(-45deg)}
.perks b{color:var(--ink);font-weight:600}

.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:26px}
.ghost{display:inline-block;background:transparent;color:var(--ink2);
border:1px solid var(--border2);border-radius:10px;padding:12px 20px;font-size:14.5px;
font-weight:500;text-decoration:none;cursor:pointer;font-family:inherit;
transition:border-color .18s ease,color .18s ease}
.ghost:hover{border-color:var(--accent);color:var(--accent)}
.btn:hover{background:var(--accent2)}

.limit{background:var(--surface);border:1px solid var(--accent);border-radius:13px;
padding:20px 22px;margin-bottom:28px;box-shadow:var(--sh)}
.limit h2{font-size:15.5px;font-weight:600;margin:0 0 6px}
.limit p{font-size:14px;color:var(--ink2);margin:0 0 16px;line-height:1.55}
.save:disabled{opacity:.45;cursor:not-allowed}
.claim{border-top:1px solid var(--border);margin-top:32px;padding-top:26px}
.claim h2{font-size:14px;font-weight:600;margin:0 0 6px}
.claim p{font-size:13.5px;color:var(--muted);margin:0 0 12px;line-height:1.55}
.claim input{max-width:260px;text-align:center;letter-spacing:.08em;font-weight:600}
.save:disabled:hover{background:var(--accent)}
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

    # Three states, and each looks like what it is. The exhausted one blocks
    # the form: a Save button that looks live and quietly changes nothing is
    # worse than one that is plainly unavailable.
    exhausted = bool(quota) and used >= quota
    disabled = " disabled" if exhausted else ""

    if exhausted:
        usage_card = f"""
<div class="limit">
  <h2>Your free alerts are used up</h2>
  <p>Foxy has sent all {quota} of them to <b>{html.escape(team)}</b>, and has paused.
  Nothing was lost &mdash; it picks up again the moment you upgrade.</p>
  <a class="btn" href="/app/{html.escape(install_id)}/upgrade">See Foxy Pro</a>
</div>"""
    elif quota:
        pct = min(100, round(used * 100 / quota))
        usage_card = f"""
<div class="usage">
  <div class="usage-top">
    <b>{html.escape(plan)}</b>
    <span>{used} of {quota} alerts used</span>
  </div>
  <div class="bar"><span style="width:{pct}%"></span></div>
  <div class="actions" style="margin-top:16px">
    <a class="ghost" href="/app/{html.escape(install_id)}/upgrade">Upgrade to Pro</a>
  </div>
</div>"""
    else:
        usage_card = f"""
<div class="usage">
  <div class="usage-top">
    <b>{html.escape(plan)}</b>
    <span>{used} alerts sent</span>
  </div>
</div>"""

    ph = "•" * 12
    body = f"""
{banner}
<h1>Foxy settings</h1>
<p class="lede">Workspace: <b>{html.escape(team)}</b>. Nothing to install and nothing
to run. Choose a channel and Foxy starts watching.</p>

{usage_card}

<form method="post" action="/app/{html.escape(install_id)}/save">
  <div class="field">
    <label for="ch">Where should alerts go?</label>
    {picker}
  </div>

  <div class="row">
    <button class="save" type="submit" name="action" value="save"{disabled}>Save</button>
    <button class="save" type="submit" name="action" value="test"{disabled}
            style="background:var(--surface);color:var(--ink);border:1px solid var(--border2)">
      Save and send a test
    </button>
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
def upgrade(install_id: str, asked: str = "") -> HTMLResponse:
    """Foxy Pro: what it costs and what it gets you.

    Foxy takes no payment. Pond sells and collects, and the plans are declared
    in the manifest, so the price shown here and the price a customer is
    charged come from one place and cannot disagree.
    """
    with session() as s:
        row = installs.get(s, install_id)
        if row is None or not row.active:
            return _err("That settings link is not valid.")
        team, active, label = row.team_name, row.plan_active, row.plan_label
        used, quota = row.alerts_used or 0, row.quota
        code = row.claim_code

    back = f'<a class="ghost" href="/app/{html.escape(install_id)}">Back to settings</a>'

    if active:
        body = f"""
<div class="ok">&#10003; {html.escape(label)}</div>
<h1>You are on Foxy Pro</h1>
<p class="lede">Everything is switched on for <b>{html.escape(team)}</b>, and
{used} alerts have gone out so far. Nothing else to do.</p>
<div class="actions">{back}</div>"""
        return _shell(body, "Foxy Pro")

    # Pond cannot tell Foxy which workspace subscribed, so the last step is a
    # person joining the two records. This at least puts the request in front
    # of them, rather than asking a customer to email a code and hope.
    if asked:
        after = """
<div class="claim">
  <h2>Thanks &mdash; nearly there</h2>
  <p>Pro is switched on by hand, usually within a day. Nothing else for you
  to do, and this page will say Pro once it is.</p>
</div>"""
    else:
        after = f"""
<div class="claim">
  <h2>Already subscribed?</h2>
  <p>Pond does not tell Foxy which workspace paid, so press this and Pro is
  switched on for <b>{html.escape(team)}</b>, usually within a day.</p>
  <form method="post" action="/app/{html.escape(install_id)}/subscribed">
    <button class="ghost" type="submit">I have subscribed</button>
  </form>
</div>"""

    price = f"{settings.price_monthly_minor / 100:.0f}"
    included = f"{settings.pro_included_results:,}"
    spent = (
        f"You have used all {quota} free alerts."
        if quota and used >= quota
        else f"You have used {used} of your {quota} free alerts."
    )

    body = f"""
<h1>Foxy Pro</h1>
<p class="lede">{spent} Pro lifts the cap. Everything else stays as it is.</p>

<div class="plans">
  <div class="plan">
    <div class="tier">Free</div>
    <div class="price">$0</div>
    <div class="cap">{settings.free_included_results} alerts, once</div>
    <ul class="perks">
      <li>All five sources</li>
      <li>Early founder signals</li>
      <li>Pauses at the cap</li>
    </ul>
  </div>

  <div class="plan pro">
    <div class="tier">Pro</div>
    <div class="price">${html.escape(price)} <em>per month</em></div>
    <div class="cap">{included} alerts a month</div>
    <ul class="perks">
      <li>Founders who announce <b>before YC publishes</b></li>
      <li>All five sources, checked every eight hours</li>
      <li>Follow-up in thread when YC confirms</li>
      <li>Cancel any time, billed by Pond</li>
    </ul>
  </div>
</div>

<div class="actions">
  <a class="btn" href="{html.escape(settings.pond_listing_url)}">Subscribe on Pond</a>
  {back}
</div>

{after}"""
    return _shell(body, "Foxy Pro")


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
