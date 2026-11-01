"""What the admin page shows: tool calls, connections, indexing events, the audit log, refresh
timings and the worker's heartbeat.

Recording here must never break or slow down the thing being recorded, so every write is
wrapped and failures are only logged.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from psycopg import AsyncConnection

from nebula import db

log = logging.getLogger(__name__)

# Refresh timings the worker uses, in seconds. An admin can change them on the admin page.
REFRESH_DEFAULTS: dict[str, int] = {
    "discover_seconds": 120,      # look for folders and files newly shared with Nebula
    "sync_seconds": 120,          # check Drive for edits, new files and sharing changes
    "embed_seconds": 120,         # make newly read text searchable
    "full_sync_minutes": 60,      # list everything again (catches anything the change feed missed)
}
REFRESH_LIMITS: dict[str, tuple[int, int]] = {
    "discover_seconds": (15, 3600),
    "sync_seconds": (15, 3600),
    "embed_seconds": (15, 3600),
    "full_sync_minutes": (5, 24 * 60),
}

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_SECONDS = 5.0


# Settings ------------------------------------------------------------------------------------


async def get_setting(key: str, default: Any = None) -> Any:
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        row = await db.fetch_one("select value from app_settings where key=%s", (key,))
    except Exception:  # the table may not exist yet while migrations run
        log.debug("app_settings read failed", exc_info=True)
        row = None
    value = row["value"] if row else default
    _cache[key] = (time.monotonic() + _CACHE_SECONDS, value)
    return value


async def set_setting(key: str, value: Any, person_id: str | None = None) -> None:
    await db.execute(
        """insert into app_settings (key, value, updated_by) values (%s, %s, %s)
           on conflict (key) do update set value=excluded.value, updated_at=now(), updated_by=excluded.updated_by""",
        (key, db.jsonb(value), person_id),
    )
    _cache.pop(key, None)


async def refresh_settings() -> dict[str, int]:
    stored = await get_setting("refresh", {}) or {}
    out = dict(REFRESH_DEFAULTS)
    for k, (lo, hi) in REFRESH_LIMITS.items():
        if isinstance(stored.get(k), (int, float)):
            out[k] = int(min(hi, max(lo, stored[k])))
    return out


async def heartbeat(name: str, job: dict | None = None) -> None:
    try:
        await set_setting(f"heartbeat:{name}", {"at": time.time(), "job": job})
    except Exception:
        log.debug("heartbeat failed", exc_info=True)


# Audit log -----------------------------------------------------------------------------------


async def audit(conn: AsyncConnection | None, person: Any, action: str, rtype: str | None = None,
                rid: str | None = None, detail: dict | None = None) -> None:
    """Record a sign-in, a source added or removed, or an admin-page action."""
    await db.execute(
        "insert into audit_log (person_id, action, resource_type, resource_id, detail) values (%s,%s,%s,%s,%s)",
        (person.id if person else None, action, rtype, rid, db.jsonb(detail or {})),
        conn,
    )


# Tool calls ----------------------------------------------------------------------------------


def _trim(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:300] + f"… ({len(value):,} characters)"
    if isinstance(value, dict) and depth < 3:
        return {k: _trim(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list) and depth < 3:
        return [_trim(v, depth + 1) for v in value[:20]]
    return value


def describe(tool: str, args: dict[str, Any]) -> tuple[str, str, str]:
    """(action, summary, target) for one tool call, in plain words for the activity feed."""
    a = args or {}
    action = str(a.get("action") or "")
    if tool == "search":
        return "", str(a.get("query") or ""), ""
    if tool == "open":
        part = f" ({a['part']})" if a.get("part") else ""
        extra = " as a picture" if a.get("as_image") else ""
        return "", f"{a.get('id', '')}{part}{extra}", str(a.get("id") or "")
    if tool == "query":
        return "", " ".join(str(a.get("sql") or "").split())[:240], ""
    if tool == "sources":
        return action, str(a.get("link") or a.get("id") or a.get("source") or a.get("name") or ""), \
            str(a.get("link") or a.get("id") or a.get("source") or "")
    if tool == "job":
        return "", str(a.get("id") or ""), str(a.get("id") or "")
    if tool == "documents":
        bits = [f"title has '{a['title']}'" if a.get("title") else "", f"in '{a['folder']}'" if a.get("folder") else "",
                {"by_me": "shared by them", "with_me": "shared with them"}.get(str(a.get("shared") or ""), ""),
                f"page {a['page']}" if a.get("page", 1) != 1 else ""]
        return "", " · ".join(b for b in bits if b), ""
    return action, "", ""


def _reply(result: Any) -> Any:
    """The JSON a tool sent back, or None."""
    content = getattr(result, "content", None) or []
    text = next((getattr(c, "text", None) for c in content if getattr(c, "text", None)), None)
    if not text:
        return None
    try:
        return json.loads(text.split("\n\n", 1)[0])
    except (ValueError, TypeError):
        return None


def result_count(result: Any) -> int | None:
    """How many things a tool returned (search results, rows, sources), when that's a clear number."""
    data = _reply(result)
    if isinstance(data, dict):
        if isinstance(data.get("documents"), list) and isinstance(data.get("total"), int):
            return data["total"]
        for key in ("results", "rows", "sources"):
            if isinstance(data.get(key), list):
                return len(data[key])
    return None


