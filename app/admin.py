"""The operator's console: is Foxy alive, and what is it doing?

Written to answer, at a glance and from a phone, the question that would
otherwise be asked of somebody: is it still working. So it reads like a status
page - one verdict at the top, then the evidence underneath - rather than like
a database viewer.

Everything here is measured, not asserted. A source is "up" because its last
run said so; alerts are counted by the Slack message id they carry, never by
how many were decided on. That distinction is not pedantry: Foxy once recorded
687 alerts and delivered none of them, and a console that counted decisions
would have shown a healthy green wall the entire time.

Guarded by ADMIN_KEY. There is no login: one secret in the URL, compared in
constant time, and the page is linked from nowhere.
"""

from __future__ import annotations

import datetime as dt
import hmac
import html
import logging
from typing import Any

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select

from . import budget, installs
from .config import settings
from .db import Alert, PondRun, PondTask, Seen, health_snapshot, session
from .oauth import _page

log = logging.getLogger("foxy.admin")

router = APIRouter()

# The sweep cron, kept here so "next due" is not a guess. Mirrors
# .github/workflows/hosted-sweep.yml — if that changes, change this.
SWEEP_HOURS = (0, 8, 16)

_CSS = """
.dash{margin:26px 0}
.verdict{display:flex;align-items:center;gap:13px;background:var(--surface);
border:1px solid var(--border);border-radius:14px;padding:20px 22px;margin-bottom:14px}
.verdict.bad{border-color:#d9822b}
.dot{width:11px;height:11px;border-radius:50%;background:#2ea043;flex:none;
box-shadow:0 0 0 4px rgba(46,160,67,.16)}
.dot.warn{background:#d9822b;box-shadow:0 0 0 4px rgba(217,130,43,.16)}
.dot.off{background:var(--border2);box-shadow:none}
.verdict h2{font-size:16.5px;font-weight:600;margin:0}
.verdict span{font-size:13px;color:var(--muted);margin-left:auto;
font-family:"JetBrains Mono",monospace}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;
margin-bottom:26px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;
padding:16px 18px}
.tile b{display:block;font-size:26px;font-weight:600;letter-spacing:-.02em;
line-height:1.15}
.tile span{display:block;font-size:12.5px;color:var(--muted);margin-top:3px}

.panel{background:var(--surface);border:1px solid var(--border);border-radius:13px;
padding:20px 22px;margin-bottom:16px}
.panel h3{font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
color:var(--muted);font-family:"JetBrains Mono",monospace;margin:0 0 14px}
.line{display:flex;align-items:center;gap:11px;padding:9px 0;font-size:14px;
border-top:1px solid var(--border)}
.line:first-of-type{border-top:0}
.line .nm{font-weight:500}
.line .val{margin-left:auto;color:var(--muted);font-size:13px;
font-family:"JetBrains Mono",monospace;text-align:right}

.meter{height:6px;border-radius:3px;background:var(--border);overflow:hidden;
margin:12px 0 8px}
.meter span{display:block;height:100%;background:var(--accent);border-radius:3px}
.meter.warn span{background:#d9822b}

.wsp{width:100%;border-collapse:collapse}
.wsp th{text-align:left;font-size:11px;font-weight:600;letter-spacing:.07em;
text-transform:uppercase;color:var(--muted);font-family:"JetBrains Mono",monospace;
padding:0 10px 9px 0;border-bottom:1px solid var(--border)}
.wsp td{padding:13px 10px 13px 0;border-bottom:1px solid var(--border);font-size:14px;
vertical-align:middle}
.wsp tr:last-child td{border-bottom:0}
.wsp .name{font-weight:600;font-size:14.5px}
.wsp .sub{font-size:12px;color:var(--muted);margin-top:2px}
.tag{display:inline-block;font-size:11px;font-weight:600;padding:3px 9px;
border-radius:20px;white-space:nowrap}
.tag.pro{background:var(--accent);color:#fff}
.tag.free{background:var(--border);color:var(--ink2)}
.tag.off{background:transparent;color:var(--muted);border:1px solid var(--border2)}
.tag.cap{background:#d9822b;color:#fff}
.acts{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.mini{border:1px solid var(--border2);background:var(--surface);color:var(--ink2);
border-radius:8px;padding:6px 11px;font-size:12.5px;font-weight:500;cursor:pointer;
font-family:inherit;transition:border-color .15s ease,color .15s ease;white-space:nowrap}
.mini:hover{border-color:var(--accent);color:var(--accent)}
.mini.go{background:var(--accent);border-color:var(--accent);color:#fff}
.mini.go:hover{background:var(--accent2);color:#fff}
.empty{color:var(--muted);font-size:14px;padding:22px 0}
@media(max-width:620px){.wsp .hide{display:none}}
"""


def _shell(body: str, title: str = "Foxy status") -> HTMLResponse:
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


# ---------------------------------------------------------------------------
# measuring
# ---------------------------------------------------------------------------


