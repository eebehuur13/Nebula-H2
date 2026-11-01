"""Keeping Nebula in step with Drive, in the background: finding what's newly shared with Nebula,
syncing changes, refreshing one file on request, and downloading an original when Claude needs it.

A sync job reads changes since the collection's cursor and brings each changed file up to date.
Each change carries the file's revision marker and a fingerprint of its sharing, so a file whose
content didn't change isn't downloaded again, and its sharing is only re-read when the fingerprint
changed or a folder above it was re-shared.

A full listing (the first run, then as often as the admin page's refresh settings say) also marks
files that no longer exist as deleted. Once a day the full listing re-reads every file's sharing
regardless of fingerprints, which catches sharing inherited from folders above a root.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from psycopg import AsyncConnection

from nebula import activity, db, jobs
from nebula.config import settings
from nebula.connectors import gdrive, get_connector
from nebula.connectors.base import Change, Connector
from nebula.knowledge import index, items

log = logging.getLogger(__name__)

ACL_SWEEP_EVERY = dt.timedelta(hours=24)
MAX_ITEMS_PER_JOB = 200
DRIVE_SLUG = "drive"
VIEWER_NOTE = ("Shared with Nebula as a Viewer, so Nebula can read the files but not who they're shared with: "
               "they stay hidden from everyone but admins. In Drive, change Nebula's access to Editor (it still "
               "only reads), and the files become visible to the right people within minutes.")
LOST_NOTE = ("Nebula can no longer open this in Drive: it was unshared from Nebula, moved to the trash or "
             "deleted. Share it with Nebula again to bring it back.")


async def active_roots(col: dict) -> list[dict]:
    return await db.fetch_all(
        "select id, external_id, kind, name, managed_by from source_roots where collection_id=%s and status='active' "
        "order by created_at", (col["id"],))


def connector_for(col: dict, roots: list[dict], rewalk: list[str] | None = None) -> Connector:
    config = dict(col.get("source_config") or {})
    config["roots"] = [{"id": r["external_id"], "kind": r["kind"]} for r in roots]
    if rewalk:
        config["rewalk"] = rewalk
    return get_connector(col["kind"], config)


async def process_change(col: dict, connector: Connector, ch: Change, *, full: bool = False, sweep: bool = False,
                         refetch: bool = False) -> tuple[str, str | None]:
    """Bring one file up to date. Returns (changed | unchanged | deleted | skipped, item id)."""
    cid = str(col["id"])
    if ch.deleted:
        async with db.tx() as conn:
            gone = await items.mark_deleted(conn, cid, ch.external_id)
        return ("deleted" if gone else "unchanged"), gone
    existing = await db.fetch_one(
        """select id, status, source_rev, acl_fingerprint, content_hash from items
           where collection_id=%s and external_id=%s""",
        (cid, ch.external_id))
    if existing and existing["status"] != "deleted" and ch.rev and existing["source_rev"] == ch.rev and not refetch:
        # Same content as stored: keep names and paths current, and re-read sharing only if it may have changed.
        item_id = str(existing["id"])
        recheck = (sweep or ch.recheck_acl or (ch.acl is None and full)
                   or (ch.acl is not None and ch.acl != existing["acl_fingerprint"]))
        audience = await connector.file_audience(ch.external_id) if recheck else None  # type: ignore[attr-defined]
        async with db.tx() as conn:
            await conn.execute(
                """update items set title=coalesce(nullif(%s, ''), title), url=coalesce(nullif(%s, ''), url), path=%s,
                          source_root=coalesce(%s, source_root), source_modified_at=coalesce(%s, source_modified_at)
                   where id=%s""",
                (ch.title, ch.url, ch.path, ch.root, ch.modified_at, item_id))
            if audience is not None:
                await items.apply_source_audience(conn, item_id, audience)
                await conn.execute("update items set acl_fingerprint=%s where id=%s", (ch.acl, item_id))
        return "unchanged", item_id
    fetched = await connector.fetch(ch)
    if fetched is None:
        return "skipped", str(existing["id"]) if existing else None
    # Read the file before opening the transaction, so it's held only for the writes.
    known = existing["content_hash"] if existing and existing["status"] != "deleted" else None
    prepared = await items.prepare(fetched, known)
    async with db.tx() as conn:
        res = await items.upsert(conn, cid, fetched, prepared=prepared)
        await conn.execute(
            "update items set source_rev=%s, acl_fingerprint=%s, source_root=coalesce(%s, source_root) where id=%s",
            (fetched.extra.get("rev", ch.rev) or None, fetched.extra.get("acl", ch.acl), ch.root, res["id"]))
        if "audience" in fetched.extra:
            await items.apply_source_audience(conn, res["id"], fetched.extra["audience"])
    return ("changed" if res["changed"] else "unchanged"), res["id"]


async def roots_lost(col: dict, root_ids: list[str]) -> None:
    """Roots Drive no longer lets us read: mark them and take their files out of Nebula."""
    cid = str(col["id"])
    async with db.tx() as conn:
        await conn.execute(
            """update source_roots set status='lost', status_note=%s, updated_at=now()
               where collection_id=%s and external_id = any(%s) and status='active'""",
            (LOST_NOTE, cid, root_ids))
        rows = await db.fetch_all(
            "select external_id from items where collection_id=%s and source_root = any(%s) and status <> 'deleted'",
            (cid, root_ids), conn)
        for r in rows:
            await items.mark_deleted(conn, cid, r["external_id"])


async def note_outcome(col: dict, ch: Change, outcome: str, item_id: str | None, started: float,
                       job_id: str | None) -> None:
    """Record what happened to one file, for the admin page's indexing feed."""
    if outcome not in ("changed", "deleted"):
        return
    ms = int((time.perf_counter() - started) * 1000)
    if outcome == "deleted":
        await activity.index_event("removed", item_id=item_id, title=ch.title or ch.external_id,
                                   collection_id=str(col["id"]), ms=ms, job_id=job_id)
        return
    it = await db.fetch_one(
        """select i.title, i.version, i.status, i.status_note,
                  (select count(*) from chunks c where c.source_type='item' and c.source_id=i.id) as pieces
           from items i where i.id=%s""", (item_id,)) if item_id else None
    if it and it["status"] == "error":
        await activity.index_event("failed", item_id=item_id, title=it["title"], collection_id=str(col["id"]),
                                   detail=it["status_note"], ms=ms, job_id=job_id)
    elif it:
        await activity.index_event("read", item_id=item_id, title=it["title"], collection_id=str(col["id"]),
                                   detail=f"version {it['version']}, {it['pieces']} pieces", ms=ms, job_id=job_id)


