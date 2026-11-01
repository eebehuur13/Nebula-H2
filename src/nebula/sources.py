"""What the `sources` tool does, and what it tells Claude: connect a Drive link now, refresh a file or a
shared folder, say where something stands, list what someone shared, and disconnect.

The background side (finding new shares, keeping files in step with Drive, refreshing one file) is in
`sync`. Whoever shared a root manages it; access never comes from the root, because each file is
visible only to the people Drive shares it with.
"""

from __future__ import annotations

import logging
import uuid

from nebula import access, activity, db, jobs, sync
from nebula.access import AccessDenied, Person
from nebula.config import settings
from nebula.knowledge import items, stages

log = logging.getLogger(__name__)

SHARE_AS = ("as an Editor. Nebula only ever reads (its Google access is read-only), but Drive only shows who "
            "a file is shared with to editors, and Nebula needs that to show each file to the right people")
ACCESS_NOTE = ("Each file is visible in Nebula only to the people Drive shares it with (by email, or everyone at RL "
               "when Drive shares it with the RL domain), and follows Drive when sharing changes. Sharing with a "
               "Google Group doesn't reach its members yet, because Nebula can't see who's in a group.")
GROUPS_NOTE = ("Drive also shares this with Google Groups ({groups}). Nebula can't see who's in a group, so their "
               "members only find it in Nebula if Drive shares it with them by email or with the RL domain.")


def _bot() -> str:
    from nebula.connectors import gdrive

    return gdrive.service_account_email()


# Queueing work -------------------------------------------------------------------------------


async def _queue_file(col: dict, external_id: str, person: Person) -> str | None:
    """Fetch one Drive file now (even if Drive says it hasn't changed) and index it straight away."""
    return await jobs.enqueue("sync_item", {"collection_id": str(col["id"]), "external_id": external_id,
                                            "refetch": True},
                              dedupe_key=f"sync-item:{col['id']}:{external_id}", person_id=person.id, priority=5)


async def _queue_root(col: dict, root: dict, person: Person, *, rewalk: bool = False) -> str | None:
    payload: dict = {"collection_id": str(col["id"])}
    if rewalk:
        payload["rewalk"] = [root["external_id"]]
    return await jobs.enqueue("sync", payload, dedupe_key=f"sync-root:{root['id']}", person_id=person.id, priority=20)


async def _with_eta(out: dict, job: str | None) -> dict:
    out["job"] = job
    out.update(await jobs.eta(job))
    out["next"] = ("It's ready once the job is done: check with the job tool, or sources action 'status'. "
                   "Tell the person roughly how long, and don't treat it as updated before then.")
    return out


# Actions behind the sources tool ---------------------------------------------------------------


async def _root_by_ref(ref: str) -> dict | None:
    """A root by its Nebula id, or by its Drive link or id."""
    from nebula.connectors import gdrive

    ref = ref.strip()
    try:
        uuid.UUID(ref)
        row = await db.fetch_one("select * from source_roots where id=%s", (ref,))
        if row:
            return row
    except ValueError:
        pass
    try:
        external = gdrive.drive_id_from(ref)
    except ValueError:
        return None
    space = await sync.drive_space()
    return await db.fetch_one("select * from source_roots where collection_id=%s and external_id=%s",
                              (space["id"], external))


def _can_manage_root(person: Person, root: dict) -> bool:
    return person.is_admin or (root["managed_by"] is not None and str(root["managed_by"]) == person.id)


