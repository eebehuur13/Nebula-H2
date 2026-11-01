"""The "My files" page: each person sees what they shared and what's shared with them, nothing else."""

from __future__ import annotations

import httpx
from helpers import SHARER, MEMBER, OUTSIDER

from nebula import access, db, sync
from nebula.config import settings


def _client():
    from nebula import admin, me, server, web, webui  # noqa: F401  (register the routes)

    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _setup():
    space = await sync.drive_space()
    member = await access.ensure_person(MEMBER, "Member Person")
    sharer = await access.ensure_person(SHARER, "Sharer Person")
    await access.ensure_person(OUTSIDER, "Outsider")
    for ext, who, name in (("me-root-01", member, "Member's folder"), ("sharer-root-01", sharer, "Sharer's folder")):
        await db.execute(
            """insert into source_roots (collection_id, external_id, kind, name, managed_by, shared_by)
               values (%s, %s, 'folder', %s, %s, %s) on conflict do nothing""",
            (space["id"], ext, name, who.id, who.email))
    mine = await db.fetch_one(
        """insert into items (collection_id, external_id, kind, title, filename, source_root, status)
           values (%s, 'me-file-01', 'doc', 'Member plan', 'Member plan.docx', 'me-root-01', 'active')
           on conflict (collection_id, external_id) do update set status='active' returning id""",
        (space["id"],))
    theirs = await db.fetch_one(
        """insert into items (collection_id, external_id, kind, title, filename, source_root, status)
           values (%s, 'sharer-file-01', 'doc', 'Sharer budget', 'Sharer budget.xlsx', 'sharer-root-01', 'active')
           on conflict (collection_id, external_id) do update set status='active' returning id""",
        (space["id"],))
    await db.execute(
        "insert into chunks (item_id, source_type, source_id, text) values (%s, 'item', %s, 'waiting piece')",
        (mine["id"], mine["id"]))
    from nebula.knowledge import index

    async with db.tx() as conn:
        await index.recount(conn, [str(mine["id"])])
    await db.execute("insert into item_access (item_id, principal) values (%s, %s) on conflict do nothing",
                     (theirs["id"], f"email:{member.email}"))
    return str(mine["id"]), str(theirs["id"])


async def test_each_person_sees_their_own_files_and_what_is_shared_with_them(mcp):
    mine_id, theirs_id = await _setup()
    async with _client() as c:
        settings().dev_user_email = MEMBER
        page = await c.get("/me")
        assert page.status_code == 200 and "me.js" in page.text
        d = (await c.get("/me/api/state")).json()
        names = [r["name"] for r in d["mine"]["roots"]]
        assert "Member's folder" in names and "Sharer's folder" not in names
        mine = {f["id"]: f for f in d["mine"]["files"]}
        assert mine_id in mine and theirs_id not in mine
        assert mine[mine_id]["state"] == "indexing"  # one piece still waiting to be made searchable
        shared = {f["id"]: f for f in d["shared"]["files"]}
        assert theirs_id in shared and mine_id not in shared
        assert shared[theirs_id]["shared_by"]["email"] == SHARER
        assert shared[theirs_id]["shared_via"] == "you"

        settings().dev_user_email = OUTSIDER
        d = (await c.get("/me/api/state")).json()
        ids = {f["id"] for f in d["mine"]["files"] + d["shared"]["files"]}
        assert mine_id not in ids and theirs_id not in ids
        r = await c.post("/me/api/action", json={"action": "reread_item", "item_id": theirs_id})
        assert r.status_code == 400  # can't refresh a file you can't see
        r = await c.post("/me/api/action", json={"action": "reread_item", "item_id": theirs_id},
                         headers={"Origin": "https://evil.example"})
        assert r.status_code == 400


async def test_claude_is_given_the_watch_link(mcp):
    from helpers import call

    res = await call(mcp, MEMBER, "sources", action="list")
    assert res["watch"].endswith("/me")


async def test_tables_count_as_ready_and_own_files_are_not_listed_as_shared(mcp):
    mine_id, theirs_id = await _setup()
    space = await sync.drive_space()
    member = await access.ensure_person(MEMBER, "Member Person")
    sheet = await db.fetch_one(
        """insert into items (collection_id, external_id, kind, title, filename, source_root, status)
           values (%s, 'me-file-02', 'file', 'Quiz results', 'Quiz results.csv', 'me-root-01', 'active')
           on conflict (collection_id, external_id) do update set status='active' returning id""",
        (space["id"],))
    await db.execute(
        """insert into datasets (collection_id, item_id, sheet, table_name, title)
           values (%s, %s, 'Sheet1', 't_me_quiz_sheet1', 'Quiz results') on conflict do nothing""", (space["id"], sheet["id"]))
    for item in (mine_id, sheet["id"]):  # Drive lists the owner among the people a file is shared with
        await db.execute("insert into item_access (item_id, principal) values (%s, %s) on conflict do nothing",
                         (item, f"email:{member.email}"))
    async with _client() as c:
        settings().dev_user_email = MEMBER
        d = (await c.get("/me/api/state")).json()
        quiz = next(f for f in d["mine"]["files"] if f["id"] == str(sheet["id"]))
        assert quiz["state"] == "ready" and quiz["tables"] == 1 and quiz["pieces"] == 0
        shared = {f["title"] for f in d["shared"]["files"]}
        assert "Sharer budget" in shared and "Member plan" not in shared and "Quiz results" not in shared
        web = (await c.get("/web")).text
        assert "Sharer budget" in web and "Quiz results" in web and "gdrive" not in web


async def test_home_page_explains_how_to_connect(mcp):
    async with _client() as c:
        page = (await c.get("/")).text
        assert "Add custom connector" in page and "/mcp</code>" in page and 'href="/me"' in page
        assert "{{" not in page


async def test_documents_lists_what_the_person_can_open(mcp):
    from helpers import call

    mine_id, theirs_id = await _setup()
    member = await access.ensure_person(MEMBER, "Member Person")
    await db.execute("insert into item_access (item_id, principal) values (%s, %s) on conflict do nothing",
                     (mine_id, f"email:{member.email}"))
    titles = lambda res: {d["title"] for d in res["documents"]}  # noqa: E731

    res = await call(mcp, MEMBER, "documents", page_size=200)
    assert {"Member plan", "Sharer budget"} <= titles(res) and res["total"] >= 2
    assert all(d["id"].startswith("item:") and d["state"] for d in res["documents"])
    by_me = await call(mcp, MEMBER, "documents", shared="by_me", page_size=200)
    assert "Member plan" in titles(by_me) and "Sharer budget" not in titles(by_me)
    with_me = await call(mcp, MEMBER, "documents", shared="with_me", page_size=200)
    assert "Sharer budget" in titles(with_me) and "Member plan" not in titles(with_me)
    found = titles(await call(mcp, MEMBER, "documents", title="budget"))
    assert "Sharer budget" in found and "Member plan" not in found and all("budget" in t.lower() for t in found)
    first = await call(mcp, MEMBER, "documents", page_size=1)
    assert len(first["documents"]) == 1 and first["next_page"] == 2 and first["pages"] == first["total"]

    outsider = await call(mcp, OUTSIDER, "documents", page_size=200)
    assert not {"Member plan", "Sharer budget"} & titles(outsider)