@jobs.handler("sync")
async def sync_collection(job: dict, payload: dict) -> dict:
    col = await db.fetch_one("select * from collections where id = %s", (payload["collection_id"],))
    if not col or col["status"] != "active":
        return {"skipped": True}
    now = dt.datetime.now(dt.UTC)
    every = dt.timedelta(minutes=(await activity.refresh_settings())["full_sync_minutes"])
    full = bool(payload.get("full")) or col["sync_cursor"] is None or (
        col["last_full_sync_at"] is None or now - col["last_full_sync_at"] > every
    )
    sweep = bool(payload["sweep"]) if "sweep" in payload else (
        full and (col["last_acl_sweep_at"] is None or now - col["last_acl_sweep_at"] > ACL_SWEEP_EVERY))
    cursor = None if full else col["sync_cursor"]
    if payload.get("resume_cursor"):
        cursor = payload["resume_cursor"]
    # A full listing deletes files it didn't see. Each file it lists is stamped `seen_at`, and the listing's
    # start time travels with its follow-on jobs, instead of a list of every file id seen so far.
    listed_since = payload.get("listed_since")
    if full and not listed_since:
        listed_since = (await db.fetch_row("select now() as t"))["t"].isoformat()
    connector = connector_for(col, await active_roots(col), rewalk=payload.get("rewalk"))
    stats = {"changed": 0, "unchanged": 0, "deleted": 0, "skipped": 0, "errors": 0}
    processed = 0
    try:
        while True:
            batch = await connector.list_changes(cursor)
            if batch.lost_roots:
                await roots_lost(col, batch.lost_roots)
                stats["lost_roots"] = stats.get("lost_roots", 0) + len(batch.lost_roots)
            for ch in batch.changes:
                await jobs.progress(str(job["id"]), None,
                                    f"{'Listing everything' if full else 'Checking changes'}: {processed} files so far"
                                    + (f", now {ch.title}" if ch.title else ""))
                started = time.perf_counter()
                try:
                    outcome, item_id = await process_change(col, connector, ch, full=full, sweep=sweep)
                    stats[outcome] += 1
                    await note_outcome(col, ch, outcome, item_id, started, str(job["id"]))
                except Exception as e:  # one bad file must not stop the sync
                    stats["errors"] += 1
                    log.warning("sync %s: item %s failed: %s", col["slug"], ch.external_id, e)
                    await activity.index_event("failed", title=ch.title or ch.external_id, collection_id=str(col["id"]),
                                               detail=str(e)[:500], job_id=str(job["id"]))
                processed += 1
            if full and batch.changes:
                await db.execute("update items set seen_at=now() where collection_id=%s and external_id = any(%s)",
                                 (col["id"], [ch.external_id for ch in batch.changes]))
            cursor = batch.next_cursor
            if not batch.has_more:
                break
            if processed >= MAX_ITEMS_PER_JOB:
                # Continue in a fresh job so progress is saved and other work (like a file someone
                # asked for) can run in between.
                await jobs.enqueue("sync", {"collection_id": str(col["id"]), "full": full, "sweep": sweep,
                                            "resume_cursor": cursor, "listed_since": listed_since},
                                   dedupe_key=f"sync-cont:{col['id']}", priority=90)
                return {**stats, "continued": True}
        if full:
            gone = await db.fetch_all(
                """select external_id from items where collection_id=%s and status <> 'deleted'
                   and seen_at < %s::timestamptz""", (col["id"], listed_since))
            async with db.tx() as conn:
                for g in gone:
                    await items.mark_deleted(conn, str(col["id"]), g["external_id"])
                    stats["deleted"] += 1
        await db.execute(
            """update collections set sync_cursor=%s, last_synced_at=now(), sync_error=null,
                      last_full_sync_at = case when %s then now() else last_full_sync_at end,
                      last_acl_sweep_at = case when %s then now() else last_acl_sweep_at end
               where id=%s""",
            (cursor, full, sweep, col["id"]),
        )
        return stats
    except Exception as e:
        await db.execute("update collections set sync_error=%s where id=%s", (str(e)[:1000], col["id"]))
        raise
    finally:
        await connector.close()


