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
/* The console keeps a light surface whatever the viewer's system says. A
   status page is read at a glance, often outdoors on a phone, and the warm
   paper reads better than a dark one for a wall of numbers. */
.adm{--paper:#FFFDFB;--card:#fff;--line:#F0EAE3;--line2:#E2D9CF;
--txt:#16120E;--txt2:#6B6157;--dim:#9C9086;--brand:#E1590C;
--up:#2E9E5B;--warn:#D9822B;
max-width:760px;margin:0 auto}
.adm *{color-scheme:light}

.top{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:44px}
.top h1{font-size:30px;letter-spacing:-.034em;margin:0;font-weight:600;color:var(--txt)}
.state{display:inline-flex;align-items:center;gap:8px;margin-left:auto;
font-size:13.5px;color:var(--txt2);padding-top:7px}
.pip{width:8px;height:8px;border-radius:50%;background:var(--up);flex:none;
box-shadow:0 0 0 3px rgba(46,158,91,.14)}
.pip.warn{background:var(--warn);box-shadow:0 0 0 3px rgba(217,130,43,.14)}

.figs{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:46px}
@media(max-width:560px){.figs{grid-template-columns:repeat(2,1fr);gap:26px 8px}}
.fig b{display:block;font-size:36px;font-weight:600;letter-spacing:-.04em;
line-height:1;color:var(--txt);font-variant-numeric:tabular-nums}
.fig span{display:block;font-size:12px;color:var(--dim);margin-top:7px;
letter-spacing:.01em}

.sec{margin-bottom:42px}
.sec>h2{font-size:11px;font-weight:600;letter-spacing:.11em;text-transform:uppercase;
color:var(--dim);font-family:"JetBrains Mono",monospace;margin:0 0 16px}

.srcs{display:flex;flex-wrap:wrap;gap:8px}
.src{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);
border-radius:999px;padding:7px 14px 7px 11px;font-size:13px;color:var(--txt2);
background:var(--card)}
.src i{width:6px;height:6px;border-radius:50%;background:var(--up);font-style:normal}
.src i.warn{background:var(--warn)}
.src em{font-style:normal;color:var(--brand);font-weight:600;font-size:12px}

.att{border:1px solid var(--warn);border-radius:12px;overflow:hidden;background:var(--card)}
.att .row{border-top:1px solid var(--line)}
.att .row:first-child{border-top:0}

