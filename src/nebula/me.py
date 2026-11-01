"""The "My files" page at /me: what someone shared with Nebula, what others shared with them, and
where each of those files is in being read and made searchable.

Anyone who can sign in to Nebula can open it, and it only shows that person's files: roots they
shared, and files Drive shares with them (by email, or with everyone at RL). Admins see the same
personal view here; the whole picture is on /admin.
"""

from __future__ import annotations

import datetime as dt

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response

from nebula import access, db, jobs
from nebula.admin import UI, _json, _pulse, _same_origin
from nebula.knowledge.stages import STAGES, file_states
from nebula.config import settings
from nebula.server import mcp

GAP_WORDS = {
    "needs_extraction": "isn't readable as text",
    "uncertain": "may not have been read fully",
}


def watch_url() -> str:
    return f"{settings().public_url.rstrip('/')}/me"


async def _person(request: Request) -> access.Person | None:
    from nebula.webui import viewer

    return await viewer(request)


@mcp.custom_route("/me", methods=["GET"], include_in_schema=False)
async def me_page(request: Request) -> Response:
    if not await _person(request):
        return RedirectResponse("/login?next=/me", status_code=302)
    return FileResponse(UI / "me.html", headers={"Cache-Control": "no-store"})


@mcp.custom_route("/me/assets/me.js", methods=["GET"], include_in_schema=False)
async def me_script(request: Request) -> Response:
    return FileResponse(UI / "me.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


# Data -----------------------------------------------------------------------------------------


async def _embed_rate() -> float | None:
    """Pieces made searchable per second, from the last hour of embedding work."""
    r = await db.fetch_one(
        """select sum((result->>'embedded')::int) as n,
                  sum(extract(epoch from updated_at - locked_at)) as secs
           from jobs where kind='embed' and status='done' and locked_at is not null
             and updated_at > now() - interval '1 hour' and coalesce((result->>'embedded')::int, 0) > 0""")
    if not r or not r["n"] or not r["secs"]:
        return None
    return float(r["n"]) / max(1.0, float(r["secs"]))


async def _waits(files: list[dict]) -> None:
    """Add a rough wait to files that are queued or being made searchable."""
    rate = await _embed_rate()
    indexing = [f["id"] for f in files if f["state"] == "indexing"]
    # How many pieces from other files wait ahead of each file's first one, in one query for all of them.
    ahead_of = {str(r["item_id"]): r["ahead"] for r in await db.fetch_all(
        """with firsts as (select item_id, min(id) as first from chunks
                           where embedding is null and item_id = any(%s::uuid[]) group by item_id)
           select f.item_id, (select count(*) from chunks c where c.embedding is null and c.id < f.first) as ahead
           from firsts f""", (indexing,))} if indexing else {}
    for f in files:
        f["wait"], f["ahead"] = None, None
        if f["state"] == "queued" and f["job"]:
            e = await jobs.eta(f["job"]["id"])
            f["wait"] = e.get("estimate")
            if e.get("jobs_ahead"):
                f["ahead"] = f"{e['jobs_ahead']} job{'s' if e['jobs_ahead'] != 1 else ''} ahead"
        elif f["state"] == "indexing":
            n_ahead = ahead_of.get(f["id"], 0)
            if rate:
                f["wait"] = jobs.describe_wait((n_ahead + f["pieces"] - f["ready"]) / rate)
            f["ahead"] = f"{n_ahead:,} pieces from other files ahead of yours" if n_ahead else "next in line"


async def _gaps(f: dict) -> list[dict]:
    """The parts of a file Nebula couldn't read, in plain words."""
    if not f["unread_parts"]:
        return []
    from nebula.ingest.types import Document
    from nebula.knowledge import items

    row = await db.fetch_one("select document from items where id=%s", (f["id"],))
    if not row or not row["document"]:
        return []
    summary = items.extraction_summary(Document.from_dict(row["document"]))
    out = []
    for g in (summary or {}).get("parts_not_read", [])[:6]:
        part = g.get("part", "")
        where = "The whole file" if part == "all" else part.replace(":", " ").capitalize()
        out.append({"part": part, "where": where, "why": g.get("reason") or GAP_WORDS.get(g.get("state", ""), "")})
    return out


async def _mine(person: access.Person) -> dict:
    roots = await db.fetch_all(
        """select r.id, r.collection_id, r.external_id, r.kind, r.name, r.status, r.status_note, r.created_at,
                  r.added_via, c.last_synced_at
           from source_roots r join collections c on c.id=r.collection_id
           where r.managed_by=%s::uuid order by r.status, r.created_at desc""", (person.id,))
    files, counts = await file_states("shared_by", person)
    await _waits(files)
    for f in files:
        f["gaps"] = await _gaps(f)
    by_root: dict[str, dict] = {}
    for f in files:
        b = by_root.setdefault(str(f["source_root"] or ""), dict.fromkeys(STAGES, 0))
        b[f["state"]] += 1
    out_roots = []
    for r in roots:
        url = (f"https://drive.google.com/file/d/{r['external_id']}/view" if r["kind"] == "file"
               else f"https://drive.google.com/drive/folders/{r['external_id']}")
        out_roots.append({"id": str(r["id"]), "name": r["name"] or "Untitled", "kind": r["kind"], "status": r["status"],
                          "status_note": r["status_note"], "added": r["created_at"], "via": r["added_via"],
                          "last_checked": r["last_synced_at"], "url": url, "counts": by_root.get(r["external_id"], {})})
    ids = [f["id"] for f in files]
    events = await db.fetch_all(
        """select id, at, item_id, title, event, detail, ms from index_events
           where item_id = any(%s::uuid[]) order by at desc limit 40""", (ids,)) if ids else []
    return {"roots": out_roots, "files": files, "counts": counts, "events": [dict(e) for e in events]}


async def _shared_with_me(person: access.Person) -> dict:
    files, counts = await file_states("shared_with", person, limit=1000)
    await _waits(files)
    return {"files": files, "counts": counts}


@mcp.custom_route("/me/api/state", methods=["GET"], include_in_schema=False)
async def me_state(request: Request) -> Response:
    person = await _person(request)
    if not person:
        return _json({"error": "Sign in again at /login?next=/me."}, 403)
    from nebula.connectors.gdrive import service_account_email

    return _json({
        "now": dt.datetime.now(dt.UTC), "me": {"email": person.email, "name": person.name, "admin": person.is_admin},
        "pulse": await _pulse(), "bot": service_account_email(),
        "mine": await _mine(person), "shared": await _shared_with_me(person),
    })


@mcp.custom_route("/me/api/action", methods=["POST"], include_in_schema=False)
async def me_action(request: Request) -> Response:
    person = await _person(request)
    if not person:
        return _json({"error": "Sign in again."}, 403)
    if not _same_origin(request) or "application/json" not in request.headers.get("content-type", ""):
        return _json({"error": "Bad request."}, 400)
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "Bad request."}, 400)
    from nebula import sources as src

    action = str(body.get("action") or "")
    try:
        if action == "reread_item":
            res = await src.request_sync(person, item=f"item:{body.get('item_id')}")
            return _json({"ok": True, "job": res.get("job"), "message": f"Re-reading {res.get('title') or 'that file'} now."})
        if action == "check_root":
            res = await src.request_sync(person, source=str(body.get("root_id") or ""))
            return _json({"ok": True, "job": res.get("job"), "message": f"Checking {res.get('name') or 'that source'} now."})
        if action == "add_link":
            link = str(body.get("link") or "").strip()
            if not link:
                raise ValueError("Paste a Drive link first.")
            res = await src.request_sync(person, link=link)
            name = res.get("title") or res.get("name") or "it"
            return _json({"ok": True, "job": res.get("job"), "message": f"Reading {name} now."})
    except (ValueError, access.AccessDenied) as e:
        return _json({"error": str(e)}, 400)
    return _json({"error": "Unknown action."}, 400)
