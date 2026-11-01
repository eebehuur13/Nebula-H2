"""A newer version Nebula can't read never replaces a readable one, and a file that leaves Drive takes its
text with it. Nebula keeps no copies of the files themselves."""

from __future__ import annotations

import io

from nebula import db, sync
from nebula.connectors.base import FetchedItem
from nebula.knowledge import items


def _docx(text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _file(data: bytes, name: str = "report.docx") -> FetchedItem:
    return FetchedItem(external_id="kept-file", title="Field report", url="", mime_type="", kind="doc",
                       data=data, filename=name)


async def test_unreadable_versions_and_deletions(mcp):
    cid = str((await sync.drive_space())["id"])
    async with db.tx() as conn:
        await items.upsert(conn, cid, _file(_docx("Visited 12 centres in Guntur.")))
    async with db.tx() as conn:
        second = await items.upsert(conn, cid, _file(_docx("Visited 14 centres in Guntur and Krishna.")))
    assert second["version"] == 2

    # A newer version Nebula can't read doesn't replace the readable one.
    async with db.tx() as conn:
        res = await items.upsert(conn, cid, _file(b"PK\x03\x04 not really a zip"))
    assert res["kept_previous"] and res["version"] == 2
    it = await items.get(second["id"])
    assert "couldn't read" in it["status_note"]
    assert "14 centres" in items.document_of(it).blocks[0].text

    # Deleted in Drive: its passages and text go; the row stays as a marker.
    async with db.tx() as conn:
        await items.mark_deleted(conn, cid, "kept-file")
    it = await items.get(second["id"])
    assert it["status"] == "deleted" and it["document"] is None
    assert not await db.fetch_one("select 1 as x from chunks where item_id=%s", (second["id"],))


async def test_a_file_nebula_cant_read_shows_as_failed_everywhere(mcp):
    from helpers import ADMIN, MEMBER, call, drive_file
    from nebula.knowledge import stages

    item_id = await drive_file("Broken report", data=b"PK\x03\x04 not really a zip", filename="broken.docx",
                               kind="doc", emails=[MEMBER])
    it = await items.get(item_id)
    assert it["status"] == "error" and it["status_note"].startswith("could not read this file")
    docs = await call(mcp, MEMBER, "documents", title="Broken report")
    assert docs["documents"][0]["state"] == "couldn't be read"
    st = await call(mcp, MEMBER, "sources", action="status", id=f"item:{item_id}")
    assert st["stage"] == "couldn't be read" and st["search_ready"] is False
    files, counts = await stages.file_states("all")
    assert next(f for f in files if f["id"] == item_id)["state"] == "failed" and counts["failed"] >= 1
    admin = await call(mcp, ADMIN, "documents", title="Broken report")
    assert admin["total"] == 1
