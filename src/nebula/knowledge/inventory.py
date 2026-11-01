"""The list of documents a person can open in Nebula, for "what documents do you have?".

It uses the same visibility as search (`access.item_filter`), so it never lists a file that search
wouldn't return to that person."""

from __future__ import annotations

from typing import Any

from nebula import access, db
from nebula.knowledge import stages

PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _format(filename: str | None, kind: str | None, mime: str | None) -> str:
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    m = mime or ""
    for key, name in (("document", "google doc"), ("spreadsheet", "google sheet"), ("presentation", "google slides")):
        if key in m:
            return name
    return kind or "file"


async def list_documents(person: access.Person, *, title: str | None = None, folder: str | None = None,
                         shared: str = "all", order: str = "folder", page: int = 1,
                         page_size: int = PAGE_SIZE) -> dict[str, Any]:
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    visible, params = access.item_filter(person)
    params = {**params, "me": person.id}
    where = ["i.status <> 'deleted'", visible]
    mine = "r.managed_by = %(me)s::uuid"
    if shared == "by_me":
        where.append(mine)
    elif shared == "with_me":
        where.append("r.managed_by is distinct from %(me)s::uuid")
    if title:
        where.append("i.title ilike %(title)s")
        params["title"] = f"%{title.strip()}%"
    if folder:
        where.append("(array_to_string(i.path, ' / ') ilike %(folder)s or r.name ilike %(folder)s)")
        params["folder"] = f"%{folder.strip()}%"
    sql_where = " and ".join(where)
    base = f"""from items i
               left join source_roots r on r.collection_id=i.collection_id and r.external_id=i.source_root
               where {sql_where}"""  # noqa: S608 - fragments above are fixed text; values are parameters
    total = (await db.fetch_one(f"select count(*) as n {base}", params))["n"]
    if not total and not title and not folder and shared == "all":
        return {"total": 0, "documents": [], "note": "Nothing in Nebula is shared with this person yet. Share a "
                                                       "Drive folder or file with Nebula (see the sources tool)."}
    sort = ("coalesce(i.source_modified_at, i.updated_at) desc" if order == "recent"
            else "r.name, array_to_string(i.path, ' / '), lower(i.title)")
    rows = await db.fetch_all(
        f"""select i.id, i.title, i.kind, i.filename, i.mime_type, i.url, i.path, i.status, i.external_id,
                   coalesce(i.source_modified_at, i.updated_at) as modified, r.name as root_name,
                   (select p.email from people p where p.id = r.managed_by) as shared_by,
                   (select count(*) from datasets d where d.item_id=i.id) as tables,
                   i.pieces, i.pieces_waiting as waiting
            {base} order by {sort} limit %(lim)s offset %(off)s""",
        {**params, "lim": page_size, "off": (page - 1) * page_size})
    reads = await stages.active_reads()
    docs = []
    for r in rows:
        job = reads.get(r["external_id"])
        state = stages.WORDS[stages.stage(r["status"], r["pieces"], r["waiting"], r["tables"],
                                          job["status"] if job else None)]
        source = r["root_name"] or "Google Drive"
        folder_path = " / ".join(r["path"] or []) or None
        docs.append({
            "id": f"item:{r['id']}", "title": r["title"], "format": _format(r["filename"], r["kind"], r["mime_type"]),
            "folder": folder_path if folder_path != source else None, "source": source,
            "shared_by": r["shared_by"], "modified": r["modified"].date().isoformat() if r["modified"] else None,
            "state": state, **({"tables": r["tables"]} if r["tables"] else {}), "url": r["url"],
        })
    pages = max(1, -(-total // page_size))
    out: dict[str, Any] = {"total": total, "page": page, "pages": pages, "documents": docs}
    if page < pages:
        out["next_page"] = page + 1
    if not docs:
        out["note"] = "No documents match." if (title or folder or shared != "all") else "No documents yet."
    return out
