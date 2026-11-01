"""Files: a structured Document, chunks and any data tables. Nebula keeps no copy of the original;
Drive has it (see `sync.fetch_original`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any

from psycopg import AsyncConnection

from nebula import access, db
from nebula.connectors.base import FetchedItem
from nebula.ingest.types import Chunk, Document
from nebula.knowledge import datasets, index

log = logging.getLogger(__name__)

PREVIEW_ROWS = 50  # rows of each sheet kept in items.document; the rest live in SQL tables


def parse_ref(ref: str) -> tuple[str, str]:
    """'item:<id>' or 'dataset:<id>' (optionally prefixed 'nebula:') -> (kind, id)."""
    kind, _, rid = ref.strip().removeprefix("nebula:").partition(":")
    rid = rid.split("#", 1)[0]
    try:
        uuid.UUID(rid)
    except ValueError:
        raise ValueError(f"'{ref}' isn't a Nebula id. Ids look like item:… or dataset:….") from None
    if kind not in ("item", "dataset"):
        raise ValueError(f"'{ref}' isn't a Nebula id. Ids look like item:… or dataset:….")
    return kind, rid


def _doc_for_storage(doc: Document) -> dict:
    slim = replace(doc, sheets=[replace(s, rows=s.rows[:PREVIEW_ROWS]) for s in doc.sheets])
    return json.loads(json.dumps(slim.to_dict(), default=str))


def _unreadable(doc: Document | None, note: str) -> bool:
    """The parser failed on this file (as opposed to it being a type Nebula only stores)."""
    if doc is None:
        return note.startswith("could not read")
    return bool(doc.metadata.get("parse_error") or doc.metadata.get("encrypted"))


def _parse(data: bytes, filename: str, mime: str, title: str) -> tuple[Document | None, str]:
    from nebula.ingest import UnsupportedFormat, parse

    try:
        return parse(data, filename=filename, mime=mime or None, title=title), ""
    except UnsupportedFormat as e:
        return None, f"Nebula doesn't read text from this kind of file: {e}"
    except Exception as e:  # a broken file must not stop a sync
        log.warning("parse failed for %s: %s", filename, e)
        return None, f"could not read this file: {e}"


@dataclass
class Prepared:
    """A fetched file read and split into passages, before anything is written to the database."""
    hash: str | None
    doc: Document | None = None
    note: str = ""
    chunks: list[Chunk] | None = None
    skipped: bool = False  # the content matched what's stored, so nothing was parsed


def _content_hash(fetched: FetchedItem) -> str | None:
    if fetched.data is not None:
        return hashlib.sha256(fetched.data).hexdigest()
    if fetched.document is not None:
        return hashlib.sha256(json.dumps(fetched.document.to_dict(), sort_keys=True, default=str).encode()).hexdigest()
    return None


async def prepare(fetched: FetchedItem, known_hash: str | None = None) -> Prepared:
    """Read a fetched file and split it into passages. This is the slow part, so it runs in a thread
    (the worker keeps its heartbeat and Drive checks going) and before any transaction opens (no
    database connection or lock is held meanwhile). With the stored `known_hash`, an unchanged file
    isn't parsed at all."""
    h = _content_hash(fetched)
    if known_hash is not None and h == known_hash:
        return Prepared(h, skipped=True)
    return await asyncio.to_thread(_prepare_sync, fetched, h)


def _prepare_sync(fetched: FetchedItem, h: str | None) -> Prepared:
    from nebula.ingest import chunk_document
    from nebula.ingest.quality import assess

    doc, note = fetched.document, ""
    if fetched.data is not None and doc is None:
        doc, note = _parse(fetched.data, fetched.filename or "file", fetched.mime_type, fetched.title)
    chunks = None
    if doc is not None:
        if not doc.title:
            doc.title = fetched.title
        doc.metadata["extraction"] = assess(doc)
        chunks = [c for c in chunk_document(doc) if not (c.kind == "card" and _is_data_sheet(doc, c))]
    return Prepared(h, doc, note, chunks)


