"""The work queue, kept in Postgres.

Jobs are enqueued in the same transaction as the change that needs them, so
nothing is lost between "saved" and "indexed". Workers claim jobs with
`FOR UPDATE SKIP LOCKED`, retry with backoff, and record results that the `job`
tool shows to Claude.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import traceback
import uuid
from typing import Any, Awaitable, Callable

from psycopg import AsyncConnection

from nebula import db

log = logging.getLogger(__name__)

Handler = Callable[[dict, dict], Awaitable[Any]]  # (job row, payload) -> result
_handlers: dict[str, Handler] = {}
# Unique per worker process, so two workers (even on one machine) never mistake each other's jobs.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
# A running job's lease: its worker renews it every few seconds (see worker._heartbeat). A job whose lease
# hasn't been renewed for this long belongs to a worker that died, and goes back to the queue.
LEASE_SECONDS = 90


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn

    return deco


async def enqueue(
    kind: str,
    payload: dict | None = None,
    *,
    conn: AsyncConnection | None = None,
    dedupe_key: str | None = None,
    priority: int = 100,
    person_id: str | None = None,
    delay_seconds: int = 0,
    max_attempts: int = 5,
) -> str | None:
    """Queue a job. With a dedupe_key, an identical queued/running job is reused."""
    row = await db.fetch_one(
        """
        insert into jobs (kind, payload, dedupe_key, priority, person_id, run_after, max_attempts)
        values (%s, %s, %s, %s, %s, now() + make_interval(secs => %s), %s)
        on conflict (dedupe_key) where status in ('queued', 'running') do nothing
        returning id
        """,
        (kind, db.jsonb(payload or {}), dedupe_key, priority, person_id, delay_seconds, max_attempts),
        conn,
    )
    if row:
        return str(row["id"])
    if dedupe_key:
        existing = await db.fetch_one(
            "select id from jobs where dedupe_key = %s and status in ('queued','running')", (dedupe_key,), conn
        )
        return str(existing["id"]) if existing else None
    return None


async def claim() -> dict | None:
    async with db.tx() as conn:
        row = await db.fetch_one(
            """
            update jobs set status = 'running', locked_by = %s, locked_at = now(),
                            attempts = attempts + 1, updated_at = now()
            where id = (
                select id from jobs where status = 'queued' and run_after <= now()
                order by priority, run_after
                for update skip locked limit 1
            )
            returning *
            """,
            (WORKER_ID,),
            conn,
        )
        return row


async def progress(job_id: str, fraction: float | None = None, message: str = "") -> None:
    await db.execute(
        "update jobs set progress = coalesce(%s, progress), message = %s, updated_at = now() where id = %s",
        (fraction, message, job_id),
    )


# The job this process is working on right now, for the worker's heartbeat.
current: dict | None = None


async def run_one() -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    global current
    job = await claim()
    if not job:
        return False
    current = {"id": str(job["id"]), "kind": job["kind"], "since": time.time()}
    try:
        return await _run(job)
    finally:
        current = None


async def _run(job: dict) -> bool:
    fn = _handlers.get(job["kind"])
    if fn is None:
        await db.execute(
            "update jobs set status='failed', error=%s, updated_at=now() where id=%s",
            (f"no handler for {job['kind']}", job["id"]),
        )
        return True
    try:
        result = await fn(job, job["payload"] or {})
        # Only if this worker still holds the job: if its lease lapsed and another worker took it over,
        # that worker's outcome is the one that counts.
        await db.execute(
            """update jobs set status='done', result=%s, progress=1, updated_at=now(), locked_by=null
               where id=%s and locked_by=%s""",
            (db.jsonb(result if result is not None else {}), job["id"], WORKER_ID),
        )
    except Exception as e:
        log.exception("job %s (%s) failed", job["id"], job["kind"])
        final = job["attempts"] >= job["max_attempts"]
        delay = min(3600, 30 * (2 ** (job["attempts"] - 1)))
        await db.execute(
            """update jobs set status = %s, error = %s, locked_by = null, updated_at = now(),
                   run_after = now() + make_interval(secs => %s) where id = %s and locked_by = %s""",
            ("failed" if final else "queued", f"{e}\n{traceback.format_exc()[-2000:]}", delay, job["id"], WORKER_ID),
        )
    return True


async def renew_lease(job_id: str) -> None:
    """Tell the other workers this one is still working on the job."""
    await db.execute("update jobs set updated_at=now() where id=%s and status='running' and locked_by=%s",
                     (job_id, WORKER_ID))


async def requeue_stale() -> int:
    """Jobs whose worker stopped renewing their lease (it died or was replaced) go back to the queue.
    Any number of workers can run this: a job that's still being worked on keeps a fresh lease."""
    rows = await db.fetch_all(
        """update jobs set status='queued', locked_by=null, locked_at=null, run_after=now(), updated_at=now()
           where status='running' and updated_at < now() - make_interval(secs => %s) returning id""",
        (LEASE_SECONDS,))
    return len(rows)