async def schedule_due_syncs() -> int:
    """Queue a sync for every collection with shared roots that's due (as often as the admin page's
    refresh setting says, two minutes by default), unless one is already queued or running."""
    rows = await db.fetch_all(
        """select c.id from collections c
           where c.status='active'
             and (c.last_synced_at is null or c.last_synced_at < now() - make_interval(secs => %s))
             and exists (select 1 from source_roots r where r.collection_id = c.id and r.status = 'active')
             and not exists (select 1 from jobs j where j.kind = 'sync' and j.status in ('queued', 'running')
                             and j.payload->>'collection_id' = c.id::text)""",
        ((await activity.refresh_settings())["sync_seconds"],),
    )
    for r in rows:
        await jobs.enqueue("sync", {"collection_id": str(r["id"])}, dedupe_key=f"sync:{r['id']}", priority=80)
    return len(rows)


# The Drive collection, and finding new shares -----------------------------------------------


async def drive_space(conn: AsyncConnection | None = None) -> dict:
    """The one collection everything shared with Nebula in Drive lands in."""
    sql = "select * from collections where slug=%s and kind='gdrive'"
    row = await db.fetch_one(sql, (DRIVE_SLUG,), conn)
    if row is None:
        await db.execute(
            """insert into collections (slug, name, description, kind)
               values (%s, 'Google Drive', 'Folders, files and Shared Drives people share with Nebula.', 'gdrive')
               on conflict (slug) do nothing""",
            (DRIVE_SLUG,), conn)
        row = await db.fetch_one(sql, (DRIVE_SLUG,), conn)
    if row is None:
        raise ValueError("The Drive source collection is missing. Ask an administrator.")
    return row