def _ago(when: dt.datetime | None) -> str:
    if when is None:
        return "never"
    if when.tzinfo is not None:
        when = when.astimezone(dt.timezone.utc).replace(tzinfo=None)
    mins = (dt.datetime.utcnow() - when).total_seconds() / 60
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{int(mins)}m ago"
    if mins < 48 * 60:
        return f"{int(mins / 60)}h ago"
    return f"{int(mins / 1440)}d ago"


def _next_sweep() -> str:
    """When the cron fires next. Stated rather than guessed at."""
    now = dt.datetime.now(dt.timezone.utc)
    for hour in SWEEP_HOURS:
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt > now:
            break
    else:
        nxt = (now + dt.timedelta(days=1)).replace(
            hour=SWEEP_HOURS[0], minute=0, second=0, microsecond=0
        )
    mins = (nxt - now).total_seconds() / 60
    return f"in {int(mins)}m" if mins < 60 else f"in {int(mins / 60)}h {int(mins % 60)}m"


def gather() -> dict[str, Any]:
    """Everything the console shows, measured in one place.

    Separated from the rendering so it can be asserted against, and so /admin
    and the JSON endpoint can never disagree about the state of the world.
    """
    with session() as s:
        snap = health_snapshot(s)

        # Delivered means Slack returned a message id. Counting decisions here
        # is exactly the mistake that hid 687 undelivered alerts.
        delivered = s.execute(
            select(func.count()).select_from(Alert).where(Alert.ts.isnot(None))
        ).scalar()
        early = s.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.ts.isnot(None), Alert.kind == "early")
        ).scalar()
        last_alert = s.execute(
            select(func.max(Alert.created_at)).where(Alert.ts.isnot(None))
        ).scalar()
        tracked = s.execute(select(func.count()).select_from(Seen)).scalar()

        tasks = dict(
            s.execute(
                select(PondTask.status, func.count()).group_by(PondTask.status)
            ).all()
        )
        pond_runs = s.execute(select(func.count()).select_from(PondRun)).scalar()
        last_pond = s.execute(select(func.max(PondTask.created_at))).scalar()

        rows = []
        for r in s.execute(select(installs.Install)).scalars().all():
            used, quota = r.alerts_used or 0, r.quota
            rows.append(
                {
                    "id": r.id,
                    "team": r.team_name or "(unnamed)",
                    "channel": r.channel_id,
                    "active": r.active,
                    "pro": r.plan_active,
                    "label": r.plan_label,
                    "used": used,
                    "quota": quota,
                    "at_cap": bool(quota) and used >= quota,
                    "no_channel": not r.channel_id,
                    "last_alert": r.last_alert_at,
                    "error": r.last_error,
                }
            )

    rows.sort(key=lambda r: (not r["active"], -r["used"], r["team"].lower()))
    live = [r for r in rows if r["active"]]

    sources = snap["sources"]
    failing = [n for n, i in sources.items() if not i["ok"]]
    # Things worth interrupting someone about, in the order they matter.
    problems: list[str] = []
    if failing:
        problems.append(f"{len(failing)} source failing")
    stalled = [r for r in live if r["at_cap"]]
    if stalled:
        problems.append(f"{len(stalled)} workspace(s) at the free cap")
    unfinished = [r for r in live if r["no_channel"]]
    if unfinished:
        problems.append(f"{len(unfinished)} install(s) without a channel")
    if tasks.get("failed"):
        problems.append(f"{tasks['failed']} Pond task(s) failed")
    b = budget.snapshot()
    if b.get("low"):
        problems.append("search credits low")

    return {
        "ok": not problems,
        "problems": problems,
        "sources": sources,
        "failing": failing,
        "sweeps": snap["sweeps_completed"],
        "last_sweep": snap["last_sweep_at"],
        "next_sweep": _next_sweep(),
        "delivered": delivered,
        "early": early,
        "last_alert": last_alert,
        "tracked": tracked,
        "tasks": tasks,
        "pond_runs": pond_runs,
        "last_pond": last_pond,
        "budget": b,
        "workspaces": rows,
        "live": len(live),
        "channels": len({r["channel"] for r in live if r["channel"]}),
    }


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


