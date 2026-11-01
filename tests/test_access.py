"""Who can open what: Drive's sharing, the RL domain, Google Groups, and the limits on what comes back."""

from __future__ import annotations

from helpers import ADMIN, MEMBER, OUTSIDER, call, drive_file
from nebula import access
from nebula.config import settings


async def test_only_people_and_the_rl_domain_are_mirrored(mcp):
    item_id = await drive_file("Access mix", "Shared in every way Drive allows.",
                               emails=["Friend@Gmail.com"], domains=["example.org", "other.org"],
                               groups=["ap-team@example.org"])
    from nebula import db

    rows = await db.fetch_all("select principal from item_access where item_id=%s order by principal", (item_id,))
    assert [r["principal"] for r in rows] == ["domain:example.org", "email:friend@gmail.com"]
    assert await access.can_open(await access.ensure_person(MEMBER), item_id)
    assert await access.can_open(await access.ensure_person("friend@gmail.com"), item_id)
    assert not await access.can_open(await access.ensure_person("someone@other.org"), item_id)


async def test_google_group_shares_do_not_reach_members_yet(mcp):
    item_id = await drive_file("Group budget", "Budget for the AP team.", groups=["gap-team@example.org"])
    assert "error" in await call(mcp, MEMBER, "open", id=f"item:{item_id}")
    r = await call(mcp, ADMIN, "sources", action="status", id=f"item:{item_id}")
    assert "gap-team@example.org" in r["sharing_note"] and "can't see who's in a group" in r["sharing_note"]


async def test_unreadable_sharing_is_for_admins_only(mcp):
    item_id = await drive_file("Locked note", "Nobody but admins.", unknown=True)
    assert "error" in await call(mcp, MEMBER, "open", id=f"item:{item_id}")
    assert (await call(mcp, ADMIN, "open", id=f"item:{item_id}"))["title"] == "Locked note"


async def test_documents_lists_only_what_drive_shares(mcp):
    await drive_file("Docs list mine", "For the member.", emails=[MEMBER])
    await drive_file("Docs list theirs", "For someone else.", emails=[OUTSIDER])
    mine = {d["title"] for d in (await call(mcp, MEMBER, "documents", title="Docs list"))["documents"]}
    assert mine == {"Docs list mine"}
    everything = {d["title"] for d in (await call(mcp, ADMIN, "documents", title="Docs list"))["documents"]}
    assert everything == {"Docs list mine", "Docs list theirs"}


def test_long_results_are_cut_with_a_note(monkeypatch):
    from fastmcp.tools.base import ToolResult
    from mcp_types import TextContent

    from nebula import server

    monkeypatch.setattr(settings(), "max_result_chars", 100)
    short = ToolResult(content=[TextContent(type="text", text="ok")])
    assert server._bounded(short) is short
    cut = server._bounded(ToolResult(content=[TextContent(type="text", text="x" * 500)]))
    assert cut.content[0].text.startswith("x" * 100) and "cut this result at 100" in cut.content[0].text


async def test_query_reaches_only_the_persons_own_tables(mcp):
    """The probes from the 1 Nov review: each one read data it shouldn't through Postgres."""
    from nebula import db

    theirs = await drive_file("Probe salaries", data=b"name,salary\nAsha,90000\nRavi,85000\n" +
                              b"".join(f"P{i},{i}\n".encode() for i in range(30)),
                              filename="salaries.csv", kind="sheet", mime="text/csv", emails=[OUTSIDER])
    mine = await drive_file("Probe visits", data=b"district,visits\nGuntur,12\nKrishna,9\n" +
                            b"".join(f"D{i},{i}\n".encode() for i in range(30)),
                            filename="visits.csv", kind="sheet", mime="text/csv", emails=[MEMBER])
    their_table = (await db.fetch_one("select table_name from datasets where item_id=%s", (theirs,)))["table_name"]
    my_table = (await db.fetch_one("select table_name from datasets where item_id=%s", (mine,)))["table_name"]

    ok = await call(mcp, MEMBER, "query", sql=f"select sum(visits) as total from data.{my_table}")
    assert ok["rows"] == [[sum(range(30)) + 21]]
    probes = [
        f"select * from data.{their_table}",
        f"select query_to_xml('select * from data.{their_table}', true, false, '')",
        f"select set_config('role', session_user, true), * from data.{my_table}",
        f"select * from data.{my_table}, people",
        f"select * from data.{my_table} where 1 = (select count(*) from items)",
        f"select * from data.{my_table}, read_text('/etc/hosts')",
        f"select *, getenv('HOME') from data.{my_table}",
        f"select * from data.{my_table}; select * from people",
        "select pg_read_file('/etc/hostname')",
    ]
    for q in probes:
        r = await call(mcp, MEMBER, "query", sql=q)
        assert "error" in r, q
        assert "Asha" not in r["error"] and "admin@" not in r["error"], q
