"""The background worker: runs queued jobs and the periodic housekeeping."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal

from nebula import activity, db, jobs, models
from nebula.config import settings

# Importing these registers their job handlers.
from nebula import sources, sync  # noqa: F401
from nebula.knowledge import index  # noqa: F401

log = logging.getLogger(__name__)

# How long finished jobs, request counts and activity are kept (activity length is a setting).
CLEANUP = [
    ("jobs", "delete from jobs where status in ('done', 'failed', 'cancelled') "
             "and updated_at < now() - make_interval(days => %(jobs_days)s)"),
    ("usage_counters", "delete from usage_counters where window_start < now() - interval '2 days'"),
    ("tool_calls", "delete from tool_calls where at < now() - make_interval(days => %(activity_days)s)"),
    ("audit_log", "delete from audit_log where at < now() - make_interval(days => %(activity_days)s)"),
    ("index_events", "delete from index_events where at < now() - make_interval(days => %(activity_days)s)"),
    ("heartbeats", "delete from app_settings where key like 'heartbeat:%%' and updated_at < now() - interval '1 day'"),
]


@jobs.handler("cleanup")
async def cleanup(job: dict | None = None, payload: dict | None = None) -> dict:
    """Once a day: delete what nobody needs any more, so these tables (and the queries that read them)
    don't grow forever."""
    s = settings()
    params = {"jobs_days": s.jobs_keep_days, "activity_days": s.activity_keep_days}
    out = {}
    for name, sql in CLEANUP:
        rows = await db.fetch_all(sql + " returning 1 as x", params)
        out[name] = len(rows)
    return out


async def _housekeeping(last: dict[str, dt.datetime]) -> None:
    now = dt.datetime.now(dt.UTC)

    def due(name: str, every: dt.timedelta) -> bool:
        if now - last.get(name, dt.datetime.min.replace(tzinfo=dt.UTC)) >= every:
            last[name] = now
            return True
        return False

    refresh = await activity.refresh_settings()  # the admin page can change these while Nebula runs
    if due("syncs", dt.timedelta(seconds=10)):  # each collection is only queued when its own interval is up
        await sync.schedule_due_syncs()
    if due("discover", dt.timedelta(seconds=refresh["discover_seconds"])):
        # Anything newly shared with Nebula in Drive becomes a source on its own; one API call when nothing changed.
        from nebula.connectors.gdrive import service_account_email

        if service_account_email():
            await jobs.enqueue("drive_discover", {}, dedupe_key="drive_discover", priority=60)
    if due("embed", dt.timedelta(seconds=refresh["embed_seconds"])):
        await jobs.enqueue("embed", {}, dedupe_key="embed", priority=index.BACKGROUND_EMBED_PRIORITY)
    if due("stale", dt.timedelta(seconds=30)):
        await jobs.requeue_stale()
    if due("cleanup", dt.timedelta(hours=24)):
        await jobs.enqueue("cleanup", {}, dedupe_key="cleanup", priority=200)


async def _heartbeat(stop: asyncio.Event) -> None:
    """Every 10 seconds, also mid-job: report that this worker is alive and what it's doing, and renew
    the lease on its current job so no other worker takes it over."""
    while not stop.is_set():
        await activity.heartbeat(f"worker:{jobs.WORKER_ID}", job=jobs.current)
        if jobs.current:
            try:
                await jobs.renew_lease(jobs.current["id"])
            except Exception:
                log.warning("could not renew a job lease", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            pass


async def run_forever() -> None:
    await db.migrate()
    await db.pool()
    await asyncio.to_thread(models.warm)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    last: dict[str, dt.datetime] = {}
    stale = await jobs.requeue_stale()
    if stale:
        log.info("requeued %s job(s) whose worker stopped", stale)
    beat = asyncio.create_task(_heartbeat(stop))
    log.info("worker started")
    while not stop.is_set():
        try:
            await _housekeeping(last)
            worked = await jobs.run_one()
        except Exception:
            log.exception("worker loop error")
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings().worker_poll_seconds)
            except TimeoutError:
                pass
    beat.cancel()
    await db.close()
    log.info("worker stopped")
