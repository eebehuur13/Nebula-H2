"""The Nebula MCP server: the tools Claude sees, sign-in, and the HTTP routes around them.

Claude on the web ignores a connector's general instructions and only reads each tool's
name and description, so the guidance on when and how to use Nebula lives in the tool
descriptions below. Keep them accurate; they are the product's manual for Claude.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import uuid
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp_types import ToolAnnotations
from pydantic import Field

from nebula import access, activity, db, jobs, limits, trace
from nebula.access import AccessDenied, Person
from nebula.config import settings
from nebula.knowledge import datasets, inventory, items, render, search as search_mod

log = logging.getLogger(__name__)

# Identity ------------------------------------------------------------------------------------

_person_cache: dict[str, tuple[float, Person]] = {}


async def me() -> Person:
    """The signed-in person for this request (cached for a minute)."""
    s = settings()
    if s.auth_mode == "dev":
        email, name, sub = s.dev_user_email, "Dev User", None
    else:
        token = get_access_token()
        claims = (token.claims if token else {}) or {}
        upstream = claims.get("upstream_claims") or {}
        email = claims.get("email") or upstream.get("email")
        name = claims.get("name") or upstream.get("name") or ""
        sub = upstream.get("sub") or claims.get("sub")
        if not email:
            raise ToolError("Nebula couldn't tell who you are. Reconnect the Nebula connector and sign in again.")
        verified = claims.get("email_verified", upstream.get("email_verified"))
        if verified is not True and verified != "true":
            raise ToolError("Nebula needs a verified Google email address. Reconnect after verifying it with Google.")
    email = email.lower()
    if not s.email_allowed(email):
        raise ToolError(f"Nebula is for Rocket Learning accounts; {email} isn't allowed. Sign in with your RL account.")
    hit = _person_cache.get(email)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        person = await access.ensure_person(email, name, sub)
    except AccessDenied as e:
        raise ToolError(str(e)) from e
    _person_cache[email] = (time.time() + 60, person)
    return person


def forget_person(email: str | None = None) -> None:
    if email:
        _person_cache.pop(email.lower(), None)
    else:
        _person_cache.clear()


def _out(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=_default, indent=1)


def _default(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return str(v)


def _fail(e: Exception) -> ToolError:
    if isinstance(e, ToolError):
        return e
    if isinstance(e, (AccessDenied, ValueError, datasets.QueryError, PermissionError)):
        return ToolError(str(e))
    log.exception("tool failed")
    return ToolError(f"Nebula hit an internal error ({type(e).__name__}). Try again; if it persists, tell an admin.")


READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

mcp = FastMCP(name="Nebula", version="0.1.0")


def _bounded(result: Any) -> Any:
    """Keep any tool's text under the limit Claude can take in one result, and say so when cutting."""
    limit = settings().max_result_chars
    content = getattr(result, "content", None)
    if not content or sum(len(getattr(c, "text", "") or "") for c in content) <= limit:
        return result
    out, used = [], 0
    for c in content:
        text = getattr(c, "text", None)
        if text is None:
            out.append(c)
            continue
        room = max(0, limit - used)
        if len(text) > room:
            text = text[:room] + (f"\n\n[Nebula cut this result at {limit:,} characters. Ask for less: a page or part, "
                                  "a smaller limit, or fewer rows.]")
            c = c.model_copy(update={"text": text})
        used += len(text)
        out.append(c)
    return ToolResult(content=out, structured_content=getattr(result, "structured_content", None),
                      meta=getattr(result, "meta", None))


def _client_app(params: Any = None) -> str:
    """Which app is calling: the MCP client's own name if it sent one, else its user agent."""
    info = getattr(params, "clientInfo", None) or getattr(params, "client_info", None)
    if info is not None and getattr(info, "name", None):
        version = getattr(info, "version", "") or ""
        return f"{info.name} {version}".strip()
    try:
        from fastmcp.server.dependencies import get_http_request

        return get_http_request().headers.get("user-agent", "")[:160]
    except Exception:
        return ""


