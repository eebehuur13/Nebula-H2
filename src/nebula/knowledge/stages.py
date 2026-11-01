"""Where each file is in being read and made searchable, worked out the same way everywhere: the
documents tool, `sources status`, the admin page and "My files"."""

from __future__ import annotations

from typing import Any, Literal

from nebula import access, db

STAGES = ("queued", "reading", "indexing", "ready", "no_text", "failed")
WORDS = {
    "queued": "waiting to be read",
    "reading": "being read",
    "indexing": "being made searchable",
    "ready": "searchable",
    "no_text": "no readable text",
    "failed": "couldn't be read",
}


def stage(status: str, pieces: int, waiting: int, tables: int, job_status: str | None = None) -> str:
    """One of STAGES. `job_status` is that of a queued or running single-file read, if there is one."""
    if status == "error":
        return "failed"
    if job_status == "running":
        return "reading"
    if job_status == "queued":
        return "queued"
    if waiting:
        return "indexing"
    if pieces or tables:  # a CSV or sheet can be all table, with no search text
        return "ready"
    return "no_text"


async def active_reads() -> dict[str, dict]:
    """Queued or running single-file reads, by Drive file id."""
    rows = await db.fetch_all(
        """select id, status, progress, message, payload from jobs
           where status in ('queued', 'running') and kind = 'sync_item'""")
    return {str(r["payload"]["external_id"]): r for r in rows if (r["payload"] or {}).get("external_id")}


Scope = Literal["all", "shared_by", "shared_with"]


async def file_states(scope: Scope = "all", person: access.Person | None = None,
                      limit: int = 2000) -> tuple[list[dict], dict[str, int]]:
    """Files with the stage each is at, newest first. 'all' is every file (the admin page); 'shared_by' is
    what the person shared with Nebula and 'shared_with' what Drive shares with them from others ("My files")."""
    params: dict[str, Any] = {"limit": int(limit)}
    if scope == "all":
        where = "true"
    elif person is None:
        raise ValueError(f"file_states({scope!r}) needs the person")
    elif scope == "shared_by":
        where = "r.managed_by = %(me)s::uuid"
        params["me"] = person.id
    else:
        where = """exists (select 1 from item_access a where a.item_id = i.id and a.principal = any(%(principals)s))
                   and r.managed_by is distinct from %(me)s::uuid"""
        params.update(me=person.id, principals=person.principals())
    via = ""
    if person is not None:
        via = """,
                  (select string_agg(distinct case when a.principal like 'email:%%' then 'you'
                                              else 'everyone at ' || split_part(a.principal, ':', 2) end, ', ')
                     from item_access a where a.item_id = i.id and a.principal = any(%(principals)s)) as shared_via"""
        params["principals"] = person.principals()
    rows = await db.fetch_all(
        f"""select i.id, i.title, i.kind, i.filename, i.url, i.path, i.status, i.status_note, i.version,
                  i.indexed_at, i.source_modified_at, i.external_id, i.source_root,
                  c.name as collection, r.name as root_name,
                  coalesce(nullif(rp.name, ''), rp.email) as shared_by_name, rp.email as shared_by_email,
                  i.pieces, i.pieces_waiting as waiting,
                  (select count(*) from datasets d where d.item_id = i.id) as tables,
                  case when coalesce(i.document->'metadata'->'extraction'->>'status', 'ok') = 'ok' then 0
                       else coalesce((i.document->'metadata'->'extraction'->>'total_gaps')::int,
                                     jsonb_array_length(coalesce(i.document->'metadata'->'extraction'->'gaps', '[]'::jsonb)))
                       end as unread_parts
                  {via}
           from items i join collections c on c.id = i.collection_id
           left join source_roots r on r.collection_id = i.collection_id and r.external_id = i.source_root
           left join people rp on rp.id = r.managed_by
           where i.status <> 'deleted' and ({where})
           order by coalesce(i.indexed_at, i.updated_at) desc nulls last limit %(limit)s""",  # noqa: S608 - fixed text
        params)
    reads = await active_reads()
    files = []
    counts = dict.fromkeys(STAGES, 0)
    for r in rows:
        job = reads.get(r["external_id"])
        state = stage(r["status"], r["pieces"], r["waiting"], r["tables"], job["status"] if job else None)
        counts[state] += 1
        fmt = (r["filename"].rsplit(".", 1)[-1].lower() if "." in (r["filename"] or "") else "") or r["kind"]
        files.append({
            "id": str(r["id"]), "title": r["title"], "format": fmt, "kind": r["kind"], "url": r["url"],
            "path": " / ".join(r["path"] or []), "source": r["root_name"] or r["collection"],
            "state": state, "status_note": r["status_note"],
            "version": r["version"], "pieces": r["pieces"], "ready": r["pieces"] - r["waiting"], "tables": r["tables"],
            "indexed_at": r["indexed_at"], "modified_in_source": r["source_modified_at"],
            "unread_parts": (r["unread_parts"] or 0) if r["status"] == "active" else 0,
            "job": ({"id": str(job["id"]), "status": job["status"], "progress": job["progress"],
                     "message": job["message"]} if job else None),
            "shared_by": ({"name": r["shared_by_name"], "email": r["shared_by_email"]}
                          if r["shared_by_email"] else None),
            "shared_via": r.get("shared_via") or "",
            "source_root": r["source_root"],
        })
    return files, counts
