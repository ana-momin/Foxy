"""The operator's console: see every workspace, switch plans with a click.

Activating a paid plan used to mean running a CLI command, which is not
something a product should ask of anybody - not of a customer, and not of the
person running it either, who will be doing this from a phone as often as not.

Pond does not tell the agent who is calling, so a subscription bought there
cannot be matched to a Slack workspace automatically. Someone has to join the
two records. This is that job, reduced to reading a row and pressing a button.

Guarded by ADMIN_KEY. There is no login: one secret in the URL, compared in
constant time, and the page is linked from nowhere.
"""

from __future__ import annotations

import datetime as dt
import hmac
import html
import logging

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from . import installs
from .config import settings
from .db import session
from .oauth import _page

log = logging.getLogger("foxy.admin")

router = APIRouter()

_CSS = """
.wsp{width:100%;border-collapse:collapse;margin:26px 0}
.wsp th{text-align:left;font-size:11.5px;font-weight:600;letter-spacing:.08em;
text-transform:uppercase;color:var(--muted);font-family:"JetBrains Mono",monospace;
padding:0 12px 10px 0;border-bottom:1px solid var(--border)}
.wsp td{padding:15px 12px 15px 0;border-bottom:1px solid var(--border);
font-size:14px;vertical-align:middle}
.wsp .name{font-weight:600;font-size:14.5px}
.wsp .code{font-family:"JetBrains Mono",monospace;font-size:12.5px;color:var(--muted)}
.tag{display:inline-block;font-size:11.5px;font-weight:600;padding:3px 9px;
border-radius:20px;letter-spacing:.02em}
.tag.pro{background:var(--accent);color:#fff}
.tag.free{background:var(--border);color:var(--ink2)}
.tag.off{background:transparent;color:var(--muted);border:1px solid var(--border2)}
.acts{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}
.mini{border:1px solid var(--border2);background:var(--surface);color:var(--ink2);
border-radius:8px;padding:7px 12px;font-size:13px;font-weight:500;cursor:pointer;
font-family:inherit;transition:border-color .15s ease,color .15s ease}
.mini:hover{border-color:var(--accent);color:var(--accent)}
.mini.go{background:var(--accent);border-color:var(--accent);color:#fff}
.mini.go:hover{background:var(--accent2);color:#fff}
.asks{background:var(--surface);border:1px solid var(--accent);border-radius:13px;
padding:20px 22px;margin-bottom:26px;box-shadow:var(--sh)}
.asks h2{font-size:15px;font-weight:600;margin:0 0 4px}
.asks p{font-size:13.5px;color:var(--muted);margin:0 0 14px}
.ask{display:flex;justify-content:space-between;align-items:center;gap:14px;
flex-wrap:wrap;padding:11px 0;border-top:1px solid var(--border)}
.empty{color:var(--muted);font-size:14px;padding:26px 0}
"""


def _shell(body: str, title: str = "Foxy admin") -> HTMLResponse:
    resp = _page(title, body)
    return HTMLResponse(resp.body.decode().replace("</style>", _CSS + "</style>"))


def _authorised(key: str) -> bool:
    """One secret, compared in constant time.

    With no ADMIN_KEY configured the console stays shut rather than open: a
    console that defaults to reachable is how a deployment ends up with one
    nobody meant to publish.
    """
    return bool(settings.admin_key) and hmac.compare_digest(key or "", settings.admin_key)


def _denied() -> HTMLResponse:
    return _shell(
        "<h1>Not available</h1><p class='lede'>This page needs a valid key.</p>",
        "Foxy",
    )