async def get(job_id: str) -> dict | None:
    return await db.fetch_one(
        "select id, kind, status, progress, message, result, error, person_id, created_at, updated_at "
        "from jobs where id = %s",
        (job_id,),
    )


# Rough waits, so Claude can tell someone when their file will be ready ----------------------------

DEFAULT_SECONDS = 20.0


async def _typical_seconds() -> dict[str, float]:
    """Median run time of the last 50 finished jobs of each kind."""
    rows = await db.fetch_all(
        """select kind, percentile_cont(0.5) within group (order by secs) as secs from (
               select kind, extract(epoch from updated_at - locked_at) as secs,
                      row_number() over (partition by kind order by updated_at desc) as n
               from jobs where status = 'done' and locked_at is not null
                 and updated_at > now() - interval '7 days') recent
           where n <= 50 group by kind"""
    )
    return {r["kind"]: max(1.0, float(r["secs"])) for r in rows if r["secs"] is not None}


def describe_wait(seconds: float) -> str:
    if seconds < 45:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes <= 1:
        return "about a minute"
    if minutes < 60:
        return f"about {minutes} minutes"
    return f"about {round(minutes / 60, 1):g} hours"


async def eta(job_id: str | None) -> dict:
    """Where a job stands and a rough wait, from how long recent jobs of each kind took."""
    if not job_id:
        return {}
    j = await db.fetch_one("select id, kind, status, priority, run_after, locked_at from jobs where id = %s", (job_id,))
    if not j:
        return {}
    if j["status"] not in ("queued", "running"):
        return {"job_status": j["status"]}
    typical = await _typical_seconds()
    now = await db.fetch_row("select now() as t")

    def left(kind: str, started: Any) -> float:
        took = (now["t"] - started).total_seconds() if started else 0.0
        return max(5.0, typical.get(kind, DEFAULT_SECONDS) - took)

    if j["status"] == "running":
        secs = left(j["kind"], j["locked_at"])
        return {"job_status": "running", "estimate_seconds": round(secs), "estimate": describe_wait(secs)}
    running = await db.fetch_all("select kind, locked_at from jobs where status = 'running'")
    ahead = await db.fetch_all(
        """select kind from jobs where status = 'queued' and id <> %s and run_after <= now()
             and (priority < %s or (priority = %s and run_after < %s))""",
        (j["id"], j["priority"], j["priority"], j["run_after"]),
    )
    secs = sum(left(r["kind"], r["locked_at"]) for r in running)
    secs += sum(typical.get(r["kind"], DEFAULT_SECONDS) for r in ahead) + typical.get(j["kind"], DEFAULT_SECONDS)
    secs += max(0.0, (j["run_after"] - now["t"]).total_seconds())
    return {"job_status": "queued", "jobs_ahead": len(running) + len(ahead), "estimate_seconds": round(secs),
            "estimate": describe_wait(secs)}
