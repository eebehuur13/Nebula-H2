"""The admin page at /admin: who uses Nebula, what they ask, what's being indexed, and the controls
that make Nebula check Drive, read files and refresh right away.

Only Nebula admins can open it. It signs in with the same Google cookie as the /web pages.
The page is static HTML, CSS and JavaScript (in admin_ui/) that polls /admin/api/state.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from nebula import access, activity, db, jobs
from nebula.knowledge import stages
from nebula.config import settings
from nebula.server import mcp

log = logging.getLogger(__name__)

UI = Path(__file__).parent / "admin_ui"
IST = ZoneInfo("Asia/Kolkata")
UTC = dt.UTC

# Time ranges: how far back, and the width of each bar in the charts.
RANGES: dict[str, tuple[dt.timedelta, dt.timedelta]] = {
    "1h": (dt.timedelta(hours=1), dt.timedelta(minutes=2)),
    "24h": (dt.timedelta(hours=24), dt.timedelta(hours=1)),
    "7d": (dt.timedelta(days=7), dt.timedelta(hours=6)),
}

TOOL_CATEGORY = {
    "search": "search", "open": "open", "documents": "open", "query": "numbers", "sources": "sources",
    "job": "sources",
}
AUDIT_CATEGORY = {
    "web_sign_in": "sign-in", "connect_drive": "sources", "disconnect": "sources", "drive_discover": "sources",
}
AUDIT_LABEL = {
    "web_sign_in": "Signed in on the web", "connect_drive": "Added something from Drive",
    "disconnect": "Disconnected a source", "drive_discover": "Found new shares in Drive",
}
JOB_LABEL = {
    "sync": "Check Drive for changes", "sync_item": "Read one file", "drive_discover": "Look for new shares",
    "embed": "Make text searchable",
}
ADMIN_ACTION_LABEL = {
    "discover_now": "Looked for new shares now", "check_drive_now": "Checked Drive for changes now",
    "full_reread": "Started a full re-read", "embed_now": "Made waiting text searchable now",
    "check_root": "Checked one source now", "reread_item": "Re-read one file", "sync_link": "Re-read a file by link",
    "retry_job": "Retried a job", "cancel_job": "Cancelled a job", "set_refresh": "Changed refresh timings",
}


# Access ---------------------------------------------------------------------------------------


async def _admin(request: Request) -> access.Person | None:
    from nebula.webui import viewer

    person = await viewer(request)
    return person if person and person.is_admin else None


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(data, default=_default)), status_code=status,
                        headers={"Cache-Control": "no-store"})


def _default(v: Any) -> Any:
    if isinstance(v, dt.datetime):
        return v.astimezone(UTC).isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # same-origin fetches from older browsers; the JSON content type still forces a preflight
    return origin.rstrip("/") == settings().public_url.rstrip("/") or origin.rstrip("/") == str(request.base_url).rstrip("/")


@mcp.custom_route("/admin", methods=["GET"], include_in_schema=False)
async def admin_page(request: Request) -> Response:
    from nebula.webui import _page, viewer

    person = await viewer(request)
    if not person:
        return RedirectResponse("/login?next=/admin", status_code=302)
    if not person.is_admin:
        return _page("Admins only", "<h1>Admins only</h1><p>This page is for Nebula admins.</p>", person)
    return FileResponse(UI / "admin.html", headers={"Cache-Control": "no-store"})


@mcp.custom_route("/admin/assets/{name}", methods=["GET"], include_in_schema=False)
async def admin_asset(request: Request) -> Response:
    name = request.path_params["name"]
    if name not in ("admin.css", "admin.js"):
        return Response(status_code=404)
    media = "text/css" if name.endswith(".css") else "text/javascript"
    return FileResponse(UI / name, media_type=media, headers={"Cache-Control": "no-cache"})


# Time windows ---------------------------------------------------------------------------------


async def _window(rng: str) -> tuple[dt.datetime, dt.timedelta, dt.datetime]:
    """(start, bar width, now) for a range name."""
    now = dt.datetime.now(UTC)
    if rng == "today":
        start = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        return start, dt.timedelta(minutes=30), now
    if rng == "all":
        row = await db.fetch_one("select min(at) as first from tool_calls")
        first = row["first"] if row and row["first"] else now - dt.timedelta(days=1)
        start = first.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        days = max(1, (now - start).days + 1)
        step = dt.timedelta(days=1) if days <= 60 else dt.timedelta(days=7)
        return start, step, now
    span, step = RANGES.get(rng, RANGES["24h"])
    epoch = dt.datetime(2026, 1, 1, tzinfo=IST).astimezone(UTC)
    start = now - span
    start = epoch + ((start - epoch) // step) * step + step
    return start, step, now


def _buckets(start: dt.datetime, step: dt.timedelta, now: dt.datetime) -> list[dt.datetime]:
    out, b = [], start
    while b <= now:
        out.append(b)
        b += step
    return out or [start]


# State ----------------------------------------------------------------------------------------


@mcp.custom_route("/admin/api/search/{call_id:int}", methods=["GET"], include_in_schema=False)
async def admin_search(request: Request) -> Response:
    """Every step of one search: what it looked for, what each method found, how the reranker reordered it,
    what went back to Claude, and how long each step took."""
    if not await _admin(request):
        return _json({"error": "Admins only. Sign in again at /login?next=/admin."}, 403)
    r = await db.fetch_one(
        """select t.id, t.at, t.summary, t.ok, t.error, t.ms, t.result_count, t.results, t.trace, t.args,
                  p.name, p.email
           from tool_calls t left join people p on p.id=t.person_id where t.id=%s and t.tool='search'""",
        (request.path_params["call_id"],))
    if not r:
        return _json({"error": "No such search."}, 404)
    return _json({"id": r["id"], "at": r["at"], "question": r["summary"], "ok": r["ok"], "error": r["error"],
                  "ms": r["ms"], "result_count": r["result_count"], "results": r["results"] or [],
                  "trace": r["trace"], "args": r["args"],
                  "person": {"name": r["name"], "email": r["email"]} if r["email"] else None})


@mcp.custom_route("/admin/api/state", methods=["GET"], include_in_schema=False)
async def admin_state(request: Request) -> Response:
    person = await _admin(request)
    if not person:
        return _json({"error": "Admins only. Sign in again at /login?next=/admin."}, 403)
    view = request.query_params.get("view", "overview")
    rng = request.query_params.get("range", "24h")
    start, step, now = await _window(rng)
    out: dict[str, Any] = {"now": now, "range": rng, "start": start, "step_seconds": int(step.total_seconds()),
                           "pulse": await _pulse(), "me": {"email": person.email, "name": person.name}}
    q = request.query_params
    if view == "overview":
        out["overview"] = await _overview(start, step, now, rng)
    elif view == "people":
        out["people"] = await _people(start, step, now)
    elif view == "activity":
        out["activity"] = await _activity(start, q.get("after"), q.get("person"), q.get("category"), q.get("status"),
                                          q.get("q"))
        out["people_index"] = await _people_index()
    elif view == "indexing":
        out["indexing"] = await _indexing()
    elif view == "sources":
        out["sources"] = await _sources()
    elif view == "jobs":
        out["jobs"] = await _jobs(start, q.get("status"))
    return _json(out)


async def _pulse() -> dict:
    beats = await db.fetch_all(
        "select value, updated_at from app_settings where key like 'heartbeat:worker%%' order by updated_at desc")
    live = [b for b in beats if b["updated_at"] > dt.datetime.now(UTC) - dt.timedelta(seconds=60)]
    busy = next((b for b in live if (b["value"] or {}).get("job")), None)
    hb = busy or (beats[0] if beats else None)
    job = (hb["value"] or {}).get("job") if hb else None
    counts = await db.fetch_one(
        """select count(*) filter (where status='running') as running,
                  count(*) filter (where status='queued' and run_after <= now()) as queued,
                  count(*) filter (where status='failed' and updated_at > now() - interval '24 hours') as failed_24h
           from jobs""")
    waiting = await db.fetch_one("select count(*) as n from chunks where embedding is null")
    last_done = await db.fetch_one("select max(updated_at) as at from jobs where status='done'")
    return {
        "worker_seen": beats[0]["updated_at"] if beats else None,
        "workers_alive": len(live),
        "worker_job": ({"kind": job.get("kind"), "label": JOB_LABEL.get(job.get("kind"), job.get("kind")),
                        "since": dt.datetime.fromtimestamp(job["since"], UTC) if job.get("since") else None}
                       if job else None),
        "jobs_running": counts["running"] if counts else 0,
        "jobs_queued": counts["queued"] if counts else 0,
        "jobs_failed_24h": counts["failed_24h"] if counts else 0,
        "pieces_waiting": waiting["n"] if waiting else 0,
        "last_job_done": last_done["at"] if last_done else None,
    }


async def _overview(start: dt.datetime, step: dt.timedelta, now: dt.datetime, rng: str) -> dict:
    span = now - start
    prev_start = start - span
    params = {"start": start, "prev": prev_start, "step": step, "origin": start}
    t = await db.fetch_row(
        """select count(*) filter (where at >= %(start)s) as requests,
                  count(*) filter (where at >= %(prev)s and at < %(start)s) as requests_prev,
                  count(*) filter (where at >= %(start)s and not ok) as errors,
                  count(*) filter (where at >= %(start)s and tool='search') as searches,
                  count(*) filter (where at >= %(prev)s and at < %(start)s and tool='search') as searches_prev,
                  count(distinct person_id) filter (where at >= %(start)s) as active,
                  count(distinct person_id) filter (where at >= %(prev)s and at < %(start)s) as active_prev,
                  percentile_cont(0.5) within group (order by ms) filter (where at >= %(start)s and tool='search' and ok) as p50,
                  percentile_cont(0.9) within group (order by ms) filter (where at >= %(start)s and tool='search' and ok) as p90
           from tool_calls where at >= %(prev)s""", params)
    people = await db.fetch_one(
        """select count(*) filter (where last_seen_at is not null) as signed_in,
                  (select count(*) from (select person_id from mcp_clients union
                                         select person_id from tool_calls where person_id is not null) x) as connected,
                  (select count(distinct managed_by) from source_roots where status='active') as shared_drive
           from people where status='active'""")
    files = await db.fetch_one(
        """with f as (
             select i.id, i.status, i.pieces, i.pieces_waiting as waiting
             from items i where i.status <> 'deleted')
           select count(*) as total,
                  count(*) filter (where status='active' and pieces>0 and waiting=0) as ready,
                  count(*) filter (where waiting>0) as waiting,
                  count(*) filter (where status='error') as failed
           from f""")
    saved = await db.fetch_one("select count(*) as sources from source_roots where status='active'")
    series = await db.fetch_all(
        """select date_bin(%(step)s::interval, at, %(origin)s::timestamptz) as b, tool, count(*) as n,
                  count(*) filter (where not ok) as errors,
                  percentile_cont(0.5) within group (order by ms) filter (where tool='search' and ok) as p50,
                  percentile_cont(0.9) within group (order by ms) filter (where tool='search' and ok) as p90
           from tool_calls where at >= %(start)s group by 1, 2""", params)
    buckets = {b: {"t": b, "total": 0, "errors": 0, "by": {}, "p50": None, "p90": None} for b in _buckets(start, step, now)}
    for r in series:
        b = buckets.get(r["b"])
        if b is None:
            continue
        cat = TOOL_CATEGORY.get(r["tool"], "other")
        b["total"] += r["n"]
        b["errors"] += r["errors"]
        b["by"][cat] = b["by"].get(cat, 0) + r["n"]
        if r["tool"] == "search":
            b["p50"], b["p90"] = r["p50"], r["p90"]
    tools = await db.fetch_all(
        """select tool, count(*) as n, count(*) filter (where not ok) as errors,
                  percentile_cont(0.5) within group (order by ms) as p50
           from tool_calls where at >= %(start)s group by tool order by n desc""", params)
    top = await db.fetch_all(
        """select p.id, p.name, p.email, count(*) as n, max(t.at) as last
           from tool_calls t join people p on p.id=t.person_id
           where t.at >= %(start)s group by p.id order by n desc, last desc limit 8""", params)
    running = await _job_rows("where j.status='running' or (j.status='queued' and j.run_after <= now())", {}, limit=6)
    finished = await _job_rows("where j.status in ('done','failed')", {}, limit=4)
    recent = await _activity(start, None, None, None, None, None, limit=8)
    return {
        "tiles": {**dict(t), **(dict(people) if people else {}), **(dict(files) if files else {}),
                  **(dict(saved) if saved else {})},
        "has_previous": rng in RANGES or rng == "today",
        "series": list(buckets.values()),
        "tools": [{"tool": r["tool"], "category": TOOL_CATEGORY.get(r["tool"], "other"), "n": r["n"],
                   "errors": r["errors"], "p50": r["p50"]} for r in tools],
        "top_people": [dict(r) for r in top],
        "running": running,
        "finished": finished,
        "recent": recent["rows"],
    }


async def _people_index() -> list[dict]:
    rows = await db.fetch_all(
        """select p.id, p.name, p.email from people p
           where p.last_seen_at is not null or exists (select 1 from tool_calls t where t.person_id=p.id)
           order by lower(coalesce(nullif(p.name,''), p.email))""")
    return [dict(r) for r in rows]


async def _people(start: dt.datetime, step: dt.timedelta, now: dt.datetime) -> dict:
    rows = await db.fetch_all(
        """select p.id, p.name, p.email, p.is_admin, p.status, p.created_at, p.last_seen_at,
                  (select min(at) from tool_calls t where t.person_id=p.id) as first_request,
                  (select max(at) from tool_calls t where t.person_id=p.id) as last_request,
                  (select min(at) from audit_log a where a.person_id=p.id and a.action='web_sign_in') as first_web,
                  (select count(*) from tool_calls t where t.person_id=p.id and t.at >= %(start)s) as requests,
                  (select count(*) from tool_calls t where t.person_id=p.id and t.at >= %(start)s and t.tool='search') as searches,
                  (select count(*) from tool_calls t where t.person_id=p.id and t.at >= %(start)s and not t.ok) as errors,
                  (select count(*) from tool_calls t where t.person_id=p.id) as requests_all
           from people p
           where p.last_seen_at is not null
              or exists (select 1 from tool_calls t where t.person_id=p.id)
              or exists (select 1 from source_roots r where r.managed_by=p.id)
           order by p.last_seen_at desc nulls last""", {"start": start})
    ids = [str(r["id"]) for r in rows]
    clients = await db.fetch_all(
        "select person_id, client, first_seen_at, last_seen_at, connects from mcp_clients where person_id = any(%s::uuid[]) "
        "order by first_seen_at", (ids,))
    roots = await db.fetch_all(
        """select r.managed_by, r.name, r.kind, r.status, r.created_at,
                  (select count(*) from items i where i.collection_id=r.collection_id and i.source_root=r.external_id
                     and i.status <> 'deleted') as files
           from source_roots r where r.managed_by = any(%s::uuid[]) order by r.created_at""", (ids,))
    spark = await db.fetch_all(
        """select person_id, date_bin(%s::interval, at, %s::timestamptz) as b, count(*) as n
           from tool_calls where person_id = any(%s::uuid[]) and at >= %s group by 1, 2""", (step, start, ids, start))
    visible = await db.fetch_all(
        """select p.id as person_id, count(distinct a.item_id) as n
           from people p join item_access a on (
                 a.principal = 'email:' || p.email
              or (a.principal = 'domain:' || split_part(p.email, '@', 2) and split_part(p.email, '@', 2) = any(%s)))
           join items i on i.id=a.item_id and i.status <> 'deleted'
           where p.id = any(%s::uuid[]) group by p.id""", (list(settings().allowed_domains), ids))
    buckets = _buckets(start, step, now)
    by_person_spark: dict[str, dict] = {}
    for r in spark:
        by_person_spark.setdefault(str(r["person_id"]), {})[r["b"]] = r["n"]
    vis = {str(r["person_id"]): r["n"] for r in visible}
    out = []
    for r in rows:
        pid = str(r["id"])
        firsts = [x for x in (r["first_request"], r["first_web"],
                              min((c["first_seen_at"] for c in clients if str(c["person_id"]) == pid), default=None))
                  if x]
        first = min(firsts) if firsts else (r["created_at"] if r["last_seen_at"] else None)
        domain = r["email"].rsplit("@", 1)[-1].lower()
        out.append({
            "id": pid, "name": r["name"], "email": r["email"], "is_admin": access.is_admin(r),
            "rl_member": domain in settings().allowed_domains,
            "status": r["status"], "first_seen": first, "last_seen": r["last_seen_at"],
            "signed_in": r["last_seen_at"] is not None,
            "clients": [dict(c, person_id=None) for c in clients if str(c["person_id"]) == pid],
            "drive": [dict(x, managed_by=None) for x in roots if str(x["managed_by"]) == pid],
            "files_visible": vis.get(pid, 0),
            "requests": r["requests"], "searches": r["searches"], "errors": r["errors"],
            "requests_all": r["requests_all"], "last_request": r["last_request"],
            "spark": [by_person_spark.get(pid, {}).get(b, 0) for b in buckets],
        })
    return {"rows": out}


async def _titles(refs: list[str]) -> dict[str, str]:
    by_kind: dict[str, list[str]] = {}
    for ref in refs:
        kind, _, rid = ref.partition(":")
        if kind in ("item", "dataset") and len(rid) == 36:
            by_kind.setdefault(kind, []).append(rid)
    out: dict[str, str] = {}
    tables = {"item": "items", "dataset": "datasets"}
    for kind, ids in by_kind.items():
        for r in await db.fetch_all(f"select id, title from {tables[kind]} where id = any(%s::uuid[])", (ids,)):  # noqa: S608
            out[f"{kind}:{r['id']}"] = r["title"]
    return out


def _tool_label(tool: str, action: str, args: dict) -> str:
    if tool == "search":
        return "Searched"
    if tool == "open":
        return "Looked at a picture" if args.get("as_image") else "Opened"
    if tool == "documents":
        return "Listed documents"
    if tool == "query":
        return "Ran a number question"
    if tool == "sources":
        return {"list": "Listed their sources", "sync": "Asked for a refresh", "status": "Checked a file's status",
                "connect": "Added something from Drive", "disconnect": "Disconnected a source"}.get(action, "Used sources")
    if tool == "job":
        return "Checked a job"
    return f"Used {tool}"


async def _activity(start: dt.datetime, after: str | None, person: str | None, category: str | None,
                    status: str | None, text: str | None, limit: int = 600) -> dict:
    since = start
    if after:
        try:
            since = max(start, dt.datetime.fromisoformat(after.replace("Z", "+00:00")))
        except ValueError:
            pass
    op = ">" if after else ">="
    pfilter = "and person_id = %(person)s::uuid" if person else ""
    params = {"since": since, "person": person, "limit": limit}
    tool_rows = await db.fetch_all(
        f"""select t.id, t.at, t.person_id, t.tool, t.action, t.summary, t.target, t.args, t.ok, t.error,
                  t.result_count, t.ms, t.client, t.results, t.trace is not null as has_trace
           from tool_calls t where t.at {op} %(since)s {pfilter} order by t.at desc limit %(limit)s""", params)
    audit_rows = await db.fetch_all(
        f"""select a.id, a.at, a.person_id, a.action, a.resource_type, a.resource_id, a.detail
           from audit_log a where a.at {op} %(since)s {pfilter} order by a.at desc limit %(limit)s""", params)
    index_rows = [] if person else await db.fetch_all(
        f"""select e.id, e.at, e.item_id, e.title, e.event, e.detail, e.ms from index_events e
           where e.at {op} %(since)s and e.event in ('read','removed','failed') order by e.at desc limit %(limit)s""",
        params)
    connect_rows = await db.fetch_all(
        f"""select m.person_id, m.client, m.first_seen_at as at from mcp_clients m
           where m.first_seen_at {op} %(since)s {pfilter.replace('person_id', 'm.person_id')}""", params)
    pids = {str(r["person_id"]) for r in [*tool_rows, *audit_rows, *connect_rows] if r["person_id"]}
    people = {str(r["id"]): r for r in await db.fetch_all(
        "select id, name, email from people where id = any(%s::uuid[])", (list(pids),))}
    titles = await _titles([r["target"] for r in tool_rows if r["target"]])

    def who(pid: Any) -> dict | None:
        p = people.get(str(pid)) if pid else None
        return {"id": str(p["id"]), "name": p["name"], "email": p["email"]} if p else None

    rows: list[dict] = []
    for r in tool_rows:
        cat = TOOL_CATEGORY.get(r["tool"], "other")
        args = r["args"] or {}
        summary = r["summary"]
        if r["target"] in titles:
            summary = titles[r["target"]] + (f" ({args['part']})" if args.get("part") else "")
        detail = (r["error"] if not r["ok"] else
                  (f"{r['result_count']} result{'s' if r['result_count'] != 1 else ''}"
                   if r["result_count"] is not None else ""))
        rows.append({"key": f"t{r['id']}", "kind": "tool", "at": r["at"], "person": who(r["person_id"]),
                     "category": cat, "label": _tool_label(r["tool"], r["action"], args), "tool": r["tool"],
                     "text": summary, "detail": detail, "ok": r["ok"], "ms": r["ms"], "hits": r["results"],
                     "trace_id": r["id"] if r["has_trace"] else None})
    for r in audit_rows:
        action = r["action"]
        if action.startswith("admin_page:"):
            name = action.split(":", 1)[1]
            label, cat = "Admin page: " + ADMIN_ACTION_LABEL.get(name, name).lower(), "admin"
        else:
            label, cat = AUDIT_LABEL.get(action, action.replace("_", " ").capitalize()), AUDIT_CATEGORY.get(action, "other")
        info = r["detail"] or {}
        shown = ("name", "link", "external_id", "values")
        note = "" if action == "web_sign_in" else ", ".join(
            str(info[k]) for k in shown if k in info and not isinstance(info[k], (dict, list)))[:300]
        rows.append({"key": f"a{r['id']}", "kind": "audit", "at": r["at"], "person": who(r["person_id"]),
                     "category": cat, "label": label, "text": note, "detail": "", "ok": True, "ms": None})
    for r in index_rows:
        label = {"read": "Read a file", "removed": "Removed a file", "failed": "Couldn't read a file"}[r["event"]]
        rows.append({"key": f"i{r['id']}", "kind": "index", "at": r["at"], "person": None, "category": "indexing",
                     "label": label, "text": r["title"], "detail": r["detail"], "ok": r["event"] != "failed",
                     "ms": r["ms"]})
    for r in connect_rows:
        rows.append({"key": f"c{r['person_id']}{r['client']}", "kind": "connect", "at": r["at"],
                     "person": who(r["person_id"]), "category": "sign-in", "label": "Connected Nebula to Claude",
                     "text": r["client"], "detail": "", "ok": True, "ms": None})
    if category:
        rows = [r for r in rows if r["category"] == category]
    if status == "ok":
        rows = [r for r in rows if r["ok"]]
    elif status == "failed":
        rows = [r for r in rows if not r["ok"]]
    if text:
        needle = text.lower()
        rows = [r for r in rows if needle in " ".join(str(x) for x in (
            r["label"], r["text"], r["detail"], (r["person"] or {}).get("name", ""),
            (r["person"] or {}).get("email", ""))).lower()]
    rows.sort(key=lambda r: r["at"], reverse=True)
    return {"rows": rows[:limit], "capped": len(rows) > limit}


# Indexing -------------------------------------------------------------------------------------

async def _indexing() -> dict:
    files, counts = await stages.file_states("all")
    x = {"files": files, "counts": counts}
    events = await db.fetch_all(
        "select id, at, item_id, title, event, detail, ms from index_events order by at desc limit 60")
    work = await _job_rows("where j.kind in ('sync','sync_item','embed','drive_discover') "
                           "and (j.status='running' or (j.status='queued' and j.run_after <= now()))", {}, limit=20)
    return {**x, "events": [dict(e) for e in events], "work": work}


# Sources --------------------------------------------------------------------------------------


async def _sources() -> dict:
    from nebula import sync
    from nebula.connectors.gdrive import service_account_email

    refresh = await activity.refresh_settings()
    space = await db.fetch_one("select * from collections where slug=%s and kind='gdrive'", (sync.DRIVE_SLUG,))
    roots = await db.fetch_all(
        """select r.id, r.external_id, r.kind, r.name, r.shared_by, r.added_via, r.status, r.status_note, r.created_at,
                  p.name as manager_name, p.email as manager_email,
                  count(i.id) filter (where i.status='active') as files,
                  count(i.id) filter (where i.status='error') as failed,
                  count(i.id) filter (where i.status='active' and i.pieces_waiting > 0) as waiting
           from source_roots r left join people p on p.id=r.managed_by
           left join items i on i.collection_id=r.collection_id and i.source_root=r.external_id
           group by r.id, p.name, p.email order by r.status, r.created_at desc""")
    last = {r["kind"]: r for r in await db.fetch_all(
        """select distinct on (kind) kind, updated_at, status from jobs
           where kind in ('drive_discover','embed','sync') and status in ('done','failed')
           order by kind, updated_at desc""")}
    running = {r["kind"] for r in await db.fetch_all(
        "select distinct kind from jobs where status='running' and kind in ('drive_discover','embed','sync')")}
    queued = {r["kind"] for r in await db.fetch_all(
        """select distinct kind from jobs where status='queued' and run_after <= now()
           and kind in ('drive_discover','embed','sync')""")}
    busy = await db.fetch_one(
        "select kind, locked_at from jobs where status='running' order by locked_at limit 1")

    def nxt(last_at: Any, seconds: int) -> Any:
        return (last_at + dt.timedelta(seconds=seconds)) if last_at else None

    sync_last = space["last_synced_at"] if space else None
    schedule = [
        {"key": "discover_seconds", "label": "Look for newly shared folders and files",
         "help": "Finds anything newly shared with Nebula's Google account.", "unit": "seconds",
         "value": refresh["discover_seconds"], "last": last.get("drive_discover", {}).get("updated_at"),
         "running": "drive_discover" in running, "action": "discover_now"},
        {"key": "sync_seconds", "label": "Check Drive for edits and sharing changes",
         "help": "Picks up edited, new and removed files, and changes to who can see them.", "unit": "seconds",
         "value": refresh["sync_seconds"], "last": sync_last, "running": "sync" in running, "action": "check_drive_now"},
        {"key": "embed_seconds", "label": "Make newly read text searchable",
         "help": "Turns text Nebula has read into search entries. Files asked for by name skip this wait.",
         "unit": "seconds", "value": refresh["embed_seconds"], "last": last.get("embed", {}).get("updated_at"),
         "running": "embed" in running, "action": "embed_now"},
        {"key": "full_sync_minutes", "label": "Re-list everything in Drive",
         "help": "A full pass that catches anything the change check missed. Unchanged files aren't downloaded again.",
         "unit": "minutes", "value": refresh["full_sync_minutes"],
         "last": space["last_full_sync_at"] if space else None, "running": False, "action": "full_reread"},
    ]
    kinds = {"discover_now": "drive_discover", "check_drive_now": "sync", "embed_now": "embed", "full_reread": "sync"}
    for s in schedule:
        secs = s["value"] * (60 if s["unit"] == "minutes" else 1)
        s["next"] = nxt(s["last"], secs)
        kind = kinds[s["action"]]
        s["queued"] = kind in queued and not s["running"]
        s["waiting_for"] = (JOB_LABEL.get(busy["kind"], busy["kind"]) if s["queued"] and busy and busy["kind"] != kind
                            else None)
    return {
        "bot": service_account_email(),
        "drive": ({"id": str(space["id"]), "last_synced_at": space["last_synced_at"],
                   "last_full_sync_at": space["last_full_sync_at"], "sync_error": space["sync_error"]} if space else None),
        "schedule": schedule,
        "roots": [{**dict(r), "id": str(r["id"]),
                   "url": (f"https://drive.google.com/drive/folders/{r['external_id']}" if r["kind"] == "folder" else
                           f"https://drive.google.com/drive/folders/{r['external_id']}" if r["kind"] == "drive" else
                           f"https://drive.google.com/file/d/{r['external_id']}/view")} for r in roots],
    }


# Jobs -----------------------------------------------------------------------------------------


async def _job_rows(where: str, params: dict, limit: int = 300) -> list[dict]:
    rows = await db.fetch_all(
        f"""select j.id, j.kind, j.status, j.priority, j.progress, j.message, j.attempts, j.max_attempts, j.payload,
                   j.result, j.error, j.created_at, j.locked_at, j.updated_at, j.run_after,
                   p.name as person_name, p.email as person_email
            from jobs j left join people p on p.id=j.person_id {where}
            order by case j.status when 'running' then 0 when 'queued' then 1 else 2 end,
                     coalesce(j.locked_at, j.created_at) desc limit {int(limit)}""", params)  # noqa: S608
    cols = {str(r["id"]): r["name"] for r in await db.fetch_all("select id, name from collections")}
    ext_ids = [str((r["payload"] or {}).get("external_id")) for r in rows if (r["payload"] or {}).get("external_id")]
    titles = {r["external_id"]: r["title"] for r in await db.fetch_all(
        "select external_id, title from items where external_id = any(%s)", (ext_ids,))} if ext_ids else {}
    root_names = {r["external_id"]: r["name"] for r in await db.fetch_all("select external_id, name from source_roots")}
    typical = await jobs._typical_seconds()
    now = dt.datetime.now(UTC)
    out = []
    for r in rows:
        p = r["payload"] or {}
        if p.get("external_id"):
            target = titles.get(p["external_id"]) or root_names.get(p["external_id"]) or "a Drive file"
        elif p.get("rewalk"):
            target = ", ".join(root_names.get(x, "a shared folder") for x in p["rewalk"])
        elif p.get("collection_id"):
            target = cols.get(p["collection_id"], "a collection")
            if p.get("full"):
                target += " (everything)"
        else:
            target = ""
        took = None
        if r["status"] in ("done", "failed") and r["locked_at"]:
            took = (r["updated_at"] - r["locked_at"]).total_seconds()
        expected = typical.get(r["kind"], jobs.DEFAULT_SECONDS)
        eta = None
        if r["status"] == "running" and r["locked_at"]:
            eta = max(1.0, expected - (now - r["locked_at"]).total_seconds())
        out.append({
            "id": str(r["id"]), "kind": r["kind"], "label": JOB_LABEL.get(r["kind"], r["kind"]), "target": target,
            "status": r["status"], "progress": r["progress"], "message": r["message"], "attempts": r["attempts"],
            "max_attempts": r["max_attempts"], "created_at": r["created_at"], "started_at": r["locked_at"],
            "updated_at": r["updated_at"], "run_after": r["run_after"], "took": took, "eta": eta,
            "expected": expected, "error": (r["error"] or "").split("\n", 1)[0][:400],
            "result": r["result"] if r["status"] == "done" else None,
            "person": ({"name": r["person_name"], "email": r["person_email"]} if r["person_email"] else None),
        })
    return out


async def _jobs(start: dt.datetime, status: str | None) -> dict:
    where = "where (j.status in ('queued','running') or j.updated_at >= %(start)s)"
    if status in ("queued", "running", "done", "failed", "cancelled"):
        where += " and j.status = %(status)s"
    rows = await _job_rows(where, {"start": start, "status": status}, limit=400)
    counts = await db.fetch_one(
        """select count(*) filter (where status='queued') as queued, count(*) filter (where status='running') as running,
                  count(*) filter (where status='done' and updated_at >= %(start)s) as done,
                  count(*) filter (where status='failed' and updated_at >= %(start)s) as failed
           from jobs""", {"start": start})
    return {"rows": rows, "counts": dict(counts) if counts else {}}


# Actions --------------------------------------------------------------------------------------


@mcp.custom_route("/admin/api/action", methods=["POST"], include_in_schema=False)
async def admin_action(request: Request) -> Response:
    person = await _admin(request)
    if not person:
        return _json({"error": "Admins only."}, 403)
    if not _same_origin(request) or "application/json" not in request.headers.get("content-type", ""):
        return _json({"error": "Bad request."}, 400)
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "Bad request."}, 400)
    action = str(body.get("action") or "")
    try:
        result = await _do(person, action, body)
    except (ValueError, access.AccessDenied) as e:
        return _json({"error": str(e)}, 400)
    except Exception as e:
        log.exception("admin action %s failed", action)
        return _json({"error": f"That didn't work ({type(e).__name__}). Check the server logs."}, 500)
    await activity.audit(None, person, f"admin_page:{action}", None, None,
                         {k: v for k, v in body.items() if k != "action" and not isinstance(v, (dict, list))})
    return _json({"ok": True, **result})


async def _do(person: access.Person, action: str, body: dict) -> dict:
    from nebula import sources as src, sync

    if action == "discover_now":
        job = await jobs.enqueue("drive_discover", {}, dedupe_key="drive_discover", priority=5, person_id=person.id)
        return {"job": job, "message": "Looking for newly shared folders and files now."}
    if action == "check_drive_now":
        space = await sync.drive_space()
        job = await jobs.enqueue("sync", {"collection_id": str(space["id"])}, dedupe_key=f"sync:{space['id']}",
                                 priority=10, person_id=person.id)
        return {"job": job, "message": "Checking Drive for changes now."}
    if action == "full_reread":
        space = await sync.drive_space()
        job = await jobs.enqueue("sync", {"collection_id": str(space["id"]), "full": True, "sweep": True},
                                 dedupe_key=f"sync-full:{space['id']}", priority=15, person_id=person.id)
        return {"job": job, "message": "Re-listing everything in Drive and re-reading who can see each file."}
    if action == "embed_now":
        job = await jobs.enqueue("embed", {}, dedupe_key="embed", priority=5, person_id=person.id)
        return {"job": job, "message": "Making waiting text searchable now."}
    if action == "check_root":
        root = await db.fetch_one("select * from source_roots where id=%s::uuid", (str(body.get("root_id")),))
        if not root:
            raise ValueError("That source no longer exists.")
        res = await src.request_sync(person, source=str(root["id"]))
        return {"job": res.get("job"), "message": f"Checking {root['name'] or 'that source'} now."}
    if action == "reread_item":
        res = await src.request_sync(person, item=f"item:{body.get('item_id')}")
        return {"job": res.get("job"), "message": f"Re-reading {res.get('title') or 'that file'} now."}
    if action == "sync_link":
        link = str(body.get("link") or "").strip()
        if not link:
            raise ValueError("Paste a Drive link first.")
        res = await src.request_sync(person, link=link)
        return {"job": res.get("job"), "message": f"Reading {res.get('title') or res.get('name') or 'that file'} now."}
    if action == "retry_job":
        row = await db.fetch_one(
            """update jobs set status='queued', attempts=0, run_after=now(), error=null, locked_at=null, locked_by=null,
                      updated_at=now() where id=%s::uuid and status='failed' returning id""", (str(body.get("job_id")),))
        if not row:
            raise ValueError("Only failed jobs can be retried.")
        return {"job": str(row["id"]), "message": "Queued again."}
    if action == "cancel_job":
        row = await db.fetch_one(
            "update jobs set status='cancelled', updated_at=now() where id=%s::uuid and status='queued' returning id",
            (str(body.get("job_id")),))
        if not row:
            raise ValueError("Only jobs that haven't started can be cancelled.")
        return {"job": str(row["id"]), "message": "Cancelled."}
    if action == "set_refresh":
        values = body.get("values") or {}
        current = await activity.refresh_settings()
        for k, v in values.items():
            if k not in activity.REFRESH_LIMITS:
                raise ValueError(f"Unknown setting {k}.")
            lo, hi = activity.REFRESH_LIMITS[k]
            if not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}.")
            current[k] = int(v)
        await activity.set_setting("refresh", current, person.id)
        return {"settings": current, "message": "Saved. The worker uses the new timings within a few seconds."}
    raise ValueError("Unknown action.")

