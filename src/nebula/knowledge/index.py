"""Writing to the search index: chunks in, embeddings filled in by the worker."""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable, Iterable

from psycopg import AsyncConnection

from nebula import db, jobs, models
from nebula.ingest.types import Chunk

# A backlog of text to embed is worked through in slices of about this many seconds, queued
# below discovery (60) and Drive checks (80), so they never wait more than one slice.
EMBED_SLICE_SECONDS = 20.0
BACKGROUND_EMBED_PRIORITY = 90
# A file someone asked for gets this long inline; the rest continues in slices queued ahead of the
# general backlog (90) but still behind discovery and Drive checks.
REQUESTED_FILE_SECONDS = 30.0
REQUESTED_EMBED_PRIORITY = 85

log = logging.getLogger(__name__)


async def replace_chunks(
    conn: AsyncConnection,
    source_type: str,
    source_id: str,
    item_id: str,
    chunks: Iterable[Chunk],
) -> int:
    """Swap a file's (or one of its tables') chunks for new ones (embeddings are added later by the worker).

    A new chunk with the same heading and text as an old one keeps the old embedding, so a small
    edit to a long document only re-embeds the pieces that actually changed.
    """
    old = await db.fetch_all("select id from chunks where source_type = %s and source_id = %s",
                             (source_type, source_id), conn)
    old_ids = [r["id"] for r in old]
    rows = [
        (source_type, source_id, item_id, i, c.kind, " › ".join(c.heading_path)[:500], c.text, db.jsonb(c.locator))
        for i, c in enumerate(chunks)
        if c.text.strip()
    ]
    if rows:
        async with conn.cursor() as cur:
            await cur.executemany(
                """insert into chunks (source_type, source_id, item_id, seq, kind, heading, text, locator)
                   values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                rows,
            )
    if old_ids:
        if rows:
            await conn.execute(
                """update chunks n set embedding = o.embedding, embed_model = o.embed_model
                   from chunks o
                   where n.source_type = %(t)s and n.source_id = %(s)s and n.embedding is null
                     and n.id <> all(%(old)s) and o.id = any(%(old)s) and o.embedding is not null
                     and o.embed_model = %(model)s and o.heading = n.heading and o.text = n.text""",
                {"t": source_type, "s": source_id, "old": old_ids, "model": models.embed_model_name()},
            )
        await conn.execute("delete from chunks where id = any(%s)", (old_ids,))
    await recount(conn, [item_id])
    pending = await db.fetch_one(
        "select exists (select 1 from chunks where source_type = %s and source_id = %s and embedding is null) as p",
        (source_type, source_id), conn)
    if pending and pending["p"]:
        await jobs.enqueue("embed", {}, conn=conn, dedupe_key="embed", priority=BACKGROUND_EMBED_PRIORITY)
    return len(rows)


async def drop_chunks(conn: AsyncConnection, item_id: str) -> None:
    """Remove a file's passages and its tables' cards from search."""
    await conn.execute("delete from chunks where item_id = %s", (item_id,))
    await recount(conn, [item_id])


async def recount(conn: AsyncConnection, item_ids: list[str]) -> None:
    """Store each file's number of search pieces and how many still wait to be embedded, so the pages
    that list many files read two columns instead of counting chunks on every refresh."""
    await conn.execute(
        """update items i set pieces = c.n, pieces_waiting = c.w
           from (select x.id,
                        (select count(*) from chunks k where k.item_id = x.id) as n,
                        (select count(*) from chunks k where k.item_id = x.id and k.embedding is null) as w
                 from unnest(%s::uuid[]) as x(id)) c
           where i.id = c.id""", (item_ids,))


async def embed_pending(batch: int = 64, max_batches: int = 50, *, source: tuple[str, str] | None = None,
                        budget_seconds: float | None = None,
                        on_progress: Callable[[int], Awaitable[None]] | None = None) -> int:
    """Fill in missing embeddings (all of them, or one source's). Returns how many chunks were embedded.

    With `budget_seconds`, stop after the batch that crosses the budget, so a long backlog is worked
    through in slices and other jobs (Drive checks, files someone asked for) get a turn in between."""
    done = 0
    started = time.monotonic()
    model = models.embed_model_name()
    only = "and source_type = %s and source_id = %s" if source else ""
    touched: set[str] = set()
    for _ in range(max_batches):
        async with db.tx() as conn:
            rows = await db.fetch_all(
                f"""select id, heading, text, item_id from chunks where embedding is null {only}
                   order by id limit %s for update skip locked""",
                (*(source or ()), batch),
                conn,
            )
            if not rows:
                break
            touched.update(str(r["item_id"]) for r in rows)
            texts = [(r["heading"] + "\n" + r["text"]).strip()[:8000] for r in rows]
            vectors = await models.embed(texts, "document")
            async with conn.cursor() as cur:
                await cur.executemany(
                    "update chunks set embedding = %s::halfvec, embed_model = %s where id = %s",
                    [(v, model, r["id"]) for v, r in zip(vectors, rows)],
                )
            await recount(conn, sorted({str(r["item_id"]) for r in rows}))
            done += len(rows)
        if on_progress:
            await on_progress(done)
        if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
            break
    if touched:
        await _note_searchable(touched)
    return done


async def _note_searchable(item_ids: set[str]) -> None:
    """Record files that just became fully searchable, for the admin page's indexing feed."""
    from nebula import activity

    rows = await db.fetch_all(
        """select i.id, i.title, i.collection_id, count(c.id) as pieces
           from items i join chunks c on c.item_id=i.id
           where i.id = any(%s::uuid[])
           group by i.id having count(*) filter (where c.embedding is null) = 0""",
        (list(item_ids),),
    )
    for r in rows:
        await activity.index_event("searchable", item_id=str(r["id"]), title=r["title"],
                                   collection_id=str(r["collection_id"]), detail=f"{r['pieces']} pieces")


async def _waiting(source: tuple[str, str] | None = None) -> int:
    if source:
        r = await db.fetch_one(
            "select count(*) as n from chunks where embedding is null and source_type=%s and source_id=%s", source)
    else:
        r = await db.fetch_one("select count(*) as n from chunks where embedding is null")
    return r["n"] if r else 0


async def embed_requested(source: tuple[str, str]) -> int:
    """Make one source searchable now, for up to REQUESTED_FILE_SECONDS. Whatever is left continues in
    slices (see the embed job). Returns how many pieces are still waiting."""
    await embed_pending(batch=models.EMBED_BATCH, max_batches=100_000, source=source,
                        budget_seconds=REQUESTED_FILE_SECONDS)
    left = await _waiting(source)
    if left:
        await jobs.enqueue("embed", {"source_type": source[0], "source_id": source[1]},
                           dedupe_key=f"embed-src:{source[0]}:{source[1]}", priority=REQUESTED_EMBED_PRIORITY)
    return left


@jobs.handler("embed")
async def _embed_job(job: dict, payload: dict) -> dict:
    """One slice of an embedding backlog: one requested source's, or everyone's. It re-queues itself
    below discovery and Drive checks, so a big backlog never holds them up for more than one slice."""
    source = (payload["source_type"], payload["source_id"]) if payload.get("source_id") else None
    total = await _waiting(source)
    if not total:
        return {"embedded": 0, "remaining": 0}
    job_id = str(job["id"])
    await jobs.progress(job_id, 0.0, f"Making {total:,} pieces of text searchable")

    async def report(done: int) -> None:
        await jobs.progress(job_id, min(1.0, done / total), f"Made {done:,} of {total:,} pieces searchable")

    n = await embed_pending(batch=models.EMBED_BATCH, max_batches=100_000, source=source,
                            budget_seconds=EMBED_SLICE_SECONDS, on_progress=report)
    left = await _waiting(source)
    if left:
        if source:
            await jobs.enqueue("embed", dict(payload), dedupe_key=f"embed-src-next:{source[0]}:{source[1]}",
                               priority=REQUESTED_EMBED_PRIORITY)
        else:
            await jobs.enqueue("embed", {}, dedupe_key="embed-next", priority=BACKGROUND_EMBED_PRIORITY)
    return {"embedded": n, "remaining": left}