def surfaced(tool: str, result: Any) -> list[dict] | None:
    """For a search, the results in the order Claude got them: what each one was and its score."""
    data = _reply(result) if tool == "search" else None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return None
    keep = ("id", "title", "type", "score", "url", "path")
    return [{k: r[k] for k in keep if r.get(k) not in (None, "")} for r in data["results"][:25] if isinstance(r, dict)]


async def record_tool_call(*, person_id: str | None, tool: str, args: dict[str, Any], ok: bool, error: str,
                           ms: int, result: Any = None, client: str = "", trace: dict | None = None) -> None:
    try:
        action, summary, target = describe(tool, args)
        hits = surfaced(tool, result) if ok else None
        await db.execute(
            """insert into tool_calls (person_id, tool, action, summary, target, args, ok, error, result_count, ms,
                                       client, results, trace)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (person_id, tool, action, summary[:500], target[:300], db.jsonb(_trim(args)), ok, error[:500],
             result_count(result) if ok else None, ms, client[:160], None if hits is None else db.jsonb(hits),
             None if trace is None else db.jsonb(trace)),
        )
    except Exception:
        log.warning("could not record a %s call", tool, exc_info=True)


_seen_clients: dict[tuple[str, str], float] = {}


async def note_client(person_id: str, client: str, *, connect: bool = False) -> None:
    """Remember that this person uses Nebula from this app. `connect` marks a new connection."""
    client = (client or "unknown app")[:160]
    key = (person_id, client)
    if not connect and _seen_clients.get(key, 0) > time.monotonic():
        return
    _seen_clients[key] = time.monotonic() + 60
    try:
        await db.execute(
            """insert into mcp_clients (person_id, client) values (%s, %s)
               on conflict (person_id, client) do update set last_seen_at=now(),
                   connects = mcp_clients.connects + %s""",
            (person_id, client, 1 if connect else 0),
        )
    except Exception:
        log.warning("could not record a connection", exc_info=True)


async def touch_clients(person_id: str) -> None:
    """A request arrived over MCP: update when this person's recorded connections were last seen.
    Only `initialize` creates connection rows, so one person isn't listed once per user agent."""
    key = (person_id, "*")
    if _seen_clients.get(key, 0) > time.monotonic():
        return
    _seen_clients[key] = time.monotonic() + 60
    try:
        await db.execute("update mcp_clients set last_seen_at=now() where person_id=%s", (person_id,))
    except Exception:
        log.warning("could not update a connection", exc_info=True)


# Indexing ------------------------------------------------------------------------------------


async def index_event(event: str, *, item_id: str | None = None, title: str = "", collection_id: str | None = None,
                      detail: str = "", ms: int | None = None, job_id: str | None = None) -> None:
    try:
        await db.execute(
            """insert into index_events (item_id, title, collection_id, event, detail, ms, job_id)
               values (%s, %s, %s, %s, %s, %s, %s)""",
            (item_id, title[:300], collection_id, event, detail[:500], ms, job_id),
        )
    except Exception:
        log.warning("could not record an indexing event", exc_info=True)
