"""What keeps Nebula working as it grows: job leases (more than one worker), the daily clean-up, and
reading files without holding up the worker or the database."""

from __future__ import annotations

import asyncio
import time

from nebula import db, jobs


async def test_only_jobs_with_a_lapsed_lease_go_back_to_the_queue(mcp):
    await db.execute("update jobs set status='cancelled' where status in ('queued', 'running')")
    alive = await jobs.enqueue("embed", {}, dedupe_key="lease-alive")
    dead = await jobs.enqueue("embed", {}, dedupe_key="lease-dead")
    await db.execute("update jobs set status='running', locked_by='other-worker', locked_at=now(), updated_at=now() "
                     "where id = any(%s::uuid[])", ([alive, dead],))
    await db.execute("update jobs set updated_at = now() - interval '5 minutes' where id=%s", (dead,))
    assert await jobs.requeue_stale() == 1
    assert (await jobs.get(dead))["status"] == "queued"
    assert (await jobs.get(alive))["status"] == "running"  # its worker is still renewing it
    await db.execute("update jobs set status='cancelled' where id = any(%s::uuid[])", ([alive, dead],))


async def test_a_worker_that_lost_its_job_does_not_overwrite_the_result(mcp):
    await db.execute("update jobs set status='cancelled' where status in ('queued', 'running')")

    @jobs.handler("lease-test")
    async def slow(job, payload):
        # Meanwhile the lease lapsed and another worker took the job over.
        await db.execute("update jobs set locked_by='other-worker' where id=%s", (job["id"],))
        return {"by": "first worker"}

    job_id = await jobs.enqueue("lease-test", {})
    assert await jobs.run_one()
    j = await jobs.get(job_id)
    assert j["status"] == "running" and j["result"] is None  # the other worker's outcome is the one that counts
    await db.execute("update jobs set status='cancelled' where id=%s", (job_id,))


async def test_the_daily_clean_up_keeps_only_what_is_recent(mcp):
    from nebula import access, worker

    who = await access.ensure_person("cleanup-person@example.org")
    old, new = "now() - interval '400 days'", "now()"
    for when in (old, new):
        await db.execute(f"insert into jobs (kind, status, updated_at) values ('embed', 'done', {when})")  # noqa: S608
        await db.execute(f"insert into tool_calls (at, person_id, tool, ok) values ({when}, %s, 'search', true)",  # noqa: S608
                         (who.id,))
        await db.execute(f"insert into audit_log (at, action) values ({when}, 'cleanup-test')")  # noqa: S608
        await db.execute(f"insert into index_events (at, event, title) values ({when}, 'read', 'cleanup-test')")  # noqa: S608
    await db.execute("insert into usage_counters (person_id, metric, window_start, used) "
                     "values (%s, 'tool_calls', now() - interval '3 days', 1)", (who.id,))
    out = await worker.cleanup()
    assert out["jobs"] >= 1 and out["tool_calls"] >= 1 and out["audit_log"] >= 1 and out["usage_counters"] >= 1
    assert (await db.fetch_one("select count(*) as n from audit_log where action='cleanup-test'"))["n"] == 1
    assert (await db.fetch_one("select count(*) as n from index_events where title='cleanup-test'"))["n"] == 1
    assert (await db.fetch_one("select count(*) as n from tool_calls where person_id=%s", (who.id,)))["n"] == 1


async def test_reading_a_file_holds_neither_the_worker_nor_the_database(mcp, monkeypatch):
    """While a slow file is being read, the worker's other tasks keep running and no database connection
    is in use."""
    from nebula import sync
    from nebula.connectors.base import Change, FetchedItem
    from nebula.knowledge import items

    col = await sync.drive_space()
    pool = await db.pool()
    seen_in_use: list[int] = []

    def slow_parse(data, filename, mime, title):
        stats = pool.get_stats()
        seen_in_use.append(stats["pool_size"] - stats["pool_available"])
        time.sleep(0.6)
        return None, ""

    monkeypatch.setattr(items, "_parse", slow_parse)

    class Drive:
        async def fetch(self, ch):
            return FetchedItem(external_id="slow-file", title="Slow file", url="", mime_type="text/plain",
                               kind="file", data=b"slow", filename="slow.txt", extra={"rev": "r1"})

    ticks: list[float] = []

    async def ticker():
        for _ in range(10):
            await asyncio.sleep(0.05)
            ticks.append(time.perf_counter())

    await asyncio.gather(sync.process_change(col, Drive(), Change(external_id="slow-file", rev="r1")), ticker())
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.3  # a 0.6 s read on the event loop would have stalled the ticker that long
    assert seen_in_use == [0]  # no transaction was open while the file was read


