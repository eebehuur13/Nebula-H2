"""The admin page: what gets recorded, who can see it, and what its buttons do."""

from __future__ import annotations

import httpx
from helpers import ADMIN, MEMBER, call, drain, drive_file

from nebula import activity, db, jobs
from nebula.config import settings


def _client():
    from nebula import admin, server, webui  # noqa: F401  (register the routes)

    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_tool_calls_are_recorded_with_what_was_asked(mcp):
    await drive_file("Admin page fact", "The sky is blue.", emails=[MEMBER])
    await drain()
    res = await call(mcp, MEMBER, "search", query="what colour is the sky")
    assert "results" in res
    row = await db.fetch_one(
        """select t.tool, t.summary, t.ok, t.result_count, t.ms, p.email from tool_calls t join people p on p.id=t.person_id
           where t.tool='search' order by t.id desc limit 1""")
    assert row is not None
    assert row["email"] == MEMBER
    assert row["summary"] == "what colour is the sky"
    assert row["ok"] is True
    assert row["result_count"] == len(res["results"])
    assert row["ms"] >= 0


async def test_failed_calls_are_recorded_with_the_error(mcp):
    res = await call(mcp, MEMBER, "open", id="item:00000000-0000-0000-0000-000000000000")
    assert "error" in res
    row = await db.fetch_one("select ok, error, target from tool_calls where tool='open' order by id desc limit 1")
    assert row is not None and row["ok"] is False
    assert row["error"]
    assert row["target"] == "item:00000000-0000-0000-0000-000000000000"


async def test_long_arguments_are_cut_down(mcp):
    await call(mcp, MEMBER, "search", query="word " * 2000)
    row = await db.fetch_one("select args from tool_calls where tool='search' order by id desc limit 1")
    assert row is not None
    assert row["args"]["query"].endswith("(10,000 characters)")  # cut down, not stored whole
    assert len(row["args"]["query"]) < 400


async def test_a_claude_connection_is_recorded_once_per_app(mcp):
    settings().dev_user_email = MEMBER
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-ai", "version": "0.1.0"}}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    from nebula import server

    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            assert (await c.post("/mcp", json=init, headers=headers)).status_code == 200
            assert (await c.post("/mcp", json=init, headers=headers)).status_code == 200
    rows = await db.fetch_all(
        "select m.client, m.connects from mcp_clients m join people p on p.id=m.person_id where p.email=%s", (MEMBER,))
    assert [(r["client"], r["connects"]) for r in rows] == [("claude-ai 0.1.0", 2)]
    await call(mcp, MEMBER, "sources", action="list")  # ordinary requests don't add rows per user agent
    assert len(await db.fetch_all(
        "select 1 from mcp_clients m join people p on p.id=m.person_id where p.email=%s", (MEMBER,))) == 1


async def test_only_admins_can_open_the_page_or_its_data(mcp):
    async with _client() as c:
        settings().dev_user_email = MEMBER
        assert (await c.get("/admin/api/state?view=overview")).status_code == 403
        page = await c.get("/admin")
        assert page.status_code == 200 and "Admins only" in page.text
        settings().dev_user_email = ADMIN
        page = await c.get("/admin")
        assert page.status_code == 200 and "admin.js" in page.text
        assert (await c.get("/admin/assets/admin.js")).status_code == 200
        assert (await c.get("/admin/assets/../config.py")).status_code == 404


async def test_every_view_loads_for_every_range(mcp):
    settings().dev_user_email = ADMIN
    async with _client() as c:
        for view in ("overview", "people", "activity", "indexing", "sources", "jobs"):
            for rng in ("1h", "today", "24h", "7d", "all"):
                r = await c.get(f"/admin/api/state?view={view}&range={rng}")
                assert r.status_code == 200, (view, rng, r.text)
                data = r.json()
                assert view in data and "pulse" in data
        ov = (await c.get("/admin/api/state?view=overview&range=24h")).json()["overview"]
        assert ov["tiles"]["requests"] >= 2
        assert sum(b["total"] for b in ov["series"]) == ov["tiles"]["requests"]
        people = (await c.get("/admin/api/state?view=people&range=24h")).json()["people"]["rows"]
        member = next(p for p in people if p["email"] == MEMBER)
        assert member["searches"] >= 1 and member["clients"]
        acts = (await c.get("/admin/api/state?view=activity&range=24h")).json()["activity"]["rows"]
        assert any(a["label"] == "Searched" and a["text"] == "what colour is the sky" for a in acts)