async def _person_for(conn: AsyncConnection, email: str) -> str | None:
    if not email:
        return None
    row = await db.fetch_one(
        "insert into people (email) values (%s) on conflict (email) do update set email=excluded.email "
        "returning id, status", (email.lower(),), conn)
    return str(row["id"]) if row and row["status"] == "active" else None


@jobs.handler("drive_discover")
async def discover(job: dict | None = None, payload: dict | None = None) -> dict:
    """Register everything newly shared with Nebula in Drive, and revive roots shared again."""
    if not gdrive.service_account_email():
        return {"skipped": "Drive isn't configured"}
    space = await drive_space()
    connector = get_connector("gdrive", {"roots": []})
    try:
        found = await connector.shared_with_nebula()  # type: ignore[attr-defined]
    finally:
        await connector.close()
    known = {r["external_id"]: r for r in await db.fetch_all(
        "select id, external_id, status, status_note from source_roots where collection_id=%s", (space["id"],))}
    added, revived, upgraded, skipped = [], [], [], []
    async with db.tx() as conn:
        for f in found:
            row = known.get(f["id"])
            note = VIEWER_NOTE if f.get("editor") is False else ""
            if row is None and not _accepted_share(f):
                skipped.append(f["name"] or f["id"])
                continue
            if row is None:
                manager = await _person_for(conn, f["shared_by"])
                await conn.execute(
                    """insert into source_roots (collection_id, external_id, kind, name, managed_by, shared_by,
                                                 added_via, status_note)
                       values (%s, %s, %s, %s, %s, %s, 'discovery', %s) on conflict do nothing""",
                    (space["id"], f["id"], f["kind"], f["name"], manager, f["shared_by"], note))
                added.append(f["name"] or f["id"])
            elif row["status"] == "lost":
                await conn.execute(
                    """update source_roots set status='active', status_note=%s, name=%s, updated_at=now()
                       where id=%s""", (note, f["name"], row["id"]))
                revived.append(f["name"] or f["id"])
            elif row["status"] == "active" and note != (row["status_note"] or "") and \
                    (note or row["status_note"] == VIEWER_NOTE):
                await conn.execute("update source_roots set status_note=%s, updated_at=now() where id=%s",
                                   (note, row["id"]))
                if not note:
                    upgraded.append(f["id"])  # now an Editor: re-read the sharing of everything under it
            # A 'removed' root was disconnected on purpose; only an explicit connect brings it back.
        if added or revived:
            await jobs.enqueue("sync", {"collection_id": str(space["id"])}, conn=conn,
                               dedupe_key=f"sync-roots:{space['id']}", priority=30)
        if upgraded:
            await jobs.enqueue("sync", {"collection_id": str(space["id"]), "rewalk": upgraded}, conn=conn,
                               dedupe_key=f"sync-upgraded:{space['id']}", priority=30)
        if added or revived or upgraded:
            await activity.audit(conn, None, "drive_discover", "collection", str(space["id"]),
                                 {"added": added, "revived": revived, "now_editor": upgraded})
    if skipped:
        log.info("drive discovery skipped %d share(s) not from RL accounts: %s", len(skipped), skipped[:10])
    return {"shared_with_nebula": len(found), "added": added, "revived": revived, "now_editor": upgraded,
            "skipped": skipped}