async def upsert(conn: AsyncConnection, collection_id: str, fetched: FetchedItem, *, prepared: Prepared | None = None,
                 force: bool = False) -> dict:
    """Create or update one item from the connector. Returns {id, changed, version}. Pass `prepared` (from
    `prepare`, called before the transaction) so the slow reading isn't done while the transaction is open."""
    if prepared is None:
        prepared = await prepare(fetched)
    h = prepared.hash

    existing = await db.fetch_one(
        """select id, content_hash, version, status,
                  (document is not null and document->'metadata'->>'parse_error' is null) as readable
           from items where collection_id = %s and external_id = %s""",
        (collection_id, fetched.external_id),
        conn,
    )
    meta = dict(
        title=fetched.title, url=fetched.url, mime_type=fetched.mime_type, filename=fetched.filename,
        path=fetched.path, author=fetched.author, kind=fetched.kind,
        source_created_at=fetched.created_at, source_modified_at=fetched.modified_at,
    )
    if existing and existing["content_hash"] == h and existing["status"] != "deleted" and not force:
        await conn.execute(
            """update items set title=%(title)s, url=%(url)s, path=%(path)s, author=%(author)s,
                   source_modified_at=%(source_modified_at)s, updated_at=now() where id=%(id)s""",
            {**meta, "id": existing["id"]},
        )
        return {"id": str(existing["id"]), "changed": False, "version": existing["version"]}
    if prepared.skipped:  # it changed after `prepare` looked; rare, so read it here
        prepared = await prepare(fetched)

    doc, note = prepared.doc, prepared.note
    if existing and existing["status"] == "active" and existing["readable"] and _unreadable(doc, note):
        # Never swap a readable copy for one Nebula couldn't read: keep the last good version, and say so.
        reason = (note or (doc.metadata.get("parse_error") if doc else "") or "unreadable")[:200]
        when = fetched.modified_at.date().isoformat() if fetched.modified_at else "recently"
        await conn.execute(
            "update items set content_hash=%s, status_note=%s where id=%s",
            (h, f"The source has a newer version ({when}) that Nebula couldn't read ({reason}). This is the last "
                "version it could read.", existing["id"]))
        return {"id": str(existing["id"]), "changed": False, "version": existing["version"], "kept_previous": True}

    item_id = str(existing["id"]) if existing else str(uuid.uuid4())
    version = (existing["version"] if existing else 0) + 1
    status = "error" if _unreadable(doc, note) else "active"
    if status == "error" and not note:
        note = "could not read this file: " + (doc.metadata.get("parse_error") or "it's password-protected")

    await conn.execute(
        """
        insert into items (id, collection_id, external_id, kind, title, url, mime_type, filename, path, author,
                           source_created_at, source_modified_at, content_hash, version, document,
                           status, status_note, indexed_at)
        values (%(id)s, %(collection_id)s, %(external_id)s, %(kind)s, %(title)s, %(url)s, %(mime_type)s,
                %(filename)s, %(path)s, %(author)s, %(source_created_at)s, %(source_modified_at)s, %(hash)s,
                %(version)s, %(document)s, %(status)s, %(note)s, now())
        on conflict (collection_id, external_id) do update set
            kind=excluded.kind, title=excluded.title, url=excluded.url, mime_type=excluded.mime_type,
            filename=excluded.filename, path=excluded.path, author=excluded.author,
            source_created_at=excluded.source_created_at, source_modified_at=excluded.source_modified_at,
            content_hash=excluded.content_hash, version=excluded.version, document=excluded.document,
            status=excluded.status, status_note=excluded.status_note,
            indexed_at=now(), updated_at=now()
        """,
        {
            **meta, "id": item_id, "collection_id": collection_id, "external_id": fetched.external_id,
            "hash": h, "version": version,
            "document": db.jsonb(_doc_for_storage(doc)) if doc else None,
            "status": status, "note": note,
        },
    )
    await reindex(conn, item_id, collection_id, doc, fetched.title, prepared.chunks)
    return {"id": item_id, "changed": True, "version": version}