async def test_buttons_queue_the_right_work(mcp):
    settings().dev_user_email = ADMIN
    async with _client() as c:
        r = await c.post("/admin/api/action", json={"action": "embed_now"})
        assert r.status_code == 200, r.text
        job = await jobs.get(r.json()["job"])
        assert job and job["kind"] == "embed"
        r = await c.post("/admin/api/action", json={"action": "nonsense"})
        assert r.status_code == 400
        r = await c.post("/admin/api/action", json={"action": "embed_now"}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 400
        settings().dev_user_email = MEMBER
        r = await c.post("/admin/api/action", json={"action": "embed_now"})
        assert r.status_code == 403
    await drain()
    audit = await db.fetch_one("select action from audit_log where action like 'admin_page:%%' order by id desc limit 1")
    assert audit and audit["action"] == "admin_page:embed_now"


async def test_refresh_timings_can_be_changed_within_limits(mcp):
    settings().dev_user_email = ADMIN
    async with _client() as c:
        r = await c.post("/admin/api/action", json={"action": "set_refresh", "values": {"discover_seconds": 30}})
        assert r.status_code == 200, r.text
        assert (await activity.refresh_settings())["discover_seconds"] == 30
        r = await c.post("/admin/api/action", json={"action": "set_refresh", "values": {"discover_seconds": 1}})
        assert r.status_code == 400
        r = await c.post("/admin/api/action", json={"action": "set_refresh", "values": {"nope": 30}})
        assert r.status_code == 400
        await c.post("/admin/api/action", json={"action": "set_refresh", "values": dict(activity.REFRESH_DEFAULTS)})


async def test_failed_jobs_can_be_retried_and_waiting_ones_cancelled(mcp):
    settings().dev_user_email = ADMIN
    failed = await jobs.enqueue("embed", {}, dedupe_key="admin-test-failed")
    await db.execute("update jobs set status='failed', error='boom' where id=%s", (failed,))
    waiting = await jobs.enqueue("embed", {}, dedupe_key="admin-test-waiting", delay_seconds=3600)
    async with _client() as c:
        assert (await c.post("/admin/api/action", json={"action": "retry_job", "job_id": failed})).status_code == 200
        assert (await jobs.get(failed))["status"] == "queued"
        assert (await c.post("/admin/api/action", json={"action": "cancel_job", "job_id": waiting})).status_code == 200
        assert (await jobs.get(waiting))["status"] == "cancelled"
        assert (await c.post("/admin/api/action", json={"action": "retry_job", "job_id": waiting})).status_code == 400
    await db.execute("update jobs set status='cancelled' where id=%s", (failed,))


async def test_embedding_backlog_runs_in_slices_below_drive_checks(mcp):
    from nebula.knowledge import index

    await drive_file("Backlog page", "\n\n".join(f"Para {i}. " + "words " * 300 for i in range(12)), emails=[ADMIN])
    await db.execute("update chunks set embedding = null")
    old = index.EMBED_SLICE_SECONDS
    index.EMBED_SLICE_SECONDS = 0.0  # stop after the first batch
    try:
        await db.execute("update jobs set status='cancelled' where status in ('queued','running')")
        job = await jobs.enqueue("embed", {}, dedupe_key="embed")
        assert await jobs.run_one()
        done = await jobs.get(job)
        assert done["status"] == "done" and done["result"]["remaining"] > 0
        assert "pieces searchable" in done["message"]
        nxt = await db.fetch_one("select priority from jobs where kind='embed' and status='queued'")
        assert nxt and nxt["priority"] == index.BACKGROUND_EMBED_PRIORITY > 80  # below Drive checks (80)
    finally:
        index.EMBED_SLICE_SECONDS = old
    await drain()
    left = await db.fetch_one("select count(*) as n from chunks where embedding is null")
    assert left["n"] == 0  # the slices together finish the backlog


async def test_heartbeat_says_what_the_worker_is_doing(mcp):
    await activity.heartbeat("worker", job={"id": "x", "kind": "embed", "since": 1_800_000_000.0})
    settings().dev_user_email = ADMIN
    async with _client() as c:
        pulse = (await c.get("/admin/api/state?view=sources")).json()["pulse"]
    assert pulse["worker_job"]["label"] == "Make text searchable"


async def test_requests_do_not_add_a_connection_per_user_agent(mcp):
    before = await db.fetch_one("select count(*) as n from mcp_clients")
    await activity.touch_clients("00000000-0000-0000-0000-000000000000")
    after = await db.fetch_one("select count(*) as n from mcp_clients")
    assert before["n"] == after["n"]


async def test_a_search_records_what_it_found_and_every_step(mcp):
    await drive_file("Trace fact", "Kamalpur attendance rose.", emails=[MEMBER])
    await drain()
    res = await call(mcp, MEMBER, "search", query="Kamalpur attendance")
    row = await db.fetch_one("select id, results, trace from tool_calls where tool='search' order by id desc limit 1")
    assert [r["id"] for r in row["results"]] == [r["id"] for r in res["results"]]
    t = row["trace"]
    assert [s["step"] for s in t["steps"]] == ["scope", "embed", "meaning", "words", "rerank", "assemble"]
    assert t["query"] == "Kamalpur attendance" and "kamalpur" in t["terms"]
    assert t["pool"] and len(t["reranked"]) == len(t["pool"])
    assert any(title == "Trace fact" for title in t["titles"].values())

    settings().dev_user_email = ADMIN
    async with _client() as c:
        acts = (await c.get("/admin/api/state?view=activity&range=today")).json()["activity"]["rows"]
        mine = next(a for a in acts if a.get("trace_id") == row["id"])
        assert mine["hits"][0]["id"] == res["results"][0]["id"]
        d = (await c.get(f"/admin/api/search/{row['id']}")).json()
        assert d["question"] == "Kamalpur attendance" and d["trace"]["steps"]
        settings().dev_user_email = MEMBER
        assert (await c.get(f"/admin/api/search/{row['id']}")).status_code == 403