async def test_a_full_listing_split_over_jobs_deletes_only_unlisted_files(mcp, monkeypatch):
    """The listing's start time travels between jobs, not a list of every file id."""
    from helpers import drain
    from nebula import sync
    from nebula.connectors.base import Change, ChangeBatch, FetchedItem
    from nebula.knowledge import items

    col = await db.fetch_row(
        "insert into collections (slug, name, kind) values ('scale-listing', 'Scale listing', 'gdrive') returning *")
    for ext in ("keep-a", "keep-b", "gone-c"):
        async with db.tx() as conn:
            await items.upsert(conn, str(col["id"]), FetchedItem(external_id=ext, title=ext, url="", mime_type="text/plain",
                                                                  kind="file", data=ext.encode(), filename=f"{ext}.txt"))
    await db.execute("update items set seen_at = now() - interval '1 day' where collection_id=%s", (col["id"],))

    class Drive:
        def __init__(self, config):
            pass

        async def list_changes(self, cursor):
            if cursor is None:
                return ChangeBatch([Change(external_id="keep-a", rev="")], next_cursor="page-2", has_more=True)
            return ChangeBatch([Change(external_id="keep-b", rev="")], next_cursor="done", has_more=False)

        async def fetch(self, ch):
            return FetchedItem(external_id=ch.external_id, title=ch.external_id, url="", mime_type="text/plain",
                               kind="file", data=ch.external_id.encode(), filename=f"{ch.external_id}.txt")

        async def close(self):
            return None

    monkeypatch.setattr(sync, "get_connector", lambda kind, config: Drive(config))
    monkeypatch.setattr(sync, "MAX_ITEMS_PER_JOB", 1)
    await db.execute("update jobs set status='cancelled' where status in ('queued', 'running')")
    await jobs.enqueue("sync", {"collection_id": str(col["id"]), "full": True})
    assert await jobs.run_one()
    follow_on = await db.fetch_row(
        "select payload from jobs where kind='sync' and status='queued' and payload->>'collection_id'=%s", (str(col["id"]),))
    assert "seen" not in follow_on["payload"] and follow_on["payload"]["listed_since"]
    await drain()
    status = {r["external_id"]: r["status"] for r in await db.fetch_all(
        "select external_id, status from items where collection_id=%s", (col["id"],))}
    assert status == {"keep-a": "active", "keep-b": "active", "gone-c": "deleted"}


async def test_polling_a_page_writes_nothing(mcp):
    import httpx

    from helpers import ADMIN
    from nebula import admin, me, server, webui  # noqa: F401  (register the routes)
    from nebula.config import settings

    settings().dev_user_email = ADMIN
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        assert (await c.get("/me/api/state")).status_code == 200  # the first visit may create the person
        before = await db.fetch_row("select last_seen_at from people where email=%s", (ADMIN,))
        for view in ("overview", "indexing", "people"):
            assert (await c.get(f"/admin/api/state?view={view}")).status_code == 200
        assert (await c.get("/me/api/state")).status_code == 200
    after = await db.fetch_row("select last_seen_at from people where email=%s", (ADMIN,))
    assert after["last_seen_at"] == before["last_seen_at"]


async def test_connection_pools_follow_the_settings(mcp):
    from nebula import app
    from nebula.config import settings

    pool = await db.pool()
    assert pool.max_size == settings().db_pool_size
    store = app._signin_store()
    signin = await store._create_pool()
    try:
        assert signin.get_max_size() == settings().db_signin_pool_size == 2
    finally:
        await signin.close()
    assert await db.migrate() == []  # nothing pending, and the one-transaction lock works