async def connect(person: Person, link: str, name: str | None = None) -> dict:
    """Start (or restart) importing a Drive folder, file or Shared Drive that's shared with Nebula."""
    from nebula.connectors import gdrive

    bot = _bot()
    if not bot:
        raise ValueError("Drive isn't set up on this Nebula yet. Ask an administrator.")
    if not (person.is_admin or settings().is_member(person.email)):
        raise AccessDenied("Only people at RL can bring Drive files into Nebula.")
    fid = gdrive.drive_id_from(link)
    space = await sync.drive_space()
    root = await db.fetch_one("select * from source_roots where collection_id=%s and external_id=%s",
                              (space["id"], fid))
    if root and root["status"] == "active":
        job = await (_queue_file(space, fid, person) if root["kind"] == "file" else _queue_root(space, root, person))
        return await _with_eta({"source": str(root["id"]), "name": root["name"], "kind": root["kind"],
                                "already_connected": True}, job)
    connector = sync.connector_for(space, await sync.active_roots(space))
    try:
        meta = await connector.describe(fid)  # type: ignore[attr-defined]
        if meta is None:
            raise ValueError(f"Nebula can't open that in Drive yet. Share it with {bot} {SHARE_AS}. Then try "
                             "again. A link on its own isn't enough: Drive has to share it with that address.")
        kind = "drive" if meta.get("driveId") == fid else "folder" if meta.get("mimeType") == gdrive.FOLDER else "file"
        if kind == "file" and gdrive.classify(meta) is None:
            raise ValueError(f"Nebula can't read '{meta.get('name')}' ({meta.get('mimeType')}). It reads Google Docs, "
                             "Sheets and Slides, PDF, Word, PowerPoint, Excel, CSV and text files up to 50 MB.")
        covering = None
        if kind != "drive" and not (root and root["status"] == "removed"):
            covering = await connector.covering_root(meta, space["sync_cursor"])  # type: ignore[attr-defined]
    finally:
        await connector.close()
    cover = await db.fetch_one("select * from source_roots where collection_id=%s and external_id=%s",
                               (space["id"], covering)) if covering and covering != fid else None
    if cover is not None:
        job = await (_queue_file(space, fid, person) if kind == "file" else _queue_root(space, cover, person))
        return await _with_eta({"name": meta.get("name", ""), "kind": kind, "already_shared_through": cover["name"],
                                "note": "It's inside something Nebula already reads, so it's being refreshed "
                                        "rather than added separately."}, job)
    from nebula.connectors.gdrive import can_edit

    viewer = can_edit(meta) is False
    async with db.tx() as conn:
        row = await db.fetch_row(
            """insert into source_roots (collection_id, external_id, kind, name, managed_by, added_via, status_note)
               values (%s, %s, %s, %s, %s, 'request', %s)
               on conflict (collection_id, external_id) do update set status='active', status_note=excluded.status_note,
                   kind=excluded.kind, name=excluded.name,
                   managed_by=coalesce(source_roots.managed_by, excluded.managed_by), updated_at=now()
               returning *""",
            (space["id"], fid, kind, name or meta.get("name", ""), person.id, sync.VIEWER_NOTE if viewer else ""), conn)
        await activity.audit(conn, person, "connect_drive", "source_root", str(row["id"]),
                             {"external_id": fid, "kind": kind})
    job = await (_queue_file(space, fid, person) if kind == "file" else _queue_root(space, row, person))
    out = {"source": str(row["id"]), "name": row["name"], "kind": kind, "how_access_works": ACCESS_NOTE}
    if viewer:
        out["warning"] = sync.VIEWER_NOTE
    return await _with_eta(out, job)


async def _item_by_ref(person: Person, ref: str) -> dict:
    kind, rid = items.parse_ref(ref)
    if kind != "item":
        raise ValueError("Give a file's item id (item:…) or its Drive link.")
    await access.require(person, "item", rid)
    it = await db.fetch_one(
        "select i.*, c.last_synced_at from items i join collections c on c.id = i.collection_id where i.id=%s", (rid,))
    if it is None:
        raise ValueError("That file isn't in Nebula any more.")
    return it


async def _item_by_drive_id(person: Person, external_id: str) -> dict | None:
    rows = await db.fetch_all(
        """select i.*, c.last_synced_at from items i join collections c on c.id = i.collection_id
           where c.status='active' and i.external_id=%s order by (i.status <> 'deleted') desc""", (external_id,))
    for it in rows:
        if await access.can_open(person, str(it["id"])):
            return it
    if rows and rows[0]["status"] != "deleted":
        raise AccessDenied("Nebula has that file, but Drive doesn't share it with you, so you can't see or refresh it.")
    return None


async def request_sync(person: Person, *, link: str | None = None, item: str | None = None,
                       source: str | None = None) -> dict:
    """Refresh something now: one file (by item id or Drive link), or a whole source."""
    from nebula.connectors import gdrive

    if item or link:
        it = await _item_by_ref(person, item) if item else await _item_by_drive_id(person, gdrive.drive_id_from(link or ""))
        if it is None:
            return await connect(person, link or "")
        col = await db.fetch_one("select * from collections where id=%s", (it["collection_id"],))
        if col is None:
            raise ValueError("That file isn't in Nebula any more.")
        job = await _queue_file(col, it["external_id"], person)
        return await _with_eta({"id": f"item:{it['id']}", "title": it["title"]}, job)
    if source:
        root = await _root_by_ref(source)
        if root:
            if not _can_manage_root(person, root):
                raise AccessDenied("Only the person who shared it with Nebula (or an admin) can re-sync the whole source. "
                                   "To refresh one file, give its link or item id instead.")
            space = await sync.drive_space()
            job = await (_queue_file(space, root["external_id"], person) if root["kind"] == "file"
                         else _queue_root(space, root, person, rewalk=True))
            return await _with_eta({"source": str(root["id"]), "name": root["name"]}, job)
        raise ValueError("No such source. Use action 'list' to see the sources you shared.")
    raise ValueError("Give a Drive link, an item id, or a source from action 'list'.")