.row{display:flex;align-items:center;gap:14px;padding:15px 18px;
border-top:1px solid var(--line)}
.rows>.row:first-child{border-top:0}
.rows{border:1px solid var(--line);border-radius:12px;background:var(--card)}
.row .who{min-width:0}
.row .nm{font-size:14.5px;font-weight:600;color:var(--txt);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .sub{font-size:12.5px;color:var(--dim);margin-top:2px}
.row .rt{margin-left:auto;display:flex;align-items:center;gap:10px;flex:none}
.num{font-size:13px;color:var(--dim);font-family:"JetBrains Mono",monospace;
font-variant-numeric:tabular-nums}

.chip{font-size:11px;font-weight:600;padding:3px 9px;border-radius:999px;
white-space:nowrap;letter-spacing:.01em}
.chip.pro{background:var(--brand);color:#fff}
.chip.free{background:#F3EEE8;color:var(--txt2)}
.chip.cap{background:#FDF0E2;color:var(--warn)}
.chip.off{background:transparent;color:var(--dim);border:1px solid var(--line2)}

.mini{border:1px solid var(--line2);background:var(--card);color:var(--txt2);
border-radius:8px;padding:6px 12px;font-size:12.5px;font-weight:500;cursor:pointer;
font-family:inherit;white-space:nowrap;transition:border-color .15s,color .15s}
.mini:hover{border-color:var(--brand);color:var(--brand)}
.mini.go{background:var(--brand);border-color:var(--brand);color:#fff}
.mini.go:hover{background:#C94D08}

.bar{height:4px;border-radius:2px;background:var(--line);overflow:hidden;margin-bottom:10px}
.bar span{display:block;height:100%;background:var(--brand);border-radius:2px}
.bar.warn span{background:var(--warn)}

.foot{font-size:13px;color:var(--dim);line-height:1.9;border-top:1px solid var(--line);
padding-top:18px}
.foot b{color:var(--txt2);font-weight:500;font-variant-numeric:tabular-nums}
.none{color:var(--dim);font-size:13.5px}
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

    # One line for the verdict. If a glance is enough, nothing else needs reading.
    if d["ok"]:
        state = '<span class="pip"></span>everything normal'
    else:
        state = f'<span class="pip warn"></span>{html.escape(d["problems"][0])}'

    figs = "".join(
        f'<div class="fig"><b>{v}</b><span>{lab}</span></div>'
        for v, lab in [
            (f"{d['delivered']:,}", "alerts"),
            (d["early"], "early"),
            (d["live"], "workspaces"),
            (f"{d['tracked']:,}", "tracked"),
        ]
    )

    # Only what needs a person, and only when there is any.
    attention = ""
    needs = [w for w in d["workspaces"] if w["active"] and (w["at_cap"] or w["no_channel"])]
    if needs:
        rows = "".join(
            f"""
    <div class="row">
      <div class="who">
        <div class="nm">{html.escape(w["team"])}</div>
        <div class="sub">{"out of free alerts" if w["at_cap"] else "no channel chosen"}</div>
      </div>
      <div class="rt">{_buttons(w, k) if w["at_cap"] else ""}</div>
    </div>"""
            for w in needs
        )
        attention = f'<div class="sec"><h2>Needs you</h2><div class="att">{rows}</div></div>'

    # Sources as a health strip: a dot each, and a number only when there is news.
    chips = "".join(
        f'<span class="src"><i class="{"" if i["ok"] else "warn"}"></i>'
        f'{html.escape(name.replace("_", " "))}'
        + (f' <em>+{i["new"]}</em>' if i["ok"] and i["new"] else "")
        + "</span>"
        for name, i in sorted(d["sources"].items())
    )

    pct = round(b.get("spent_share", 0) * 100)
    meter = "bar warn" if b.get("low") else "bar"
    credits = (
        f'<div class="{meter}"><span style="width:{min(100, pct)}%"></span></div>'
        if b.get("tracked")
        else ""
    )

    left = f"{b['remaining']:,} searches left" if b.get("tracked") else "searches untracked"
    pond_done = d["tasks"].get("completed", 0)
    pond_bad = d["tasks"].get("failed", 0)

    return _shell(
        f"""
<div class="adm">
  <div class="top">
    <h1>Foxy</h1>
    <div class="state">{state}</div>
  </div>

  <div class="figs">{figs}</div>

  {attention}

  <div class="sec">
    <h2>Sources</h2>
    <div class="srcs">{chips}</div>
  </div>

  <div class="sec">
    <h2>Workspaces</h2>
    {_rows(d["workspaces"], k)}
  </div>

  <div class="sec">
    <h2>Capacity</h2>
    {credits}
    <div class="foot" style="border:0;padding:0">
      <b>{left}</b> &middot; swept {_ago(_parse(d["last_sweep"]))}, next {d["next_sweep"]}
    </div>
  </div>

  <div class="foot">
    Pond &middot; <b>{d["pond_runs"]}</b> calls, <b>{pond_done}</b> scans
    {f", <b>{pond_bad}</b> failed" if pond_bad else ""}<br>
    {d["sweeps"]} sweeps &middot; last alert {_ago(d["last_alert"])}
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


def _rows(rows: list[dict], key: str) -> str:
    """One line per workspace. Name, state, count, and a way to act on it."""
    if not rows:
        return '<p class="none">Nobody has installed Foxy yet.</p>'

    out = ""
    for r in rows:
        count = (
            f'<span class="num">{r["used"]}/{r["quota"]}</span>'
            if r["quota"]
            else f'<span class="num">{r["used"]}</span>'
        )
        out += f"""
    <div class="row">
      <div class="who"><div class="nm">{html.escape(r["team"])}</div></div>
      <div class="rt">{count}{_chip(r)}{_buttons(r, key)}</div>
    </div>"""
    return f'<div class="rows">{out}</div>'


def _chip(r: dict) -> str:
    if not r["active"]:
        return '<span class="chip off">stopped</span>'
    if r["pro"]:
        return '<span class="chip pro">Pro</span>'
    if r["at_cap"]:
        return '<span class="chip cap">at cap</span>'
    return '<span class="chip free">Free</span>'


def _buttons(r: dict, key: str) -> str:
    """One form per action. A GET that changes a plan would be triggered by
    anything that follows links, a preview fetch included.

    Taking a plan away asks first. It sat as a bare button beside a harmless
    one and a stray click cost a live workspace its Pro plan - which nothing
    announced, because a downgrade looks exactly like a workspace that was
    never upgraded.
    """

    def form(months: int, label: str, cls: str, confirm: str = "") -> str:
        ask = (
            f' onsubmit="return confirm({html.escape(confirm, quote=True)!r})"'
            if confirm
            else ""
        )
        return f"""
      <form method="post" action="/admin/plan" style="display:inline"{ask}>
        <input type="hidden" name="key" value="{key}">
        <input type="hidden" name="install_id" value="{html.escape(r["id"])}">
        <input type="hidden" name="months" value="{months}">
        <button class="mini {cls}" type="submit">{label}</button>
      </form>"""

    if r["pro"]:
        return form(
            0,
            "Remove Pro",
            "",
            confirm=f"Remove Pro from {r['team']}? Alerts stop at the free cap.",
        )
    # A stopped workspace receives nothing, so offering it a plan is noise.
    if not r["active"]:
        return ""
    # One that has run out is the one worth acting on, so it leads.
    return form(12, "Give Pro", "go" if r["at_cap"] else "")


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