def _accepted_share(f: dict) -> bool:
    """Discovery only takes folders and files shared by someone at RL. Drive doesn't say who added Nebula
    to a Shared Drive, so those come in only when someone at RL connects one with its link."""
    return f["kind"] != "drive" and settings().is_member(f["shared_by"])


# One file on request ------------------------------------------------------------------------


@jobs.handler("sync_item")
async def sync_item(job: dict, payload: dict) -> dict:
    """Bring one Drive file up to date and make it searchable before the job finishes."""
    col = await db.fetch_one("select * from collections where id=%s", (payload["collection_id"],))
    if not col or col["status"] != "active":
        return {"skipped": True}
    ext = payload["external_id"]
    connector = connector_for(col, await active_roots(col))
    known = await db.fetch_one("select title from items where collection_id=%s and external_id=%s", (col["id"], ext))
    await jobs.progress(str(job["id"]), 0.1, f"Reading {known['title'] if known else 'a file'} from Drive")
    started = time.perf_counter()
    try:
        change = await connector.change_for(ext, col["sync_cursor"])  # type: ignore[attr-defined]
        if change is None:
            async with db.tx() as conn:
                gone = await items.mark_deleted(conn, str(col["id"]), ext)
            return {"available": False, "removed_from_nebula": bool(gone),
                    "note": "Nebula can't read that file in Drive any more, or it's no longer in anything shared "
                            "with Nebula."}
        outcome, item_id = await process_change(col, connector, change, refetch=bool(payload.get("refetch")))
        await note_outcome(col, change, outcome, item_id, started, str(job["id"]))
    finally:
        await connector.close()
    if not item_id:
        return {"available": False, "outcome": outcome,
                "note": "Nebula couldn't download or read that file (it may be too large, or downloads are "
                        "restricted for viewers)."}
    await jobs.progress(str(job["id"]), 0.7, "Making it searchable")
    # Bounded, so one big file can't hold up Drive checks and discovery; the rest continues in slices.
    left = await index.embed_requested(("item", item_id))
    for d in await db.fetch_all("select id from datasets where item_id=%s", (item_id,)):
        left += await index.embed_requested(("dataset", str(d["id"])))
    it = await db.fetch_one("select title, version, status, status_note from items where id=%s", (item_id,))
    if it is None:
        return {"available": False, "note": "The file was removed from Nebula while it was being read."}
    note = it["status_note"] or ""
    if left:
        note = (note + " " if note else "") + (
            f"It's read, and {left:,} pieces are still being made searchable; that continues in the background. "
            "Check with sources action 'status'.")
    return {"item": f"item:{item_id}", "title": it["title"], "version": it["version"], "outcome": outcome,
            "ready": it["status"] == "active" and not left, **({"note": note} if note else {})}


async def fetch_original(item: dict) -> tuple[bytes, str, bool]:
    """A file's current original from Drive, for showing a page or picture to Claude. Returns (bytes, a
    filename with the right extension, whether Drive has a newer version than the one Nebula read)."""
    if not gdrive.service_account_email():
        raise ValueError("Drive isn't set up on this Nebula, so it can't fetch the original.")
    col = await db.fetch_one("select * from collections where id=%s", (item["collection_id"],))
    if col is None:
        raise ValueError("That file isn't in Nebula any more.")
    connector = connector_for(col, [])
    try:
        got = await connector.download(item["external_id"])  # type: ignore[attr-defined]
    finally:
        await connector.close()
    if got is None:
        raise ValueError("Nebula couldn't download this file from Drive just now. It may have been removed or "
                         "unshared, or be over 50 MB.")
    file, plan, _mime, data = got
    newer = bool(item.get("source_rev")) and gdrive.revision(file) != item["source_rev"]
    return data, gdrive.filename_for(file, plan), newer