@router.get("/admin", response_model=None)
def console(key: str = "") -> HTMLResponse:
    if not _authorised(key):
        return _denied()

    with session() as s:
        rows = [
            {
                "id": r.id,
                "team": r.team_name or "(unnamed)",
                "code": r.claim_code,
                "active": r.active,
                "pro": r.plan_active,
                "label": r.plan_label,
                "used": r.alerts_used or 0,
                "quota": r.quota,
                "asked": r.upgrade_requested_at,
            }
            for r in s.execute(select(installs.Install)).scalars().all()
        ]

    rows.sort(key=lambda r: (r["asked"] is None, not r["active"], r["team"].lower()))
    k = html.escape(key)

    waiting = [r for r in rows if r["asked"]]
    asks = ""
    if waiting:
        items = "".join(
            f"""
    <div class="ask">
      <div>
        <div class="name">{html.escape(r["team"])}</div>
        <div class="code">{html.escape(r["code"])} &middot; asked
        {r["asked"]:%d %b}</div>
      </div>
      {_buttons(r, k)}
    </div>"""
            for r in waiting
        )
        asks = f"""
<div class="asks">
  <h2>Waiting to be switched on</h2>
  <p>These workspaces say they have subscribed on Pond.</p>
  {items}
</div>"""

    body_rows = "".join(
        f"""
  <tr>
    <td>
      <div class="name">{html.escape(r["team"])}</div>
      <div class="code">{html.escape(r["code"])}</div>
    </td>
    <td>{_tag(r)}</td>
    <td class="code">{r["used"]}{f" / {r['quota']}" if r["quota"] else ""}</td>
    <td><div class="acts">{_buttons(r, k)}</div></td>
  </tr>"""
        for r in rows
    )

    table = (
        f"""
<table class="wsp">
  <tr><th>Workspace</th><th>Plan</th><th>Alerts</th><th></th></tr>
  {body_rows}
</table>"""
        if rows
        else '<p class="empty">No workspaces yet.</p>'
    )

    return _shell(
        f"""
<h1>Workspaces</h1>
<p class="lede">{len(rows)} installed, {sum(1 for r in rows if r["pro"])} on Pro.</p>
{asks}
{table}""",
        "Foxy admin",
    )


def _tag(r: dict) -> str:
    if not r["active"]:
        return '<span class="tag off">stopped</span>'
    if r["pro"]:
        return f'<span class="tag pro">{html.escape(r["label"])}</span>'
    return '<span class="tag free">Free</span>'


def _buttons(r: dict, key: str) -> str:
    """One form per action. A GET that changes a plan would be triggered by
    anything that follows links, a preview fetch included."""
    def form(months: int, label: str, cls: str) -> str:
        return f"""
      <form method="post" action="/admin/plan" style="display:inline">
        <input type="hidden" name="key" value="{key}">
        <input type="hidden" name="install_id" value="{html.escape(r["id"])}">
        <input type="hidden" name="months" value="{months}">
        <button class="mini {cls}" type="submit">{label}</button>
      </form>"""

    if r["pro"]:
        return form(12, "+1 year", "") + form(0, "Downgrade", "")
    return form(1, "Pro &middot; 1 month", "go") + form(12, "Pro &middot; 1 year", "go")


@router.post("/admin/plan", response_model=None)
def set_plan(
    key: str = Form(""),
    install_id: str = Form(""),
    months: int = Form(1),
) -> HTMLResponse | RedirectResponse:
    if not _authorised(key):
        return _denied()

    with session() as s:
        row = installs.get(s, install_id)
        if row is None:
            return _denied()
        if months <= 0:
            installs.downgrade(row)
            log.info("admin downgraded %s", row.team_name)
        else:
            installs.activate(row, months)
            log.info("admin gave %s %d month(s) of Pro", row.team_name, months)

    return RedirectResponse(f"/admin?key={key}", 303)


@router.post("/app/{install_id}/subscribed", response_model=None)
def subscribed(install_id: str) -> RedirectResponse:
    """The workspace says it has paid.

    It does not grant anything - it cannot, since nothing here can see a Pond
    subscription. It puts the request in front of whoever can check, which
    beats asking a customer to email a code and hope.
    """
    with session() as s:
        row = installs.get(s, install_id)
        if row is not None and row.active:
            row.upgrade_requested_at = dt.datetime.now(dt.timezone.utc).replace(
                tzinfo=None
            )
            log.info("%s says it has subscribed", row.team_name)

    return RedirectResponse(f"/app/{install_id}/upgrade?asked=1", 303)
