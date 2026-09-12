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
from .db import Alert, PondRun, PondTask, Seen, health_snapshot, session
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
.bars{display:flex;align-items:flex-end;gap:6px;height:56px}
.bars div{flex:1;background:var(--sf);border-radius:4px 4px 2px 2px;position:relative;
min-height:3px;transition:background .15s}
.bars div.has{background:var(--accent)}
.bars div:hover{background:var(--accent2)}
.days{display:flex;gap:6px;margin-top:8px}
.days span{flex:1;text-align:center;font-size:10.5px;color:var(--dim);
font-family:"JetBrains Mono",monospace}

.sec{margin-bottom:38px}
.sec>h2{font-size:11px;font-weight:600;letter-spacing:.11em;text-transform:uppercase;
color:var(--dim);font-family:"JetBrains Mono",monospace;margin:0 0 14px}

.srcs{display:flex;flex-wrap:wrap;gap:8px}
.src{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);
border-radius:10px;padding:9px 14px;font-size:13px;color:var(--txt2);
background:var(--card)}
.src i{width:6px;height:6px;border-radius:50%;background:var(--up);font-style:normal;
flex:none}
.src i.warn{background:var(--warn)}
.src u{text-decoration:none;color:var(--dim);font-size:11.5px;
font-family:"JetBrains Mono",monospace}
.src em{font-style:normal;color:var(--brand);font-weight:600;font-size:12px}

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
        since = _utcnow() - dt.timedelta(days=7)
        recent = s.execute(
            select(Alert.created_at).where(
                Alert.ts.isnot(None), Alert.created_at >= since
            )
        ).scalars().all()

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

    days: list[dict[str, Any]] = []
    today = _utcnow().date()
    for back in range(6, -1, -1):
        day = today - dt.timedelta(days=back)
        days.append(
            {
                "label": day.strftime("%a")[:1],
                "date": day.isoformat(),
                "count": sum(1 for c in recent if c and c.date() == day),
            }
        )

    return {
        "ok": not problems,
        "week": days,
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
    bars = "".join(
        f'<div class="{"has" if x["count"] else ""}" '
        f'style="height:{max(3, round(x["count"] / peak * 56))}px" '
        f'title="{x["date"]}: {x["count"]}"></div>'
        for x in week
    )
    labels = "".join(f"<span>{x['label']}</span>" for x in week)
    week_total = sum(x["count"] for x in week)

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
        + f' <u>{_ago(_parse(i["ran_at"]))}</u></span>'
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
    pond_done = d["tasks"].get("completed", 0)
    pond_bad = d["tasks"].get("failed", 0)

    return _shell(
        f"""
<div class="adm">
  <div class="top">
    <h1>Foxy</h1>
    <div class="state{" warn" if not d["ok"] else ""}">{state}</div>
  </div>


  <div class="figs">{figs}</div>

  <div class="spark">
    <div class="hd"><b>Last 7 days</b><span>{week_total} delivered</span></div>
    <div class="bars">{bars}</div>
    <div class="days">{labels}</div>
  </div>

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

  <div class="sec">
    <h2>Controls</h2>
    <details class="tool">
      <summary>Send an announcement</summary>
      <form method="post" action="/admin/announce">
        <input type="hidden" name="key" value="{k}">
        <input type="text" name="message" placeholder="Goes to every active channel">
        <button class="mini go" type="submit">Send</button>
      </form>
    </details>
    <details class="tool">
      <summary>Replace the search key &middot; {html.escape(keys["serper_source"])}
        {html.escape(keys["serper_hint"])}</summary>
      <form method="post" action="/admin/key">
        <input type="hidden" name="key" value="{k}">
        <input type="text" name="serper" placeholder="New serper.dev API key">
        <button class="mini go" type="submit">Save</button>
      </form>
      <p class="hint">Free keys at serper.dev. Saved here, it takes effect on the
      next sweep &mdash; no deployment.</p>
    </details>
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
        out += f"""
    <div class="row">
      <div class="av">{html.escape(_initials(r["team"]))}</div>
      <div class="who">
        <div class="nm">{html.escape(r["team"])}</div>
        <div class="sub">{where} &middot; joined {r["joined"]:%d %b}</div>
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

    # A stopped workspace receives nothing, so every control is noise on it.
    if not r["active"]:
        return ""

    def act(path: str, label: str, cls: str, extra: str = "", confirm: str = "") -> str:
        ask = (
            f' onsubmit="return confirm({html.escape(confirm, quote=True)!r})"'
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

    stop = act(
        "stop",
        "Stop",
        "",
        confirm=f"Stop sending to {r['team']}? It keeps its history.",
    )

    if r["pro"]:
        return (
            form(
                0,
                "Remove Pro",
                "",
                confirm=f"Remove Pro from {r['team']}? Alerts stop at the free cap.",
            )
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
def replace_key(key: str = Form(""), serper: str = Form("")) -> HTMLResponse | RedirectResponse:
    """Replace the search key without a deployment.

    It is the one credential certain to need swapping one day, and needing a
    developer for that is how an agent quietly stops finding things.
    """
    if not _authorised(key):
        return _denied()
    value = (serper or "").strip()
    if value:
        runtime.set_serper_key(value)
    return RedirectResponse(f"/admin?key={key}", 303)


@router.post("/admin/grant", response_model=None)
def grant_alerts(
    key: str = Form(""), install_id: str = Form(""), alerts: int = Form(50)
) -> HTMLResponse | RedirectResponse:
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
    log.info("admin granted %s %d alerts", team, alerts)
    return RedirectResponse(f"/admin?key={key}", 303)


@router.post("/admin/stop", response_model=None)
def stop_workspace(
    key: str = Form(""), install_id: str = Form("")
) -> HTMLResponse | RedirectResponse:
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
        log.info("admin stopped %s", row.team_name)
    return RedirectResponse(f"/admin?key={key}", 303)


@router.post("/admin/announce", response_model=None)
def announce(
    key: str = Form(""), message: str = Form(""), install_id: str = Form("")
) -> HTMLResponse | RedirectResponse:
    """Say something in one channel, or in all of them.

    Failures are per workspace: one unreachable channel must not stop the rest
    from hearing it.
    """
    if not _authorised(key):
        return _denied()

    text = (message or "").strip()
    if not text:
        return RedirectResponse(f"/admin?key={key}", 303)

    with session() as s:
        rows = [
            (r.id, r.team_name, r.token, r.channel_id)
            for r in installs.active_installs(s)
            if not install_id or r.id == install_id
        ]

    sent = 0
    for _id, team, token, channel in rows:
        if _say(token, channel, text):
            sent += 1
        else:
            log.warning("could not announce to %s", team)
    log.info("admin announced to %d of %d channels", sent, len(rows))
    return RedirectResponse(f"/admin?key={key}", 303)


def _say(token: str, channel: str, text: str) -> bool:
    """One message, best effort. Never raises: this is never the main event."""
    if not token or not channel:
        return False
    try:
        SlackClient(token=token, target=channel).post(
            [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], text
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
