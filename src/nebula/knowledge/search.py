"""Hybrid search: meaning (vectors) + words (full text), fused, reranked, filtered to what
the person may open, then grouped per file with citations."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any

from nebula import access, db, models, trace
from nebula.config import settings
from nebula.knowledge import items, render

log = logging.getLogger(__name__)

STOPWORDS = [
    # English
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "were", "be", "been", "by",
    "with", "what", "which", "who", "whom", "how", "when", "where", "why", "did", "do", "does", "we", "our", "us",
    "it", "its", "this", "that", "these", "those", "about", "from", "at", "as", "any", "all", "can", "could",
    "should", "would", "will", "there", "their", "they", "i", "me", "my", "you", "your", "has", "have", "had",
    # Hindi
    "है", "हैं", "था", "थे", "थी", "का", "की", "के", "को", "में", "से", "पर", "और", "या", "यह", "वह", "जो", "तो",
    "कि", "भी", "क्या", "कैसे", "कब", "कहाँ", "कौन", "हम", "हमने",
]

TYPES = ("item", "dataset")
RRF_K = 60
CANDIDATES = 40
# A file this short comes back whole, so Claude doesn't answer about it from two excerpts.
FULL_TEXT_CHARS = 4000
FULL_TEXT_BUDGET = 12000  # whole-file text per search, across all results
EXCERPT_NOTE = ("Matches are short excerpts, not whole documents. Open an id to read it in full before "
                "summarising, retelling or answering about the whole thing.")


@dataclass
class Candidate:
    chunk_id: int
    source_type: str
    source_id: str
    heading: str
    text: str
    locator: dict
    kind: str
    score: float = 0.0


def _traced(r: Any) -> dict:
    """One candidate passage, as the admin page shows it."""
    if isinstance(r, Candidate):
        return {"chunk": r.chunk_id, "src": f"{r.source_type}:{r.source_id}", "where": where_text(r.locator, r.heading)}
    return {"chunk": r["id"], "src": f"{r['source_type']}:{r['source_id']}",
            "where": where_text(r["locator"] or {}, r["heading"])}


def trace_rows(key: str) -> list[dict]:
    t = trace._current.get()
    return (t or {}).get(key) or []


async def search(
    person: access.Person,
    query: str,
    *,
    types: list[str] | None = None,
    limit: int | None = None,
    since: dt.date | None = None,
) -> dict:
    trace.begin()
    limit = max(1, min(limit or settings().search_default_limit, 25))
    types = [t for t in (types or TYPES) if t in TYPES]
    visible, scope = access.item_filter(person, "c.item_id")
    trace.step("scope")
    trace.note(query=query, limit=limit, types=types)

    params = {**scope, "types": types, "k": CANDIDATES}
    where = f"c.source_type = any(%(types)s::text[]) and {visible}"
    qvec = (await models.embed([query], "query"))[0]
    trace.step("embed")

    async with db.tx() as conn:
        await conn.execute("set local hnsw.iterative_scan = 'relaxed_order'")
        await conn.execute("set local hnsw.ef_search = 120")
        vec_rows = await db.fetch_all(
            f"""select id, source_type, source_id, heading, text, locator, kind
                from chunks c where c.embedding is not null and {where}
                order by c.embedding <=> %(q)s::halfvec limit %(k)s""",  # noqa: S608 - fixed fragments
            {**params, "q": qvec},
            conn,
        )
        trace.step("meaning")
        kw_rows = await db.fetch_all(
            f"""with q as (
                    select to_tsquery('simple', string_agg(quote_literal(lexeme), ' | ')) as tsq
                    from unnest(to_tsvector('simple', %(text)s)) where lexeme <> all(%(stop)s::text[])
                )
                select c.id, c.source_type, c.source_id, c.heading, c.text, c.locator, c.kind
                from chunks c, q where q.tsq is not null and c.tsv @@ q.tsq and {where}
                order by ts_rank_cd(c.tsv, q.tsq, 1) desc limit %(k)s""",  # noqa: S608 - fixed fragments
            {**params, "text": query, "stop": STOPWORDS},
            conn,
        )
        trace.step("words")
        if trace.active():
            terms = await db.fetch_one(
                "select array_agg(lexeme) as t from unnest(to_tsvector('simple', %s)) where lexeme <> all(%s::text[])",
                (query, STOPWORDS), conn)
            trace.note(terms=terms["t"] or [], meaning=[_traced(r) for r in vec_rows],
                       words=[_traced(r) for r in kw_rows])

    fused: dict[int, Candidate] = {}
    for ranked in (vec_rows, kw_rows):
        for rank, r in enumerate(ranked):
            c = fused.get(r["id"])
            if c is None:
                c = fused[r["id"]] = Candidate(r["id"], r["source_type"], str(r["source_id"]), r["heading"], r["text"],
                                               r["locator"] or {}, r["kind"])
            c.score += 1.0 / (RRF_K + rank + 1)
    # Keep enough candidates for the requested result count; a CPU-only server may use a smaller
    # reranking budget while keeping the full hybrid retrieval.
    pool = sorted(fused.values(), key=lambda c: c.score, reverse=True)[:max(limit, settings().search_rerank_candidates)]
    if trace.active():
        vrank = {r["id"]: i + 1 for i, r in enumerate(vec_rows)}
        krank = {r["id"]: i + 1 for i, r in enumerate(kw_rows)}
        trace.note(combined=len(fused), rerank_chars=settings().search_rerank_chars, pool=[
            {**_traced(c), "meaning": vrank.get(c.chunk_id), "words": krank.get(c.chunk_id),
             "text": " ".join(c.text.split())[:280]} for c in pool])
    if not pool:
        return {"results": [], "note": "Nothing found. Try other words (or the Hindi/English equivalent)."}

    try:
        order = await models.rerank(
            query, [(c.heading + "\n" + c.text)[:settings().search_rerank_chars] for c in pool], top_n=len(pool)
        )
    except Exception as e:  # a reranker outage must not break search: keep the fused order, and say so
        log.warning("rerank failed, keeping fused order: %s", e)
        order = [(i, 1.0 / (i + 1)) for i in range(len(pool))]
        trace.note(rerank_failed=f"{type(e).__name__}: {e}"[:300])
    reranked = [(pool[i], s) for i, s in order]
    trace.step("rerank")
    trace.note(reranker=settings().rerank_provider, reranked=[[i, round(float(s), 4)] for i, s in order])

    meta = await _metadata({(c.source_type, c.source_id) for c, _ in reranked})
    grouped: dict[tuple[str, str], dict] = {}
    for c, s in reranked:
        m = meta.get((c.source_type, c.source_id))
        if not m or m.get("status") == "deleted":
            continue
        if since and m.get("date") and str(m["date"]) < since.isoformat():
            continue
        key = (c.source_type, c.source_id)
        entry = grouped.get(key)
        if entry is None:
            entry = grouped[key] = {**m, "score": s, "matches": []}
        if len(entry["matches"]) < 2:
            entry["matches"].append({"where": where_text(c.locator, c.heading), "text": _snippet(c.text)})
    results = sorted(grouped.values(), key=lambda e: e["score"], reverse=True)[:limit]
    if trace.active():
        trace.note(files=len(grouped))
    for r in results:
        r["score"] = round(r["score"], 3)
    await _inline_short_items(results)
    trace.step("assemble")
    if trace.active():
        keys = {(x["src"].split(":", 1)[0], x["src"].split(":", 1)[1])
                for x in [*trace_rows("meaning"), *trace_rows("words")]}
        trace.note(titles={f"{k[0]}:{k[1]}": m.get("title") for k, m in (await _metadata(keys)).items()})
    if not results and not await _anything_visible(person):
        return {"results": [], "note": "Nothing in Nebula is shared with you yet. Share a Drive folder or file with "
                                       "Nebula (see the sources tool), or ask a colleague to share one with you."}
    if any("matches" in r for r in results):
        return {"note": EXCERPT_NOTE, "results": results}
    return {"results": results}


async def _anything_visible(person: access.Person) -> bool:
    visible, params = access.item_filter(person)
    row = await db.fetch_one(f"select exists (select 1 from items i where i.status <> 'deleted' and {visible}) as any",
                             params)  # noqa: S608 - fixed fragment
    return bool(row and row["any"])


async def _inline_short_items(results: list[dict]) -> None:
    """Swap a short file's excerpts for its whole text (as `open` would show it), marked complete."""
    ids = [r["id"].split(":", 1)[1] for r in results if r["id"].startswith("item:")]
    if not ids:
        return
    # Overlapping chunks add up to more than the text, so this skips big documents without loading them.
    rows = await db.fetch_all(
        """select i.id, i.document from items i
           where i.id = any(%s::uuid[]) and i.document is not null
             and not exists (select 1 from datasets d where d.item_id = i.id)
             and (select coalesce(sum(length(c.text)), 0) from chunks c
                  where c.source_type = 'item' and c.source_id = i.id) <= %s""",
        (ids, FULL_TEXT_CHARS * 2),
    )
    docs = {f"item:{r['id']}": items.document_of(r) for r in rows}
    budget = FULL_TEXT_BUDGET
    for r in results:
        doc = docs.get(r["id"])
        if doc is None or doc.sheets:
            continue
        text, _ = render.document_markdown(doc)
        if not text.strip() or len(text) > min(FULL_TEXT_CHARS, budget):
            continue
        budget -= len(text)
        del r["matches"]
        r["content"], r["complete"] = text, True