async def _file_status(person: Person, it: dict) -> dict:
    from nebula.ingest.types import Document

    counts = {"pieces": it["pieces"], "waiting": it["pieces_waiting"],
              "tables": (await db.fetch_row("select count(*) as n from datasets where item_id = %s", (it["id"],)))["n"]}
    pending = (await stages.active_reads()).get(it["external_id"])
    audience = it["source_audience"] or {}
    state = stages.stage(it["status"], counts["pieces"], counts["waiting"], counts["tables"],
                         pending["status"] if pending else None)
    out: dict = {
        "id": f"item:{it['id']}", "title": it["title"], "stage": stages.WORDS[state], "version": it["version"],
        "modified_in_source": it["source_modified_at"], "indexed_at": it["indexed_at"],
        "source_last_checked": it["last_synced_at"],
        "search_ready": state == "ready",
        "pieces_in_search": counts["pieces"],
    }
    if counts["waiting"]:
        out["pieces_waiting_for_index"] = counts["waiting"]
    if pending:
        out["refresh"] = {"job": str(pending["id"]), **await jobs.eta(str(pending["id"]))}
    if it["status_note"]:
        out["note"] = it["status_note"]
    if audience.get("unknown"):
        out["sharing"] = ("Hidden from everyone but admins: Nebula couldn't read this file's sharing in Drive. "
                          "Drive only shows sharing to editors, so make Nebula's service account an Editor of the "
                          "file or its folder (Nebula still only reads).")
    elif audience:
        bits = [f"{len(audience.get('emails', []))} people"]
        if audience.get("groups"):
            bits.append(f"{len(audience['groups'])} Google Groups")
        if audience.get("domains"):
            bits.append("domain: " + ", ".join(audience["domains"]))
        out["sharing"] = "Follows Drive: " + ", ".join(bits)
        if audience.get("groups"):
            out["sharing_note"] = GROUPS_NOTE.format(groups=", ".join(audience["groups"][:5]))
    if it.get("document"):
        summary = items.extraction_summary(Document.from_dict(it["document"]))
        if summary:
            out["extraction"] = {"status": summary["status"], "parts_not_read": len(summary["parts_not_read"]),
                                 "hint": "open the item to see which parts weren't read"}
    return out


async def _root_status(person: Person, root: dict) -> dict:
    if not _can_manage_root(person, root):
        raise AccessDenied("Only the person who shared it with Nebula (or an admin) can see a whole source's status.")
    space = await sync.drive_space()
    counts = await db.fetch_row(
        """select count(*) filter (where status <> 'deleted') as active, count(*) filter (where status='deleted') as removed,
                  count(*) filter (where status <> 'deleted' and source_audience->>'unknown'='true') as hidden
           from items where collection_id=%s and source_root=%s""", (space["id"], root["external_id"]))
    waiting = await db.fetch_one(
        """select coalesce(sum(pieces_waiting), 0) as n from items
           where collection_id=%s and source_root=%s and status <> 'deleted'""", (space["id"], root["external_id"]))
    pending = await db.fetch_one(
        """select id from jobs where status in ('queued','running') and kind in ('sync','sync_item')
           and payload->>'collection_id'=%s order by priority limit 1""", (str(space["id"]),))
    manager = await db.fetch_one("select email from people where id=%s", (root["managed_by"],)) if root["managed_by"] else None
    recent = await db.fetch_all(
        """select id, title, kind, updated_at from items where collection_id=%s and source_root=%s and status <> 'deleted'
           order by updated_at desc limit 10""", (space["id"], root["external_id"]))
    visible = [r for r in recent if await access.can_open(person, str(r["id"]))]
    out = {
        "source": str(root["id"]), "name": root["name"], "kind": root["kind"], "status": root["status"],
        "managed_by": manager["email"] if manager else None, "shared_by": root["shared_by"] or None,
        "added": root["created_at"], "added_via": root["added_via"],
        "files": {"in_nebula": counts["active"], "removed": counts["removed"]},
        "pieces_waiting_for_index": waiting["n"] if waiting else 0,
        "last_checked": space["last_synced_at"], "sync_error": space["sync_error"],
        "recent": [{"id": f"item:{r['id']}", "title": r["title"], "kind": r["kind"], "updated": r["updated_at"]}
                   for r in visible],
    }
    if root["status_note"]:
        out["note"] = root["status_note"]
    if counts["hidden"]:
        out["hidden_unreadable_sharing"] = counts["hidden"]
        out["hidden_note"] = ("These files are hidden from everyone but admins because Nebula couldn't read their "
                              "sharing in Drive. Drive only shows sharing to editors, so make Nebula's service "
                              "account an Editor (Nebula still only reads).")
    if pending:
        out["sync"] = {"job": str(pending["id"]), **await jobs.eta(str(pending["id"]))}
    return out


