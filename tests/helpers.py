"""Shared helpers for the database-backed tests: call a tool as someone, add a Drive file, run queued jobs."""

from __future__ import annotations

import json
import re

from fastmcp import Client

from nebula import jobs
from nebula.config import settings

ADMIN = "admin@example.org"
MEMBER = "member@example.org"
OUTSIDER = "outsider@example.org"
SHARER = "sharer@example.org"


async def call(mcp, who: str, tool: str, **args):
    from nebula import server

    settings().dev_user_email = who
    server.forget_person()
    async with Client(mcp) as c:
        res = await c.call_tool(tool, args, raise_on_error=False)
    text = res.content[0].text if res.content else ""
    if res.is_error:
        return {"error": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        head, _, rest = text.partition("\n\n")
        try:
            return {**json.loads(head), "_body": rest}
        except json.JSONDecodeError:
            return {"_text": text}


async def raw_call(mcp, who: str, tool: str, **args):
    """The tool result itself, for checking non-text content such as images."""
    from nebula import server

    settings().dev_user_email = who
    server.forget_person()
    async with Client(mcp) as c:
        return await c.call_tool(tool, args, raise_on_error=False)


async def drive_file(title: str, text: str = "", *, emails=(), domains=(), groups=(), unknown: bool = False,
                     data: bytes | None = None, filename: str | None = None, mime: str = "text/plain",
                     kind: str = "file", external_id: str | None = None, root: str | None = None) -> str:
    """Store a file as if Drive sync had read it, shared with `emails` and `domains`. Returns its item id."""
    from nebula import db, sync
    from nebula.connectors.base import FetchedItem
    from nebula.knowledge import items

    space = await sync.drive_space()
    ext = external_id or "f-" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    fetched = FetchedItem(external_id=ext, title=title, url=f"https://drive.google.com/file/d/{ext}/view",
                          mime_type=mime, kind=kind, data=data if data is not None else text.encode(),
                          filename=filename or f"{title}.txt")
    async with db.tx() as conn:
        res = await items.upsert(conn, str(space["id"]), fetched)
        if root:
            await conn.execute("update items set source_root=%s where id=%s", (root, res["id"]))
        await items.apply_source_audience(conn, res["id"], {
            "emails": list(emails), "groups": list(groups), "domains": list(domains), "public": False,
            "unknown": unknown})
    return res["id"]


async def drain() -> None:
    from nebula import worker  # noqa: F401  (registers handlers)

    while await jobs.run_one():
        pass