@router.get("/admin", response_model=None)
def console(key: str = "") -> HTMLResponse:
    if not _authorised(key):
        return _denied()

    d = gather()
    k = html.escape(key)
    b = d["budget"]

    if d["ok"]:
        verdict = (
            '<div class="verdict"><span class="dot"></span>'
            "<h2>Foxy is running normally</h2>"
        )
    else:
        trouble = "; ".join(d["problems"])
        verdict = (
            '<div class="verdict bad"><span class="dot warn"></span>'
            f"<h2>{html.escape(trouble[:1].upper() + trouble[1:])}</h2>"
        )
    verdict += f'<span>checked {dt.datetime.now(dt.timezone.utc):%H:%M} UTC</span></div>'

    tiles = "".join(
        f'<div class="tile"><b>{v}</b><span>{lab}</span></div>'
        for v, lab in [
            (d["live"], "active workspaces"),
            (d["channels"], "Slack channels"),
            (f"{d['delivered']:,}", "alerts delivered"),
            (d["early"], "early catches"),
            (f"{d['tracked']:,}", "companies tracked"),
        ]
    )

    # Sources: the "are the servers up" part.
    src_lines = ""
    for name, info in sorted(d["sources"].items()):
        cls = "dot" if info["ok"] else "dot warn"
        detail = (
            f"{info['found']} seen · {info['new']} new"
            if info["ok"]
            else html.escape(str(info["error"])[:60])
        )
        src_lines += (
            f'<div class="line"><span class="{cls}"></span>'
            f'<span class="nm">{html.escape(name)}</span>'
            f'<span class="val">{detail}<br>{_ago(_parse(info["ran_at"]))}</span></div>'
        )

    pond_lines = "".join(
        f'<div class="line"><span class="nm">{nm}</span>'
        f'<span class="val">{val}</span></div>'
        for nm, val in [
            ("Tasks completed", d["tasks"].get("completed", 0)),
            ("Tasks failed", d["tasks"].get("failed", 0)),
            ("Tasks waiting", d["tasks"].get("queued", 0) + d["tasks"].get("running", 0)),
            ("Action calls", d["pond_runs"]),
            ("Last call", _ago(d["last_pond"])),
        ]
    )

    sweep_lines = "".join(
        f'<div class="line"><span class="nm">{nm}</span>'
        f'<span class="val">{val}</span></div>'
        for nm, val in [
            ("Sweeps completed", d["sweeps"]),
            ("Last sweep", _ago(_parse(d["last_sweep"]))),
            ("Next sweep", d["next_sweep"]),
            ("Last alert delivered", _ago(d["last_alert"])),
        ]
    )

    if b.get("tracked"):
        pct = round(b["spent_share"] * 100)
        meter = "meter warn" if b.get("low") else "meter"
        credits = f"""
<div class="{meter}"><span style="width:{min(100, pct)}%"></span></div>
<div class="line" style="border:0;padding-top:4px">
  <span class="nm">{b['remaining']:,} searches left</span>
  <span class="val">{b['used']:,} of {b['allowance']:,} used</span>
</div>"""
    else:
        credits = '<p class="empty">Not tracked.</p>'

    return _shell(
        f"""
<h1>Foxy</h1>
<p class="lede">Everything Foxy is doing, measured rather than assumed.</p>

<div class="dash">
  {verdict}
  <div class="tiles">{tiles}</div>

  <div class="panel"><h3>Sources</h3>{src_lines}</div>
  <div class="panel"><h3>Sweeps</h3>{sweep_lines}</div>
  <div class="panel"><h3>Pond</h3>{pond_lines}</div>
  <div class="panel"><h3>Search credits</h3>{credits}</div>

  <div class="panel">
    <h3>Workspaces</h3>
    {_table(d["workspaces"], k)}
  </div>
</div>""",
        "Foxy status",
    )


def _parse(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _table(rows: list[dict], key: str) -> str:
    if not rows:
        return '<p class="empty">No workspaces yet.</p>'

    body = ""
    for r in rows:
        note = ""
        if r["no_channel"]:
            note = "no channel chosen"
        elif r["error"]:
            note = html.escape(str(r["error"])[:48])
        elif r["last_alert"]:
            note = f"last alert {_ago(r['last_alert'])}"

        body += f"""
  <tr>
    <td>
      <div class="name">{html.escape(r["team"])}</div>
      {f'<div class="sub">{note}</div>' if note else ""}
    </td>
    <td>{_tag(r)}</td>
    <td class="val hide">{r["used"]}{f" / {r['quota']}" if r["quota"] else ""}</td>
    <td><div class="acts">{_buttons(r, key)}</div></td>
  </tr>"""

    return f"""
<table class="wsp">
  <tr><th>Workspace</th><th>Plan</th><th class="hide">Alerts</th><th></th></tr>
  {body}
</table>"""


def _tag(r: dict) -> str:
    if not r["active"]:
        return '<span class="tag off">stopped</span>'
    if r["pro"]:
        return f'<span class="tag pro">{html.escape(r["label"])}</span>'
    if r["at_cap"]:
        return '<span class="tag cap">at cap</span>'
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
    # A workspace that has run out is the one worth lifting, so it leads.
    lead = "go" if r["at_cap"] else ""
    return form(1, "Pro &middot; 1 month", lead) + form(12, "1 year", "")


def _jsonable(value: Any) -> Any:
    """Datetimes do not survive json.dumps.

    The page formats them and never noticed; this endpoint handed them straight
    to the serialiser and returned a 500 in production while every test passed,
    because the test database had no dated rows to trip over.
    """
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


@router.get("/admin/status")
def status_json(key: str = "") -> JSONResponse:
    """The same numbers as JSON, for watching from somewhere else.

    Reads from `gather` so the page and this can never tell different stories.
    """
    if not _authorised(key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    d = gather()
    d["workspaces"] = [
        {k: v for k, v in w.items() if k != "id"} for w in d["workspaces"]
    ]
    return JSONResponse(_jsonable(d))


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