async def reindex(conn: AsyncConnection, item_id: str, collection_id: str, doc: Document | None, title: str,
                  chunks: list[Chunk] | None = None) -> None:
    """Write a file's passages and spreadsheet tables. `chunks` are the passages `prepare` made, if any."""
    keep: set[str] = set()
    if doc is not None:
        if chunks is None:
            from nebula.ingest import chunk_document

            chunks = [c for c in chunk_document(doc) if not (c.kind == "card" and _is_data_sheet(doc, c))]
        for sheet in doc.sheets:
            if sheet.kind == "data" and sheet.rows:
                dataset_id, card = await datasets.load_sheet(
                    conn, collection_id=collection_id, item_id=item_id, doc_title=title or doc.title, sheet=sheet
                )
                keep.add(sheet.name)
                await index.replace_chunks(conn, "dataset", dataset_id, item_id, [card])
    else:
        chunks = [Chunk(text=f"{title}\n(file without readable text)", heading_path=[title])]
    await datasets.drop_for_item(conn, item_id, keep_sheets=keep)
    await index.replace_chunks(conn, "item", item_id, item_id, chunks)


def _is_data_sheet(doc: Document, chunk: Chunk) -> bool:
    name = chunk.locator.get("sheet")
    return any(s.name == name and s.kind == "data" and s.rows for s in doc.sheets)


async def mark_deleted(conn: AsyncConnection, collection_id: str, external_id: str) -> str | None:
    row = await db.fetch_one(
        "update items set status='deleted', updated_at=now() where collection_id=%s and external_id=%s "
        "and status <> 'deleted' returning id",
        (collection_id, external_id),
        conn,
    )
    if not row:
        return None
    item_id = str(row["id"])
    await index.drop_chunks(conn, item_id)
    await datasets.drop_for_item(conn, item_id)
    # Forget its text too. The row stays as a marker, so a file that comes back is simply read again.
    await conn.execute("update items set document = null where id = %s", (item_id,))
    return item_id


async def get(item_id: str, conn: AsyncConnection | None = None) -> dict | None:
    return await db.fetch_one(
        """select i.*, c.name as collection_name, c.slug as collection_slug
           from items i join collections c on c.id = i.collection_id where i.id = %s""",
        (item_id,),
        conn,
    )


def document_of(row: dict[str, Any]) -> Document | None:
    return Document.from_dict(row["document"]) if row.get("document") else None


async def apply_source_audience(conn: AsyncConnection, item_id: str, audience: dict | None) -> dict:
    """Mirror who Drive shares the file with onto `item_access`, replacing what was there, so a share
    removed in Drive is removed here on the next sync. The file's datasets and search passages
    follow it. Google Groups, public links and other domains are recorded but not mirrored."""
    await conn.execute("delete from item_access where item_id=%s", (item_id,))
    await conn.execute("update items set source_audience=%s where id=%s", (db.jsonb(audience or {}), item_id))
    if not audience or audience.get("unknown"):
        return {"mirrored": False, "reason": "source permissions unknown"}
    principals = access.principals_for(audience)
    for p in principals:
        await conn.execute("insert into item_access (item_id, principal) values (%s, %s) on conflict do nothing",
                           (item_id, p))
    return {"mirrored": True, "principals": len(principals), "groups_not_mirrored": audience.get("groups", []),
            "public_link_ignored": bool(audience.get("public"))}


FIX_HINT = ("Text extraction missed these parts. If they matter for what the person asked, open each one with "
            "`part` and as_image=true and read it yourself. Don't guess at what you can't read. 'uncertain' parts "
            "may be fine (a logo or a decorative photo).")


def extraction_summary(doc: Document | None) -> dict | None:
    """What text extraction may have missed in this version."""
    ext = (doc.metadata.get("extraction") if doc else None) or {}
    if not ext or ext.get("status") == "ok":
        return None
    gaps = ext.get("gaps", [])
    out: dict[str, Any] = {"status": ext["status"], "parts_not_read": gaps[:15], "how_to_fix": FIX_HINT}
    if len(gaps) > 15 or ext.get("total_gaps", 0) > len(gaps):
        out["more_parts_not_read"] = max(len(gaps), ext.get("total_gaps", 0)) - 15
    return out