def _snippet(text: str, n: int = 700) -> str:
    t = re.sub(r"\s+\n", "\n", text).strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + " …"


def where_text(locator: dict, heading: str = "") -> str:
    parts = []
    if heading:
        parts.append(heading)
    if "page" in locator:
        parts.append(f"page {locator['page']}")
    if "pages" in locator:
        ps = locator["pages"]
        parts.append(f"pages {ps[0]}–{ps[-1]}" if isinstance(ps, list) and ps else f"pages {ps}")
    if "slide" in locator:
        parts.append(f"slide {locator['slide']}")
    if "sheet" in locator:
        parts.append(f"sheet '{locator['sheet']}'" + (f" rows {locator['rows']}" if locator.get("rows") else ""))
    return " · ".join(parts)


async def _metadata(keys: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    by: dict[str, list[str]] = {}
    for t, i in keys:
        by.setdefault(t, []).append(i)
    out: dict[tuple[str, str], dict] = {}
    if by.get("item"):
        for r in await db.fetch_all(
            """select id, title, kind, url, path, status, source_modified_at, updated_at
               from items where id = any(%s::uuid[])""",
            (by["item"],),
        ):
            out[("item", str(r["id"]))] = {
                "id": f"item:{r['id']}", "type": r["kind"], "title": r["title"],
                "url": r["url"], "path": " / ".join(r["path"] or []), "status": r["status"],
                "date": (r["source_modified_at"] or r["updated_at"]).date().isoformat(),
            }
    if by.get("dataset"):
        for r in await db.fetch_all(
            """select id, title, table_name, row_count, updated_at
               from datasets where id = any(%s::uuid[])""",
            (by["dataset"],),
        ):
            out[("dataset", str(r["id"]))] = {
                "id": f"dataset:{r['id']}", "type": "dataset", "title": r["title"],
                "table": f"data.{r['table_name']}", "rows": r["row_count"], "date": r["updated_at"].date().isoformat(),
            }
    return out
