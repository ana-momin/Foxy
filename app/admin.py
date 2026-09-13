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

from . import budget, installs, runtime
from .config import settings
from .db import (
    Alert,
    PondRun,
    PondTask,
    Seen,
    health_snapshot,
    record,
    recent_events,
    session,
)
from .oauth import _page
from .slack import SlackClient

log = logging.getLogger("foxy.admin")

router = APIRouter()

# The sweep cron, kept here so "next due" is not a guess. Mirrors
# .github/workflows/hosted-sweep.yml — if that changes, change this.
SWEEP_HOURS = (0, 8, 16)

_CSS = """
/* The console stays light whatever the system says. It is read at a glance,
   often outdoors, and warm paper carries a wall of numbers better than a dark
   one. These come after the shared dark-mode block, so they win on order -
   which is the whole trick, and why the page was previously dark text on a
   dark ground. */
:root{
--bg:#FBF8F5;--surface:#fff;--ink:#171310;--ink2:#5C5249;--muted:#938779;
--border:#EDE6DE;--border2:#E0D6CA;--accent:#E1590C;--accent2:#FF7A38;
--sf:#FFF2E9;--good:#2E9E5B;--goodsf:#EAF6EF;
--sh:0 1px 2px rgba(40,25,10,.04),0 10px 30px -16px rgba(40,25,10,.18)}
body{background:#FBF8F5;color:#171310}
.w{max-width:840px}

.adm{--paper:#FBF8F5;--card:#fff;--line:#EFE8E1;--line2:#E2D9CE;
--txt:#171310;--txt2:#5C5249;--dim:#9A8E81;--brand:#E1590C;
--up:#2E9E5B;--warn:#D9822B}

.top{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.top h1{font-size:34px;letter-spacing:-.035em;margin:0;font-weight:600;color:var(--txt)}
.state{display:inline-flex;align-items:center;gap:9px;margin-left:auto;
font-size:13.5px;color:var(--txt2);background:var(--card);border:1px solid var(--line);
border-radius:999px;padding:7px 15px 7px 13px}
.state.warn{border-color:#F0D4AF;background:#FFFBF5;color:#8A5418}
.pip{width:7px;height:7px;border-radius:50%;background:var(--up);flex:none}
.pip.warn{background:var(--warn)}
.top{margin-bottom:34px}

.figs{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
@media(max-width:600px){.figs{grid-template-columns:repeat(2,1fr)}}
.fig{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 20px}
.fig b{display:block;font-size:32px;font-weight:600;letter-spacing:-.04em;
line-height:1;color:var(--txt);font-variant-numeric:tabular-nums}
.fig span{display:block;font-size:12px;color:var(--dim);margin-top:8px}

.spark{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 20px 14px;margin-bottom:44px}
.spark .hd{display:flex;align-items:baseline;gap:10px;margin-bottom:16px}
.spark .hd b{font-size:12px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;
color:var(--dim);font-family:"JetBrains Mono",monospace}
.spark .hd span{margin-left:auto;font-size:12.5px;color:var(--dim)}
.spark .hd span{margin-left:auto;font-size:12.5px;color:var(--dim);
display:inline-flex;align-items:baseline;gap:8px}
.spark .hd strong{color:var(--txt);font-weight:600;font-size:14px;
font-variant-numeric:tabular-nums}
.trend{font-size:11.5px;font-weight:600;padding:2px 7px;border-radius:999px;
font-family:"JetBrains Mono",monospace}
.trend.up{background:#EAF6EF;color:#2E7D4F}
.trend.down{background:#F4EFE9;color:var(--txt2)}

.bars{display:flex;align-items:flex-end;gap:7px;height:62px}
.col{flex:1;display:flex;align-items:flex-end;justify-content:center;height:100%;
border-radius:5px;padding-bottom:0;transition:background .15s}
.col:hover{background:var(--sf)}
.col.now .stack{outline:2px solid var(--card);outline-offset:-2px;
box-shadow:0 0 0 2px var(--brand)}
/* Listed underneath, early stacked on top: the split is the whole point and a
   single bar hides it. */
.stack{display:flex;flex-direction:column;justify-content:flex-start;width:100%;
max-width:34px;background:#F0DFCD;border-radius:5px 5px 3px 3px;overflow:hidden}
.stack i{display:block;width:100%;background:var(--brand);border-radius:5px 5px 0 0}

.days{display:flex;gap:7px;margin-top:9px}
.days span{flex:1;text-align:center;font-size:10.5px;color:var(--dim);
font-family:"JetBrains Mono",monospace}
.days span.now{color:var(--brand);font-weight:600}

.legend{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:16px;
padding-top:14px;border-top:1px solid var(--line);font-size:12px;color:var(--txt2)}
.legend span{display:inline-flex;align-items:center;gap:7px}
.legend i{width:9px;height:9px;border-radius:3px}
.legend .k-e{background:var(--brand)}
.legend .k-l{background:#F0DFCD}
.legend em{margin-left:auto;font-style:normal;color:var(--dim);font-size:11.5px}

.sec{margin-bottom:38px}
.sec>h2{font-size:11px;font-weight:600;letter-spacing:.11em;text-transform:uppercase;
color:var(--dim);font-family:"JetBrains Mono",monospace;margin:0 0 14px}

.srcs{border:1px solid var(--line);border-radius:14px;background:var(--card);
overflow:hidden}
.src{display:grid;grid-template-columns:auto 1fr auto auto;align-items:center;
gap:12px;padding:13px 18px;border-top:1px solid var(--line);font-size:13.5px}
.srcs>.src:first-child{border-top:0}
.src i{width:6px;height:6px;border-radius:50%;background:var(--up);font-style:normal}
.src i.warn{background:var(--warn)}
.src .src-n{color:var(--txt);font-weight:500}
.src .src-c{color:var(--brand);font-weight:600;font-size:12.5px;
font-family:"JetBrains Mono",monospace;min-width:38px;text-align:right}
.src .src-t{color:var(--dim);font-size:11.5px;font-family:"JetBrains Mono",monospace;
min-width:56px;text-align:right}

.att{border:1px solid #F0D4AF;border-radius:14px;overflow:hidden;background:#FFFBF5}
.rows{border:1px solid var(--line);border-radius:14px;background:var(--card);
overflow:hidden}
.row{display:flex;align-items:center;gap:14px;padding:16px 18px;
border-top:1px solid var(--line)}
.rows>.row:first-child,.att>.row:first-child{border-top:0}
.att .row{border-top-color:#F5E3CB}
.av{width:34px;height:34px;border-radius:10px;flex:none;display:grid;place-items:center;
background:var(--sf);color:var(--brand);font-size:13px;font-weight:600;
font-family:"JetBrains Mono",monospace}
.row .who{min-width:0;flex:1}
.row .nm{font-size:14.5px;font-weight:600;color:var(--txt);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .sub{font-size:12.5px;color:var(--dim);margin-top:2px}
/* How much of the allowance is gone, on the row rather than in a number
   somebody has to divide in their head. */
.use{height:3px;border-radius:2px;background:var(--line);overflow:hidden;
margin-top:9px;max-width:200px}
.use span{display:block;height:100%;background:var(--brand);border-radius:2px}
.use span.full{background:var(--warn)}
.row .rt{display:flex;align-items:center;gap:9px;flex:none}
.num{font-size:13px;color:var(--dim);font-family:"JetBrains Mono",monospace;
font-variant-numeric:tabular-nums}

.chip{font-size:11px;font-weight:600;padding:4px 10px;border-radius:999px;
white-space:nowrap}
.chip.pro{background:var(--brand);color:#fff}
.chip.free{background:#F4EFE9;color:var(--txt2)}
.chip.cap{background:#FBE6CC;color:#8A5418}
.chip.off{background:transparent;color:var(--dim);border:1px solid var(--line2)}

.mini{border:1px solid var(--line2);background:var(--card);color:var(--txt2);
border-radius:9px;padding:7px 13px;font-size:12.5px;font-weight:500;cursor:pointer;
font-family:inherit;white-space:nowrap;transition:border-color .15s,color .15s}
.mini:hover{border-color:var(--brand);color:var(--brand)}
.mini.go{background:var(--brand);border-color:var(--brand);color:#fff}
.mini.go:hover{background:#C94D08}

.bar{height:5px;border-radius:3px;background:var(--line);overflow:hidden;
margin-bottom:12px}
.bar span{display:block;height:100%;background:var(--brand);border-radius:3px}
.bar.warn span{background:var(--warn)}

.foot{font-size:13px;color:var(--dim);line-height:1.9;border-top:1px solid var(--line);
padding-top:20px;margin-top:6px}
.foot b{color:var(--txt2);font-weight:500;font-variant-numeric:tabular-nums}
.none{color:var(--dim);font-size:13.5px}

.tool{border:1px solid var(--line);border-radius:14px;background:var(--card);
margin-bottom:9px;padding:0 18px}
.tool summary{cursor:pointer;padding:15px 0;font-size:14px;color:var(--txt2);
list-style:none;display:flex;align-items:center;gap:9px}
.tool summary::-webkit-details-marker{display:none}
.tool summary:before{content:"+";color:var(--dim);font-size:15px;width:12px}
.tool[open] summary:before{content:"2"}
.tool[open] summary{color:var(--txt);font-weight:500}
.tool form{display:flex;gap:8px;padding:0 0 16px;flex-wrap:wrap}
.tool input[type=text]{flex:1;min-width:210px;padding:10px 13px;border-radius:9px;
border:1px solid var(--line2);background:var(--paper);color:var(--txt);
font-family:inherit;font-size:13.5px}
.tool input[type=text]:focus{outline:2px solid var(--brand);outline-offset:1px}
.tool .hint{font-size:12.5px;color:var(--dim);margin:0 0 16px;line-height:1.55}
.tool form{display:block;padding-bottom:18px}
.tool label{display:block;font-size:12.5px;font-weight:600;color:var(--txt2);
margin:0 0 14px}
.tool label span{font-weight:400;color:var(--dim)}
.tool label input,.tool label select,.tool label textarea{display:block;width:100%;
margin-top:6px;padding:10px 12px;border-radius:9px;border:1px solid var(--line2);
background:var(--paper);color:var(--txt);font-family:inherit;font-size:13.5px}
.tool textarea{resize:vertical;line-height:1.55}
.tool label input:focus,.tool label select:focus,.tool label textarea:focus{
outline:2px solid var(--brand);outline-offset:1px}
.grid2{display:grid;gap:14px}
@media(min-width:560px){.grid2{grid-template-columns:1fr 1fr}}
.keystate{font-style:normal;margin-left:auto;font-size:12px;color:var(--dim);
font-family:"JetBrains Mono",monospace}

/* What the channel will actually see, updating as it is typed. Guessing at
   Slack's rendering from a plain textarea is how a message goes out with a
   stray asterisk in it. */
.prev{margin:2px 0 16px}
.prev-h{font-size:11px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;
color:var(--dim);font-family:"JetBrains Mono",monospace;margin-bottom:9px}
.slackmsg{display:flex;gap:11px;background:#fff;border:1px solid var(--line);
border-radius:12px;padding:14px 15px}
.sm-av{width:34px;height:34px;border-radius:9px;background:var(--sf);flex:none;
display:grid;place-items:center;font-size:17px}
.sm-body{min-width:0;flex:1}
.sm-who{font-size:13px;margin-bottom:7px}
.sm-who b{font-weight:600;color:var(--txt)}
.sm-who span{font-size:10px;font-weight:600;background:var(--line);color:var(--txt2);
padding:1px 5px;border-radius:3px;margin-left:6px;letter-spacing:.03em}
.sm-head{font-size:14px;font-weight:600;color:var(--txt)}
.sm-rule{height:1px;background:var(--line);margin:10px 0}
.sm-text{font-size:13.5px;color:var(--txt2);line-height:1.6;white-space:pre-wrap;
word-break:break-word;min-height:20px}
.sm-text em{font-style:italic}.sm-text b{color:var(--txt);font-weight:600}
.sm-btn{display:inline-block;margin-top:12px;background:#007a5a;color:#fff;
border-radius:6px;padding:8px 14px;font-size:13px;font-weight:600;text-decoration:none}
.sm-foot{font-size:11.5px;color:var(--dim);margin-top:12px}
.tool select{padding:10px 12px;border-radius:9px;border:1px solid var(--line2);
background:var(--paper);color:var(--txt);font-family:inherit;font-size:13.5px}

.mark{width:38px;height:38px;border-radius:11px;flex:none;object-fit:cover;
box-shadow:var(--sh)}
.ico{margin-left:8px;width:36px;height:36px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--txt2);display:grid;place-items:center;cursor:pointer;
text-decoration:none;transition:border-color .15s,color .15s}
.ico:hover{border-color:var(--brand);color:var(--brand)}
.ico svg{width:16px;height:16px}

/* Replaces the browser's confirm(), which announces the hostname and looks
   like a phishing prompt on the one screen that changes billing. */
.veil{position:fixed;inset:0;background:rgba(23,19,16,.38);backdrop-filter:blur(3px);
display:none;place-items:center;z-index:40;padding:22px}
.veil.on{display:grid}
.ask{background:var(--card);border-radius:16px;padding:26px;max-width:400px;width:100%;
box-shadow:0 24px 60px -20px rgba(30,20,10,.4);border:1px solid var(--line)}
.ask h3{margin:0 0 8px;font-size:17px;font-weight:600;color:var(--txt)}
.ask p{margin:0 0 22px;font-size:14px;color:var(--txt2);line-height:1.55}
.ask .btns{display:flex;gap:9px;justify-content:flex-end}

.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,80px);
background:var(--txt);color:#fff;padding:12px 20px;border-radius:11px;font-size:13.5px;
box-shadow:0 16px 40px -14px rgba(0,0,0,.5);opacity:0;transition:all .22s ease;z-index:50}
.toast.on{transform:translate(-50%,0);opacity:1}

.busy{opacity:.5;pointer-events:none}

.logs{border:1px solid var(--line);border-radius:14px;background:var(--card);
overflow:hidden}
.logday{font-size:11px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;
color:var(--dim);font-family:"JetBrains Mono",monospace;padding:14px 18px 9px;
background:var(--paper);border-top:1px solid var(--line)}
.logs>.logday:first-child{border-top:0}

.log{border-top:1px solid var(--line)}
.log summary{display:grid;grid-template-columns:52px 74px 1fr auto;align-items:center;
gap:12px;padding:12px 18px;cursor:pointer;list-style:none;font-size:13.5px}
.log summary::-webkit-details-marker{display:none}
.log summary:hover{background:var(--paper)}
.log .when{color:var(--dim);font-size:11.5px;font-family:"JetBrains Mono",monospace}
.log .kd{font-size:10px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
font-family:"JetBrains Mono",monospace;padding:3px 7px;border-radius:5px;
background:var(--line);color:var(--txt2);text-align:center}
/* Foxy's own work and an operator's decisions read differently, so they should
   not look the same in a list of both. */
.log .kd.scan,.log .kd.pond{background:#EAF1F7;color:#3A6EA5}
.log .kd.sweep{background:#EAF6EF;color:#2E7D4F}
.log .kd.grant,.log .kd.plan{background:var(--sf);color:var(--brand)}
.log .kd.stop,.log .kd.key{background:#FBE6CC;color:#8A5418}
.log .what{color:var(--txt);font-weight:500;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.log .by{color:var(--dim);font-size:11.5px}
.log.bad .what{color:var(--warn)}

.logmore{padding:2px 18px 16px;display:grid;gap:7px;font-size:13px;color:var(--txt2);
background:var(--paper)}
.logmore div{display:grid;grid-template-columns:74px 1fr;gap:12px}
.logmore b{font-weight:500;color:var(--dim);font-size:11.5px;
font-family:"JetBrains Mono",monospace;padding-top:1px}
@media(max-width:600px){
  .log summary{grid-template-columns:48px 1fr;row-gap:4px}
  .log .by{grid-column:2}
}
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


def _done(key: str, wants_json: bool, ok: bool = True, **extra: Any) -> Any:
    """Answer an action.

    A browser form gets a redirect; the page's own fetch gets JSON and updates
    in place. Both paths exist so the console still works with no JavaScript,
    and so a full reload is not the price of pressing a button.
    """
    if wants_json:
        return JSONResponse({"ok": ok, **extra})
    return RedirectResponse(f"/admin?key={key}", 303)


def _denied() -> HTMLResponse:
    return _shell(
        "<h1>Not available</h1><p class='lede'>This page needs a valid key.</p>",
        "Foxy",
    )


# ---------------------------------------------------------------------------
# measuring
# ---------------------------------------------------------------------------


def _utcnow() -> dt.datetime:
    """Naive UTC, matching how the database stores its timestamps."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _ago(when: dt.datetime | None) -> str:
    if when is None:
        return "never"
    if when.tzinfo is not None:
        when = when.astimezone(dt.timezone.utc).replace(tzinfo=None)
    mins = (_utcnow() - when).total_seconds() / 60
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

        # A week of delivery, so the shape of the last few days is visible
        # rather than only the running total. Counted in Python because the
        # date functions differ between SQLite and Postgres, and a week of rows
        # is nothing to read.
        # Fourteen days, not seven: the second week is never drawn, it is what
        # the first is compared against. A total with nothing beside it cannot
        # say whether things are picking up or dying off.
        since = _utcnow() - dt.timedelta(days=14)
        recent = s.execute(
            select(Alert.created_at, Alert.kind).where(
                Alert.ts.isnot(None), Alert.created_at >= since
            )
        ).all()

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
                    "channel_name": r.channel_name,
                    "joined": r.created_at,
                    "bonus": r.bonus_alerts or 0,
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
    def count(n: int, one: str, many: str) -> str:
        """English, rather than "1 install(s)". It is the first line anyone
        reads on the page and it should not look generated."""
        return f"{n} {one}" if n == 1 else f"{n} {many}"

    if failing:
        problems.append(count(len(failing), "source is failing", "sources are failing"))
    stalled = [r for r in live if r["at_cap"]]
    if stalled:
        problems.append(
            count(len(stalled), "workspace has", "workspaces have") + " run out of alerts"
        )
    unfinished = [r for r in live if r["no_channel"]]
    if unfinished:
        problems.append(
            count(len(unfinished), "workspace has", "workspaces have")
            + " not chosen a channel"
        )
    if tasks.get("failed"):
        problems.append(count(tasks["failed"], "Pond scan failed", "Pond scans failed"))
    b = budget.snapshot()
    if b.get("low"):
        problems.append("search credits low")

    days: list[dict[str, Any]] = []
    today = _utcnow().date()
    for back in range(6, -1, -1):
        day = today - dt.timedelta(days=back)
        on_day = [k for c, k in recent if c and c.date() == day]
        early = sum(1 for k in on_day if k == "early")
        days.append(
            {
                "label": day.strftime("%a")[:1],
                "date": day.strftime("%a %d %b"),
                "count": len(on_day),
                "early": early,
                # Everything that is not an early catch is a confirmed listing.
                "listed": len(on_day) - early,
                "today": back == 0,
            }
        )

    this_week = sum(d["count"] for d in days)
    week_ago = today - dt.timedelta(days=7)
    last_week = sum(
        1 for c, _ in recent if c and week_ago - dt.timedelta(days=7) <= c.date() < week_ago
    )
    busiest = max(days, key=lambda d: d["count"]) if days else None

    return {
        "ok": not problems,
        "week": days,
        "week_total": this_week,
        "week_before": last_week,
        "week_early": sum(d["early"] for d in days),
        "busiest": busiest,
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
        "keys": runtime.snapshot(),
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

    week = d["week"]
    peak = max((x["count"] for x in week), default=0) or 1

    # Stacked: early catches sit on top of confirmed listings, because the
    # split is the whole point of Foxy and a single bar hides it.
    bars = ""
    for x in week:
        h = round(x["count"] / peak * 62) if x["count"] else 0
        early_h = round(x["early"] / peak * 62) if x["early"] else 0
        tip = f'{x["date"]} · {x["count"]} delivered'
        if x["early"]:
            tip += f', {x["early"]} early'
        bars += (
            f'<div class="col{" now" if x["today"] else ""}" title="{html.escape(tip)}">'
            f'<span class="stack" style="height:{max(h, 2)}px">'
            f'<i class="e" style="height:{early_h}px"></i>'
            "</span></div>"
        )

    labels = "".join(
        f"<span{_now_attr(x)}>{x['label']}</span>"
        for x in week
    )

    total, before = d["week_total"], d["week_before"]
    if before:
        change = round((total - before) / before * 100)
        trend = (
            f'<span class="trend {"up" if change >= 0 else "down"}">'
            f'{"+" if change > 0 else ""}{change}%</span>'
        )
    else:
        trend = ""

    busiest = d["busiest"]
    footnote = ""
    if busiest and busiest["count"]:
        footnote = (
            f'Busiest {html.escape(busiest["date"])} with {busiest["count"]}'
            f' &middot; {total / 7:.1f} a day'
        )

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
    # A grid rather than a row of pills: six items at different widths wrapped
    # into a ragged block where nothing lined up and the counts were hard to
    # compare. Aligned columns make the odd one out obvious.
    chips = "".join(
        '<div class="src">'
        f'<i class="{"" if i["ok"] else "warn"}"></i>'
        f'<span class="src-n">{html.escape(name.replace("_", " "))}</span>'
        f'<span class="src-c">{("+" + str(i["new"])) if i["ok"] and i["new"] else ""}</span>'
        f'<span class="src-t">{_ago(_parse(i["ran_at"]))}</span>'
        "</div>"
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
    keys = d["keys"]
    targets = "".join(
        f'<option value="{html.escape(w["id"])}">{html.escape(w["team"])}'
        + (f' &middot; #{html.escape(w["channel_name"])}' if w["channel_name"] else "")
        + "</option>"
        for w in d["workspaces"]
        if w["active"] and w["channel"]
    )
    pond_done = d["tasks"].get("completed", 0)
    pond_bad = d["tasks"].get("failed", 0)

    return _shell(
        f"""
<div class="adm">
  <div class="top">
    <img class="mark" src="/assets/foxy.png" alt="" width="38" height="38">
    <h1>Foxy</h1>
    <div class="state{" warn" if not d["ok"] else ""}" id="state">{state}</div>
    <a class="ico" href="/admin/logs?key={k}" title="Activity log" aria-label="Activity log">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M3 12h4l2.5-7 5 14 2.5-7h4"/>
      </svg>
    </a>
  </div>


  <div class="figs" id="figs">{figs}</div>

  <div class="spark">
    <div class="hd">
      <b>Last 7 days</b>
      <span><strong>{total}</strong> delivered{trend}</span>
    </div>
    <div class="bars">{bars}</div>
    <div class="days">{labels}</div>
    <div class="legend">
      <span><i class="k-e"></i>{d["week_early"]} early</span>
      <span><i class="k-l"></i>{total - d["week_early"]} listed</span>
      <em>{footnote}</em>
    </div>
  </div>

  {attention}

  <div class="sec">
    <h2>Sources</h2>
    <div class="srcs">{chips}</div>
  </div>

  <div class="sec">
    <h2>Workspaces</h2>
    <div id="wsp">{_rows(d["workspaces"], k)}</div>
  </div>

  <div class="sec">
    <h2>Capacity</h2>
    <div id="cap">{credits}
    <div class="foot" style="border:0;padding:0">
      <b>{left}</b> &middot; swept {_ago(_parse(d["last_sweep"]))}, next {d["next_sweep"]}
    </div></div>
  </div>

  <div class="sec">
    <h2>Controls</h2>
    <details class="tool">
      <summary>Send an announcement</summary>
      <form method="post" action="/admin/announce" id="annc">
        <input type="hidden" name="key" value="{k}">
        <div class="grid2">
          <label>To
            <select name="install_id" id="a-to">
              <option value="">Every active channel</option>
              {targets}
            </select>
          </label>
          <label>Style
            <select name="tone" id="a-tone">
              <option value="news">Announcement</option>
              <option value="update">What&#39;s new</option>
              <option value="heads-up">Heads up</option>
              <option value="thanks">From Foxy</option>
              <option value="gift">Good news</option>
            </select>
          </label>
        </div>
        <label>Heading <span>optional &mdash; replaces the default</span>
          <input type="text" name="title" id="a-title" placeholder="Announcement">
        </label>
        <label>Message
          <textarea name="message" id="a-msg" rows="3"
            placeholder="Supports *bold*, _italic_ and links."></textarea>
        </label>
        <div class="grid2">
          <label>Button text <span>optional</span>
            <input type="text" name="link_label" id="a-blab" placeholder="Read more">
          </label>
          <label>Button link <span>optional</span>
            <input type="text" name="link_url" id="a-burl" placeholder="https://">
          </label>
        </div>

        <div class="prev" id="a-prev" aria-hidden="true">
          <div class="prev-h">Preview</div>
          <div class="slackmsg">
            <div class="sm-av">&#129418;</div>
            <div class="sm-body">
              <div class="sm-who"><b>Foxy</b><span>APP</span></div>
              <div class="sm-head" id="p-head"></div>
              <div class="sm-rule"></div>
              <div class="sm-text" id="p-text"></div>
              <a class="sm-btn" id="p-btn" hidden></a>
            </div>
          </div>
        </div>

        <button class="mini go" type="submit">Send</button>
      </form>
    </details>

    <details class="tool">
      <summary>Search key
        <em class="keystate">{html.escape(keys["serper_source"])}
        {html.escape(keys["serper_hint"])}</em>
      </summary>
      <form method="post" action="/admin/key">
        <input type="hidden" name="key" value="{k}">
        <label>New serper.dev key
          <input type="text" name="serper" placeholder="Paste it here">
        </label>
        <button class="mini go" type="submit">Check and save</button>
      </form>
      <p class="hint">It is tried against serper before it is stored, so a dud
      cannot replace a working one. Takes effect on the next sweep &mdash; no
      deployment. Free keys at <b>serper.dev</b>.</p>
    </details>
  </div>

  <div class="veil" id="veil">
    <div class="ask" role="dialog" aria-modal="true">
      <h3></h3>
      <p></p>
      <div class="btns">
        <button class="mini no" type="button">Cancel</button>
        <button class="mini go" type="button">Yes, do it</button>
      </div>
    </div>
  </div>
  <div class="toast" id="toast"></div>
  <script src="/assets/admin.js" defer></script>
  <script src="/assets/preview.js" defer></script>

  <div class="foot">
    Pond &middot; <b>{d["pond_runs"]}</b> calls, <b>{pond_done}</b> scans
    {f", <b>{pond_bad}</b> failed" if pond_bad else ""}<br>
    {d["sweeps"]} sweeps &middot; last alert {_ago(d["last_alert"])}
  </div>
</div>""",
        "Foxy status",
    )


def _day_label(when: dt.datetime) -> str:
    """Today and Yesterday by name; everything older by date."""
    today = _utcnow().date()
    on = when.date()
    if on == today:
        return "Today"
    if on == today - dt.timedelta(days=1):
        return "Yesterday"
    return when.strftime("%A %d %B")


def _now_attr(day: dict) -> str:
    """Marks today's column.

    Kept out of the f-string because a backslash inside an f-string expression
    is a syntax error before Python 3.12 - which the local interpreter allowed
    and CI did not.
    """
    return ' class="now"' if day.get("today") else ""


def _parse(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _initials(name: str) -> str:
    """Two letters at most, so a row is identifiable before it is read."""
    parts = [w for w in name.replace("-", " ").split() if w]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


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
        where = (
            f'#{html.escape(r["channel_name"])}'
            if r["channel_name"]
            else ("no channel" if r["no_channel"] else html.escape(r["channel"]))
        )
        pct = (
            min(100, round(r["used"] * 100 / r["quota"])) if r["quota"] else 100
        )
        meter = (
            f'<div class="use"><span style="width:{pct}%" '
            f'class="{"full" if r["at_cap"] else ""}"></span></div>'
            if r["active"]
            else ""
        )
        last = _ago(r["last_alert"]) if r["last_alert"] else "no alerts yet"
        out += f"""
    <div class="row">
      <div class="av">{html.escape(_initials(r["team"]))}</div>
      <div class="who">
        <div class="nm">{html.escape(r["team"])}</div>
        <div class="sub">{where} &middot; {last}</div>
        {meter}
      </div>
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


# Filled as the questions are built, read by the dialog on the page.
_DETAIL: dict[str, str] = {}


def _remove_q(team: str) -> str:
    q = f"Remove Pro from {team}?"
    _DETAIL[q] = "Alerts stop once it reaches the free cap again."
    return q


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
            f' data-ask="{html.escape(confirm, quote=True)}"'
            f' data-detail="{html.escape(_DETAIL.get(confirm, ""), quote=True)}"'
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

    # A stopped workspace receives nothing, so every control is noise on it.
    if not r["active"]:
        return ""

    def act(path: str, label: str, cls: str, extra: str = "", confirm: str = "") -> str:
        ask = (
            f' data-ask="{html.escape(confirm, quote=True)}"'
            f' data-detail="{html.escape(_DETAIL.get(confirm, ""), quote=True)}"'
            if confirm
            else ""
        )
        return f"""
      <form method="post" action="/admin/{path}" style="display:inline"{ask}>
        <input type="hidden" name="key" value="{key}">
        <input type="hidden" name="install_id" value="{html.escape(r["id"])}">
        {extra}
        <button class="mini {cls}" type="submit">{label}</button>
      </form>"""

    stop_q = f"Stop sending to {r['team']}?"
    _DETAIL[stop_q] = "It keeps everything it has received. Reinstalling resumes it."
    stop = act("stop", "Stop", "", confirm=stop_q)

    if r["pro"]:
        return (
            form(0, "Remove Pro", "", confirm=_remove_q(r["team"]))
            + stop
        )

    give = act("grant", "+50", "", '<input type="hidden" name="alerts" value="50">')
    # A workspace that has run out is the one worth acting on, so it leads.
    return give + form(12, "Give Pro", "go" if r["at_cap"] else "") + stop


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


@router.get("/admin/logs", response_model=None)
def logs(key: str = "") -> HTMLResponse:
    """What has happened, newest first.

    Sweeps and deliveries leave their own trail; this is for the changes that
    otherwise leave none - somebody granting alerts, replacing a key, sending
    an announcement. A workspace lost its plan to a stray click once and there
    was no way to say when or by whom.
    """
    if not _authorised(key):
        return _denied()

    with session() as s:
        rows = [
            {
                "at": e.at,
                "kind": e.kind,
                "actor": e.actor,
                "subject": e.subject,
                "detail": e.detail,
                "ok": e.ok,
            }
            for e in recent_events(s, limit=150)
        ]

    if rows:
        items, day = "", None
        for r in rows:
            on = r["at"].strftime("%d %b")
            if on != day:
                day = on
                items += f'<div class="logday">{html.escape(_day_label(r["at"]))}</div>'
            items += f"""
    <details class="log{"" if r["ok"] else " bad"}">
      <summary>
        <span class="when">{r["at"]:%H:%M}</span>
        <span class="kd {html.escape(r["kind"])}">{html.escape(r["kind"])}</span>
        <span class="what">{html.escape(r["subject"] or r["kind"])}</span>
        <span class="by">{html.escape(r["actor"])}</span>
      </summary>
      <div class="logmore">
        <div><b>When</b>{r["at"]:%d %b %Y, %H:%M:%S} UTC</div>
        <div><b>What</b>{html.escape(r["kind"])}</div>
        <div><b>Who</b>{html.escape(r["actor"])}</div>
        <div><b>Subject</b>{html.escape(r["subject"] or "-")}</div>
        <div><b>Detail</b>{html.escape(r["detail"] or "-")}</div>
        <div><b>Outcome</b>{"succeeded" if r["ok"] else "failed"}</div>
      </div>
    </details>"""
        body = f'<div class="logs">{items}</div>'
    else:
        body = '<p class="none">Nothing has happened yet.</p>'

    return _shell(
        f"""
<div class="adm">
  <div class="top">
    <img class="mark" src="/assets/foxy.png" alt="" width="38" height="38">
    <h1>Activity</h1>
    <a class="ico" href="/admin?key={html.escape(key)}" title="Back"
       aria-label="Back" style="margin-left:auto">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>
    </a>
  </div>
  <div class="sec">{body}</div>
</div>""",
        "Foxy activity",
    )


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


@router.post("/admin/key", response_model=None)
def replace_key(
    key: str = Form(""), serper: str = Form(""), ajax: str = Form("")
) -> Any:
    """Replace the search key without a deployment.

    It is the one credential certain to need swapping one day, and needing a
    developer for that is how an agent quietly stops finding things.
    """
    if not _authorised(key):
        return _denied()
    value = (serper or "").strip()
    if not value:
        return _done(key, bool(ajax), ok=False, message="Nothing to save")

    # Tried before it is trusted. A key stored without checking looks saved and
    # fails silently on the next sweep, hours later, where nobody is watching.
    working, why = _key_works(value)
    if not working:
        record("key", subject="serper", detail=why, actor="admin", ok=False)
        return _done(key, bool(ajax), ok=False, message=f"Not saved: {why}")

    runtime.set_serper_key(value)
    record("key", subject="serper", detail="search key replaced and verified",
           actor="admin")
    return _done(key, bool(ajax), message="Key verified and saved")


def _key_works(value: str) -> tuple[bool, str]:
    """Ask serper whether this key is good for anything."""
    from .sources.base import client

    try:
        with client(headers={"X-API-KEY": value,
                             "Content-Type": "application/json"}) as c:
            r = c.post("https://google.serper.dev/search",
                       json={"q": "y combinator", "num": 10})
    except Exception as exc:  # noqa: BLE001
        return False, f"could not reach serper ({type(exc).__name__})"

    if r.status_code in (401, 403):
        return False, "serper rejected the key"
    if r.status_code == 429:
        return False, "the key has no credits left"
    if r.status_code >= 400:
        return False, f"serper answered {r.status_code}"
    return True, "ok"


@router.post("/admin/grant", response_model=None)
def grant_alerts(
    key: str = Form(""),
    install_id: str = Form(""),
    alerts: int = Form(50),
    ajax: str = Form(""),
) -> Any:
    """Give a workspace more headroom, and tell it so.

    Silently raising a cap leaves somebody wondering why the bot went quiet and
    then why it did not, so the workspace hears about it in its own channel.
    """
    if not _authorised(key):
        return _denied()

    with session() as s:
        row = installs.get(s, install_id)
        if row is None:
            return _denied()
        installs.grant(row, alerts)
        token, channel, team = row.token, row.channel_id, row.team_name
        left = row.remaining

    if token and channel:
        _say(token, channel, f"*{alerts} more alerts added.* {left} left on this channel.")
    record("grant", subject=team, detail=f"+{alerts} alerts, {left} remaining", actor="admin")
    log.info("admin granted %s %d alerts", team, alerts)
    return _done(key, bool(ajax), message=f"{alerts} alerts added to {team}")


@router.post("/admin/stop", response_model=None)
def stop_workspace(
    key: str = Form(""), install_id: str = Form(""), ajax: str = Form("")
) -> Any:
    """Stop delivering to a workspace. Deactivated rather than deleted: the
    seen-set and the record of what it received stay true, and a reinstall
    picks up where it left off."""
    if not _authorised(key):
        return _denied()

    with session() as s:
        row = installs.get(s, install_id)
        if row is None:
            return _denied()
        row.active = False
        team = row.team_name
    record("stop", subject=team, detail="delivery stopped", actor="admin")
    log.info("admin stopped %s", team)
    return _done(key, bool(ajax), message=f"Stopped {team}")


@router.post("/admin/announce", response_model=None)
def announce(
    key: str = Form(""),
    message: str = Form(""),
    install_id: str = Form(""),
    tone: str = Form("news"),
    title: str = Form(""),
    link_label: str = Form(""),
    link_url: str = Form(""),
    ajax: str = Form(""),
) -> Any:
    """Say something in one channel, or in all of them.

    Failures are per workspace: one unreachable channel must not stop the rest
    from hearing it.
    """
    if not _authorised(key):
        return _denied()

    text = (message or "").strip()
    if not text:
        return _done(key, bool(ajax), message="Nothing to say")

    with session() as s:
        rows = [
            (r.id, r.team_name, r.token, r.channel_id)
            for r in installs.active_installs(s)
            if not install_id or r.id == install_id
        ]

    blocks, fallback = _announcement(
        text, tone, title=title, link_label=link_label, link_url=link_url
    )
    sent = 0
    for _id, team, token, channel in rows:
        if _say(token, channel, fallback, blocks=blocks):
            sent += 1
        else:
            log.warning("could not announce to %s", team)
    where = rows[0][1] if len(rows) == 1 else f"{sent}/{len(rows)} channels"
    record(
        "announce",
        subject=where,
        detail=text,
        actor="admin",
        ok=sent == len(rows),
    )
    log.info("admin announced to %d of %d channels", sent, len(rows))
    return _done(
        key, bool(ajax), message=f"Sent to {sent} of {len(rows)} channels"
    )


TONES = {
    "news":     (":loudspeaker:", "Announcement"),
    "update":   (":sparkles:", "What's new"),
    "heads-up": (":warning:", "Heads up"),
    "thanks":   (":wave:", "From Foxy"),
    "gift":     (":tada:", "Good news"),
}


def _announcement(
    text: str,
    tone: str,
    *,
    title: str = "",
    link_label: str = "",
    link_url: str = "",
) -> tuple[list[dict], str]:
    """Dress an announcement so it does not read like an alert.

    A bare line of text in a channel full of company alerts looks like another
    detection. A header, a rule and a quiet footer say, before a word is read,
    that this one came from a person.
    """
    icon, default_title = TONES.get(tone, TONES["news"])
    heading = (title or default_title).strip()

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{icon}  *{heading}*"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]

    # An announcement that asks for something should carry the way to do it.
    if link_url.strip() and link_label.strip():
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": link_label.strip()[:74],
                            "emoji": True,
                        },
                        "url": link_url.strip(),
                        "style": "primary",
                    }
                ],
            }
        )

    # No footer. It promised that a reply would be read, which is not
    # something this can keep, and it repeated on every announcement until the
    # line became furniture rather than information.
    return blocks, f"{heading}: {text}"


def _say(token: str, channel: str, text: str, blocks: list[dict] | None = None) -> bool:
    """One message, best effort. Never raises: this is never the main event."""
    if not token or not channel:
        return False
    try:
        SlackClient(token=token, target=channel).post(
            blocks or [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            text,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("message to %s failed", channel, exc_info=True)
        return False


@router.post("/admin/plan", response_model=None)
def set_plan(
    key: str = Form(""),
    install_id: str = Form(""),
    months: int = Form(1),
    ajax: str = Form(""),
) -> Any:
    if not _authorised(key):
        return _denied()

    with session() as s:
        row = installs.get(s, install_id)
        if row is None:
            return _denied()
        team = row.team_name
        if months <= 0:
            installs.downgrade(row)
            note = "Pro removed"
        else:
            installs.activate(row, months)
            note = f"Pro for {months} month(s)"
    record("plan", subject=team, detail=note, actor="admin")
    log.info("admin: %s - %s", team, note)
    return _done(key, bool(ajax), message=f"{team}: {note.lower()}")
