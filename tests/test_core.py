"""End-to-end tests of the core through the MCP tools, against a real local Postgres.

Needs the local pgvector container (see README) with a `nebula_core_test` database.
"""

from __future__ import annotations

import io

from helpers import ADMIN, MEMBER, OUTSIDER, call, drain, drive_file
from nebula import db
from nebula.config import settings


async def test_claude_sees_six_tools(mcp):
    from fastmcp import Client

    from nebula import server

    settings().dev_user_email = MEMBER
    server.forget_person()
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == {"search", "documents", "open", "query", "sources", "job"}


async def test_spreadsheet_from_drive_becomes_a_table(mcp):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Centres"
    ws.append(["District", "Month", "Active centres", "Children enrolled"])
    rows = [("Guntur", "2026-07-01", 410, 9800), ("Guntur", "2026-08-01", 425, 10100),
            ("Krishna", "2026-07-01", 380, 8700), ("Krishna", "2026-08-01", 390, 9050)]
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    await drive_file("AP centres by month", data=buf.getvalue(), filename="ap_centres.xlsx", kind="sheet",
                     mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", emails=[MEMBER])
    await drain()

    r = await call(mcp, MEMBER, "search", query="AP centres by month active centres", types=["dataset"])
    ds = r["results"][0]
    assert ds["type"] == "dataset"
    d = await call(mcp, MEMBER, "open", id=ds["id"])
    cols = {c["name"] for c in d["columns"]}
    assert "active_centres" in cols
    q = await call(mcp, MEMBER, "query",
                   sql=f"select district, sum(active_centres) as total from {d['table']} group by district order by district")
    assert q["rows"] == [["Guntur", 835], ["Krishna", 770]]
    # Someone Drive doesn't share it with can't find, open or query it, and nobody can read Nebula's own tables
    r = await call(mcp, OUTSIDER, "search", query="AP centres by month active centres", types=["dataset"])
    assert not any(x["id"] == ds["id"] for x in r["results"])
    assert "error" in await call(mcp, OUTSIDER, "open", id=ds["id"])
    assert "error" in await call(mcp, OUTSIDER, "query", sql=f"select * from {d['table']}")
    assert "error" in await call(mcp, MEMBER, "query", sql="select * from people")
    assert "error" in await call(mcp, MEMBER, "query", sql="delete from people")


async def test_search_returns_short_files_whole(mcp):
    """A short file comes back complete; a long one comes back as excerpts with a note to open it."""
    story = "Meera built a small robot called Pico. " * 60  # ~2,500 characters, like the Drive test file
    survey = " ".join(f"Paragraph {i}: the zebra survey counted herds near river bend {i}." for i in range(200))
    ids = {title: f"item:{await drive_file(title, text, emails=[MEMBER])}"
           for title, text in (("Robot story", story), ("Zebra survey", survey))}
    await drain()

    r = await call(mcp, MEMBER, "search", query="robot Pico story", types=["item"])
    hit = next(x for x in r["results"] if x["id"] == ids["Robot story"])
    assert hit["complete"] is True and "matches" not in hit
    assert hit["content"].count("Pico") == 60

    r = await call(mcp, MEMBER, "search", query="zebra survey river bend", types=["item"])
    hit = next(x for x in r["results"] if x["id"] == ids["Zebra survey"])
    assert hit["matches"] and "complete" not in hit
    assert "Open an id" in r["note"]


async def test_web_pages_show_only_files_drive_shares(mcp):
    from starlette.testclient import TestClient

    from nebula import server

    item_id = await drive_file("Field visit notes", "We visited the centre in Guntur.", emails=[MEMBER])
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    settings().dev_user_email = MEMBER
    server.forget_person()
    with TestClient(app) as c:
        r = c.get("/web")
        assert r.status_code == 200 and "Field visit notes" in r.text
        r = c.get(f"/item/{item_id}")
        assert r.status_code == 200 and "visited the centre" in r.text
    settings().dev_user_email = OUTSIDER
    server.forget_person()
    with TestClient(app) as c:
        assert "Field visit notes" not in c.get("/web").text
        assert "Not found" in c.get(f"/item/{item_id}").text


async def test_drive_sharing_is_mirrored(mcp, monkeypatch):
    """Drive sharing decides who sees each file; sharing a folder with Nebula doesn't reveal it to the sharer."""
    from nebula import sync
    from nebula.connectors import gdrive

    folder = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"

    class FakeConnector:
        def __init__(self, config):
            self.config = config

        async def describe(self, file_id):
            return {"id": file_id, "name": "Nebula Test", "mimeType": gdrive.FOLDER}

        async def covering_root(self, meta, cursor=None):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(sync, "get_connector", lambda kind, config: FakeConnector(config))
    monkeypatch.setattr(gdrive, "service_account_email", lambda: "nebula-drive@example.iam.gserviceaccount.com")
    r = await call(mcp, OUTSIDER, "sources", action="connect", link=f"https://drive.google.com/drive/folders/{folder}")
    source = r["source"]
    assert r["kind"] == "folder" and r["name"] == "Nebula Test" and r["job"] and r["estimate"]
    again = await call(mcp, OUTSIDER, "sources", action="connect", link=folder)
    assert again["already_connected"] and again["source"] == source

    # This permission test seeds the stored item directly instead of calling real Drive.
    await db.execute("update jobs set status='done' where kind='sync' and status='queued'")
    item_id = await drive_file("Meera Story", "Meera grew up in Chennai and loves robots.", emails=[MEMBER],
                               external_id="f-story", root=folder)
    await drain()
    item = f"item:{item_id}"

    r = await call(mcp, MEMBER, "search", query="Meera story robots")
    assert any(x["id"] == item for x in r["results"])
    # The person who shared the folder isn't on the file's Drive sharing, so they can't read it,
    # but they can still manage the source.
    r = await call(mcp, OUTSIDER, "search", query="Meera story robots")
    assert not any(x["id"] == item for x in r["results"])
    assert "error" in await call(mcp, OUTSIDER, "open", id=item)
    r = await call(mcp, OUTSIDER, "sources", action="status", source=source)
    assert r["files"]["in_nebula"] == 1 and r["recent"] == []
    r = await call(mcp, MEMBER, "sources", action="status", source=source)
    assert "error" in r  # only whoever shared it (or an admin) manages it
    r = await call(mcp, MEMBER, "sources", action="status", id=item)
    assert r["title"] == "Meera Story" and r["sharing"].startswith("Follows Drive")
    assert (await call(mcp, ADMIN, "open", id=item))["title"] == "Meera Story"  # admins can open everything

    # Drive changes replace what was mirrored.
    from nebula.knowledge import items

    async with db.tx() as conn:
        await items.apply_source_audience(conn, item_id, {
            "emails": [OUTSIDER], "groups": [], "domains": [], "public": False, "unknown": False})
    assert "error" in await call(mcp, MEMBER, "open", id=item)  # removed in Drive -> removed here
    assert (await call(mcp, OUTSIDER, "open", id=item))["title"] == "Meera Story"

    # The RL domain on Drive means everyone at RL
    async with db.tx() as conn:
        await items.apply_source_audience(conn, item_id, {
            "emails": [], "groups": [], "domains": ["example.org"], "public": False, "unknown": False})
    assert (await call(mcp, MEMBER, "open", id=item))["title"] == "Meera Story"

    r = await call(mcp, OUTSIDER, "sources", action="list")
    assert [x["source"] for x in r["sources"]] == [source]
    r = await call(mcp, OUTSIDER, "sources", action="disconnect", source=source)
    assert r["status"] == "disconnected" and r["files_removed"] == 1
    await db.execute("update jobs set status='cancelled' where kind='sync' and status='queued'")
    r = await call(mcp, MEMBER, "search", query="Meera story robots")
    assert not any(x["id"] == item for x in r["results"])
    r = await call(mcp, OUTSIDER, "sources", action="list")
    assert r["sources"] == []


async def test_drive_reads_only_what_changed(mcp):
    """Unchanged files aren't downloaded again, and sharing is re-read only when it may have changed."""
    from nebula import access, sync
    from nebula.connectors.base import Change, FetchedItem

    reader = await access.ensure_person("drive-refresh-reader@example.com")
    col = await sync.drive_space()
    audience = {"emails": [reader.email], "groups": [], "domains": [], "public": False, "unknown": False}

    class Connector:
        fetches = reads = 0
        sharing = audience

        async def fetch(self, ch):
            self.fetches += 1
            return FetchedItem(external_id="unchanged-file", title="ACL refresh regression", url="",
                               mime_type="text/plain", kind="file", data=b"Permission refresh regression test.",
                               filename="acl.txt", extra={"audience": self.sharing, "rev": ch.rev, "acl": ch.acl})

        async def file_audience(self, external_id):
            assert external_id == "unchanged-file"
            self.reads += 1
            return self.sharing

    def change(**kw):
        return Change(external_id="unchanged-file", title="ACL refresh regression", **{"rev": "r1", "acl": "p1,p2", **kw})

    c = Connector()
    outcome, item_id = await sync.process_change(col, c, change())
    assert outcome == "changed" and c.fetches == 1 and c.reads == 0
    assert await access.can_open(reader, item_id)
    # Same revision, same sharing: nothing downloaded or re-read.
    assert (await sync.process_change(col, c, change()))[0] == "unchanged"
    assert (c.fetches, c.reads) == (1, 0)
    # Sharing changed in Drive (new fingerprint): re-read, but the file isn't downloaded.
    c.sharing = {**audience, "emails": []}
    await sync.process_change(col, c, change(acl="p1"))
    assert (c.fetches, c.reads) == (1, 1)
    assert not await access.can_open(reader, item_id)
    # A folder above it was re-shared: re-read although its own fingerprint didn't change.
    c.sharing = audience
    await sync.process_change(col, c, change(acl="p1", recheck_acl=True))
    assert c.reads == 2 and await access.can_open(reader, item_id)
    # No fingerprint from Drive: re-read on full listings only. The daily sweep re-reads everything.
    await sync.process_change(col, c, change(acl=None))
    assert c.reads == 2
    await sync.process_change(col, c, change(acl=None), full=True)
    assert c.reads == 3
    await sync.process_change(col, c, change(acl="p1"), sweep=True)
    assert c.reads == 4
    # New content: downloaded again.
    await sync.process_change(col, c, change(rev="r2", acl="p1"))
    assert c.fetches == 2
    # Unreadable sharing hides the file again.
    c.sharing = {"emails": [], "groups": [], "domains": [], "public": False, "unknown": True}
    await sync.process_change(col, c, change(rev="r2", acl="p0"))
    assert not await access.can_open(reader, item_id)