async def status(person: Person, *, link: str | None = None, item: str | None = None,
                 source: str | None = None) -> dict:
    from nebula.connectors import gdrive

    if item:
        return await _file_status(person, await _item_by_ref(person, item))
    if link:
        fid = gdrive.drive_id_from(link)
        root = await _root_by_ref(fid)
        if root and root["kind"] != "file":
            return await _root_status(person, root)
        it = await _item_by_drive_id(person, fid)
        if it:
            return await _file_status(person, it)
        pending = await db.fetch_one(
            "select id from jobs where status in ('queued','running') and kind='sync_item' and payload->>'external_id'=%s",
            (fid,))
        out: dict = {"in_nebula": False,
                     "note": f"Nebula doesn't have this yet. If it's shared with {_bot() or 'Nebula'} in Drive it will be "
                             "picked up within a few minutes; use action 'sync' with the link to start now."}
        if pending:
            out["refresh"] = {"job": str(pending["id"]), **await jobs.eta(str(pending["id"]))}
        return out
    if source:
        root = await _root_by_ref(source)
        if root:
            return await _root_status(person, root)
        raise ValueError("No such source. Use action 'list' to see the sources you shared.")
    return await list_sources(person)


async def list_sources(person: Person) -> dict:
    rows = await db.fetch_all(
        """select r.*, p.email as manager_email,
                  (select count(*) from items i where i.collection_id = r.collection_id
                   and i.source_root = r.external_id and i.status <> 'deleted') as files
           from source_roots r left join people p on p.id = r.managed_by
           where r.status <> 'removed' and (r.managed_by = %s or %s) order by r.created_at desc limit 200""",
        (person.id, person.is_admin))
    return {
        "sources": [{"source": str(r["id"]), "name": r["name"], "kind": r["kind"], "status": r["status"],
                     "files": r["files"], "added": r["created_at"],
                     **({"managed_by": r["manager_email"]} if person.is_admin else {}),
                     **({"note": r["status_note"]} if r["status_note"] else {})} for r in rows],
        "how_to_add": f"Share a Drive folder, file or Shared Drive with {_bot() or 'Nebula'} {SHARE_AS}. "
                      "Nebula picks it up within a few minutes.",
    }


async def disconnect(person: Person, source: str) -> dict:
    root = await _root_by_ref(source)
    if root is None:
        raise ValueError("No such source. Use action 'list' to see the sources you shared.")
    if not _can_manage_root(person, root):
        raise AccessDenied("Only the person who shared it with Nebula (or an admin) can disconnect it.")
    space = await sync.drive_space()
    async with db.tx() as conn:
        await conn.execute("update source_roots set status='removed', status_note='', updated_at=now() where id=%s",
                           (root["id"],))
        rows = await db.fetch_all(
            "select external_id from items where collection_id=%s and source_root=%s and status <> 'deleted'",
            (space["id"], root["external_id"]), conn)
        for r in rows:
            await items.mark_deleted(conn, str(space["id"]), r["external_id"])
        # Anything also inside another shared folder comes back on the next full listing.
        await jobs.enqueue("sync", {"collection_id": str(space["id"]), "full": True}, conn=conn,
                           dedupe_key=f"sync-full:{space['id']}", priority=40)
        await activity.audit(conn, person, "disconnect", "source_root", str(root["id"]),
                             {"external_id": root["external_id"]})
    return {"source": str(root["id"]), "name": root["name"], "status": "disconnected", "files_removed": len(rows),
            "note": "Its files are no longer in Nebula, and Nebula won't pick it up again unless someone asks to "
                    "connect it. Nothing in Drive changed; to cut Nebula off completely, unshare it there too."}