class ActivityMiddleware(Middleware):
    """Rate-limits each person and records every call (and each search's steps) for the admin page."""

    async def on_initialize(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        try:
            person = await me()
            params = getattr(context.message, "params", context.message)
            await activity.note_client(person.id, _client_app(params), connect=True)
        except Exception:  # sign-in problems surface on the next real request, not here
            log.debug("initialize: could not note the client", exc_info=True)
        return result

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        try:
            person = await me()
        except ToolError:
            return tools
        await activity.touch_clients(person.id)
        return tools

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = getattr(context.message, "name", "")
        args = getattr(context.message, "arguments", None) or {}
        person = await me()
        started = time.perf_counter()
        client = _client_app()
        await activity.touch_clients(person.id)
        result = None
        error = ""
        steps, token = trace.open_trace() if name == "search" else (None, None)
        try:
            try:
                await limits.consume(person.id, "tool_calls")
            except limits.LimitExceeded as e:
                raise ToolError(str(e)) from e
            result = _bounded(await call_next(context))
            if getattr(result, "is_error", False):
                error = next((getattr(c, "text", "") for c in getattr(result, "content", None) or []), "") or "error"
            return result
        except Exception as e:
            error = str(e) or type(e).__name__
            raise
        finally:
            if token is not None:
                trace.close_trace(token)
            await activity.record_tool_call(
                person_id=person.id, tool=name, args=args, ok=not error, error=error,
                ms=int((time.perf_counter() - started) * 1000), result=result, client=client,
                trace=trace.finished(steps))


mcp.add_middleware(ActivityMiddleware())


# Tools -----------------------------------------------------------------------------------------


@mcp.tool(annotations=READ, output_schema=None)
async def search(
    query: Annotated[str, Field(description="What to look for, in plain words. Questions are fine.")],
    types: Annotated[list[Literal["item", "dataset"]] | None,
                     Field(description="Limit to kinds: item (documents, sheets, slides, PDFs), dataset "
                                       "(spreadsheet tables you can query with SQL).")] = None,
    since: Annotated[str | None, Field(description="Only material dated on/after this day (YYYY-MM-DD).")] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 8,
) -> str:
    """Search Nebula, Rocket Learning's shared Google Drive documents and spreadsheet tables that the
    person can open.

    Use this FIRST for any question about RL — its programmes, states, partners, teams, past decisions,
    numbers, processes or people's work — instead of answering from memory or general knowledge.
    Also use it first whenever the person mentions a document or file by name, or asks whether you have,
    can see or can find something: search Nebula before saying you don't have it. To list the documents a
    person can see, use `documents`.
    If the material may be in Hindi or another Indian language, search again in that language too.

    Results carry ids like 'item:…' and 'dataset:…'. A short file comes back whole (`complete: true`).
    Everything else comes back as short excerpts, so `open` it before summarising, retelling or answering
    about the whole thing. Use `query` for numbers in a dataset. In your answer, cite what you used (title
    and date). If nothing relevant comes back, say Nebula doesn't have it rather than guessing. Don't use
    Nebula for general knowledge or for work you can do yourself (writing, analysis, making documents,
    charts or files)."""
    try:
        person = await me()
        since_d = dt.date.fromisoformat(since) if since else None
        res = await search_mod.search(person, query, types=list(types) if types else None, limit=limit, since=since_d)
        return _out(res)
    except Exception as e:
        raise _fail(e) from e


@mcp.tool(annotations=READ, output_schema=None)
async def documents(
    title: Annotated[str | None, Field(description="Only documents whose title contains these words.")] = None,
    folder: Annotated[str | None, Field(description="Only documents in a folder whose name contains this.")] = None,
    shared: Annotated[Literal["all", "by_me", "with_me"], Field(
        description="'by_me': what this person shared with Nebula; 'with_me': what others shared with them.")] = "all",
    order: Annotated[Literal["folder", "recent"], Field(
        description="'folder' groups by folder and title; 'recent' puts the latest changes first.")] = "folder",
    page: Annotated[int, Field(ge=1, description="Which page of the list; replies say how many pages there are.")] = 1,
    page_size: Annotated[int, Field(ge=1, le=200)] = 50,
) -> str:
    """List the documents and files in Nebula that this person can open: the Google Drive files Drive
    shares with them.

    Use this whenever someone asks what documents or files you have or can see, e.g. "list the documents
    you have", "what files can you access?", "what's in Nebula?", "what did I share?", "what's in the
    pilot folder?", or wants a count or inventory of files. Each entry has its id, title, format, folder,
    who shared it, when it last changed, and whether it's searchable yet. The list is paged: if
    `next_page` is present, ask for it before saying the list is complete. To read a document, `open` its
    id. To find what documents say about something, use `search` instead."""
    try:
        person = await me()
        return _out(await inventory.list_documents(person, title=title, folder=folder, shared=shared, order=order,
                                                   page=page, page_size=page_size))
    except Exception as e:
        raise _fail(e) from e


@mcp.tool(annotations=READ, output_schema=None, meta={"anthropic/maxResultSizeChars": 120_000})
async def open(  # noqa: A001 - the tool is called "open" on purpose
    id: Annotated[str, Field(description="An id from search or documents, e.g. 'item:…' or 'dataset:…'.")],
    part: Annotated[str | None, Field(description="'page:3', 'pages:3-5', 'slide:5', 'sheet:Q2' or "
                                                  "'section:Heading text'.")] = None,
    page: Annotated[int, Field(ge=1, description="Which chunk of a long result to return.")] = 1,
    as_image: Annotated[bool, Field(description="Items: return `part` ('page:3' of a PDF, 'slide:5', or 'image:2') as a "
                                                "picture, to read what text extraction missed.")] = False,
) -> str | list:
    """Read one Nebula file or dataset with its structure intact.

    - item: a Drive file — documents by page, slides by number, sheets as tables (with the dataset id to
      query)
    - dataset: its card, columns and types, and sample rows (use `query` for numbers)

    Long items come in chunks; pass `page` to continue. Quote and cite precisely (title, page/slide/section,
    date).

    If an item lists `extraction.parts_not_read` (scanned pages, pictures, charts) and those parts matter for
    the question, open each with `part` and as_image=true and read it."""
    try:
        person = await me()
        kind, rid = items.parse_ref(id)
        await access.require(person, kind, rid)
        if kind == "dataset":
            return await _open_dataset(rid)
        if as_image:
            return await _open_item_image(rid, part)
        return await _open_item(rid, part, page, 40_000)
    except Exception as e:
        raise _fail(e) from e


async def _open_item(rid: str, part: str | None, page: int, size: int) -> str:
    it = await items.get(rid)
    if it is None:
        raise ValueError("That file isn't in Nebula any more.")
    head = {
        "id": f"item:{rid}", "title": it["title"], "kind": it["kind"], "url": it["url"],
        "path": " / ".join(it["path"] or []), "author": it["author"],
        "modified": it["source_modified_at"], "version": it["version"], "status": it["status"],
    }
    if it["status_note"]:
        head["note"] = it["status_note"]
    ds = await db.fetch_all("select id, sheet, table_name, row_count from datasets where item_id=%s", (rid,))
    if ds:
        head["datasets"] = [{"id": f"dataset:{d['id']}", "sheet": d["sheet"], "table": f"data.{d['table_name']}",
                             "rows": d["row_count"]} for d in ds]
    doc = items.document_of(it)
    if doc is None:
        return _out({**head, "content": None, "hint": "No readable text. Open it in Drive with the url."})
    text, label = render.document_markdown(doc, part)
    extraction = items.extraction_summary(doc)
    if extraction:
        head["extraction"] = extraction
    chunk, total = render.paginate(text, page, size)
    head["showing"] = (label + " · " if label else "") + f"chunk {min(page, total)} of {total}"
    if doc.metadata:
        head["metadata"] = {k: v for k, v in doc.metadata.items() if k in
                            ("page_count", "slide_count", "sheet_names", "needs_ocr", "word_count")}
    return _out(head) + "\n\n" + chunk


async def _open_item_image(rid: str, part: str | None) -> list:
    from fastmcp.utilities.types import Image

    from nebula.ingest import images

    from nebula import sync

    if not part:
        raise ValueError("Give the part to show, e.g. 'page:3', 'slide:5' or 'image:2'.")
    it = await items.get(rid)
    if it is None or it["status"] == "deleted":
        raise ValueError("That file isn't in Nebula any more.")
    data, filename, newer = await sync.fetch_original(it)  # Nebula keeps no copies; Drive has the file
    try:
        pictures = await asyncio.to_thread(images.render_part, data, filename, part)
    except images.NoImage as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        raise ValueError(f"Nebula couldn't render {part} of this file ({type(e).__name__}).") from e
    head = {"id": f"item:{rid}", "title": it["title"], "version": it["version"], "part": part,
            "pictures": len(pictures), "next": "Read what the picture says. Transcribe; don't summarise or guess."}
    if newer:
        head["note"] = ("This picture is from the current file in Drive, which changed after Nebula last read it, "
                        "so page and slide numbers may not match the text. Refresh it with sources action 'sync'.")
    return [_out(head), *(Image(data=b, format=fmt) for b, fmt in pictures)]


async def _open_dataset(rid: str) -> str:
    d = await db.fetch_one(
        "select d.*, i.title as item_title, i.url from datasets d join items i on i.id=d.item_id where d.id=%s",
        (rid,),
    )
    if d is None:
        raise ValueError("That dataset isn't in Nebula any more.")
    sample = await db.fetch_all(f'select * from data."{d["table_name"]}" limit 5')  # noqa: S608
    return _out({
        "id": f"dataset:{rid}", "title": d["title"], "table": f"data.{d['table_name']}", "rows": d["row_count"],
        "columns": d["columns"], "source": {"title": d["item_title"], "url": d["url"]}, "card": d["card"],
        "sample_rows": sample, "updated": d["updated_at"],
        "hint": "Use the query tool with SQL against this table. Column names are the 'name' fields above.",
    })


@mcp.tool(annotations=READ, output_schema=None)
async def query(
    sql: Annotated[str, Field(description="One SELECT over data.<table> dataset tables, in DuckDB SQL (close to "
                                          "PostgreSQL; use strftime, not to_char).")],
    max_rows: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> str:
    """Answer number questions from RL's spreadsheet data with SQL (DuckDB, read-only).

    Find datasets with `search` (type 'dataset' results show a table like data.t_…), then `open` the dataset to
    see its columns, types and sample rows before writing SQL. Use this for counts, sums, averages, trends and
    comparisons by state, district, programme or period — never estimate numbers from text snippets.
    State the period, filters and source table in your answer."""
    try:
        person = await me()
        return _out(await datasets.run_query(person, sql, max_rows))
    except Exception as e:
        raise _fail(e) from e


def _sources_description() -> str:
    from nebula.connectors.gdrive import service_account_email

    bot = service_account_email() or "Nebula's Google service account"
    return f"""Bring Google Drive material into Nebula, refresh it, and check on it.

    Sharing is enough: when someone at RL shares a Drive folder or a single file with {bot} as an
    Editor, Nebula notices within a few minutes, imports it and keeps it in step with Drive (edits
    show up within minutes). A Shared Drive needs one more step: after adding that address as a
    member, connect it here with its link, because Drive doesn't say who added Nebula to it. Editor is needed because Drive only shows who a file is
    shared with to editors; Nebula itself only reads. A link alone isn't enough: Drive has to share it
    with that address. If a result carries a `warning` or `note` about Viewer access, pass it on.

    Use this tool when the person wants something now or wants to know where it stands:
    - 'sync' with `link` (a Drive file, folder or Shared Drive link) or `id` (item:…): import or refresh it
      right away, e.g. "I've updated this doc, use the new version" or "save this file to Nebula". With
      `source` (from 'list'): re-check a whole shared folder.
    - 'status' with `link` or `id`: is this file in Nebula, up to date, searchable, and fully read? With
      `source`: a shared folder's progress.
    - 'connect' with `link`: the same as sync, for something not in Nebula yet.
    - 'list': the folders, files and drives this person has shared with Nebula (not every document they
      can see: for that, use the documents tool).
    - 'disconnect' with `source`: stop indexing it (Drive itself is untouched).
    sync and connect return a job and a rough wait. Tell the person how long it should take, and don't
    treat the new version as available until the job (check it with the job tool) says done. A very long
    file can finish reading with some pieces still being made searchable; the job's result says so
    (`ready: false` and a note), and 'status' shows when it's fully searchable.
    Every reply includes `watch`: a link to the person's own "My files" page, where they can see each of
    their files being read and made searchable, and files others shared with them. Give them that link
    whenever they add or refresh something, or ask how it's going.

    Access follows Drive: each file is visible in Nebula only to the people Drive shares it with (by
    email, or everyone at RL when it's shared with the RL domain), and changes when Drive sharing
    changes. Sharing with a Google Group doesn't reach its members yet. Sharing a folder with Nebula
    never shows its files to anyone Drive doesn't already let open them."""


@mcp.tool(name="sources", annotations=WRITE, output_schema=None, description=_sources_description())
async def sources_tool(
    action: Annotated[Literal["list", "sync", "status", "connect", "disconnect"], Field(description="What to do.")],
    link: Annotated[str | None, Field(description="A Google Drive link (file, folder, Docs/Sheets/Slides or "
                                                  "Shared Drive) or id.")] = None,
    id: Annotated[str | None, Field(description="status/sync: a Nebula item id (item:…) for one file.")] = None,
    source: Annotated[str | None, Field(description="status/sync/disconnect: a source id from 'list'.")] = None,
    name: Annotated[str | None, Field(description="connect: a name for it (default: its name in Drive).")] = None,
) -> str:
    from nebula import sources
    from nebula.me import watch_url

    def watched(res: Any) -> str:
        if isinstance(res, dict):
            res["watch"] = watch_url()
        return _out(res)

    try:
        person = await me()
        if action == "list":
            return watched(await sources.list_sources(person))
        if action == "connect":
            if not link:
                raise ValueError("Give the Drive link.")
            return watched(await sources.connect(person, link, name))
        if action == "sync":
            return watched(await sources.request_sync(person, link=link, item=id, source=source))
        if action == "status":
            return watched(await sources.status(person, link=link, item=id, source=source))
        if action == "disconnect":
            if not source and not link:
                raise ValueError("Give the source (see action 'list').")
            return _out(await sources.disconnect(person, source or link or ""))
        raise ValueError("Unknown action.")
    except Exception as e:
        raise _fail(e) from e


@mcp.tool(annotations=READ, output_schema=None)
async def job(id: Annotated[str, Field(description="The job id.")]) -> str:
    """Check a long-running Nebula job (a sync or a file refresh): status, a rough wait while it's queued
    or running, and the result when it's done."""
    try:
        person = await me()
        try:
            uuid.UUID(id)
        except ValueError:
            raise ValueError("No such job of yours.") from None
        j = await jobs.get(id)
        if not j or (not person.is_admin and (not j["person_id"] or str(j["person_id"]) != person.id)):
            raise ValueError("No such job of yours.")
        out = {k: j[k] for k in ("id", "kind", "status", "progress", "message", "result", "updated_at")}
        if j["status"] in ("queued", "running"):
            out.update(await jobs.eta(str(j["id"])))
        elif j["status"] == "failed":
            out["error"] = (j["error"] or "").split("\n", 1)[0][:500]
        return _out(out)
    except Exception as e:
        raise _fail(e) from e
