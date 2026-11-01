"""Spreadsheets that are tables of data become real SQL tables in schema `data`.

The search index only gets the sheet's card (what it is, columns, ranges, period);
number questions are answered with SQL through the `query` tool, which runs in DuckDB (below).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import tempfile
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection, sql

from nebula import access, db
from nebula.ingest.types import Chunk, Sheet

PG_TYPES = {
    "integer": "bigint",
    "numeric": "numeric",
    "date": "date",
    "timestamp": "timestamp",
    "boolean": "boolean",
    "text": "text",
}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %Y", "%B %Y",
                 "%m/%d/%Y", "%Y/%m/%d")


def _num(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return Decimal(int(v))
    if isinstance(v, (int, float, Decimal)):
        try:
            return Decimal(str(v))
        except InvalidOperation:
            return None
    s = str(v).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d / 100 if pct else d


def _date(v: Any) -> dt.date | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    for f in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _ts(v: Any) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    d = _date(v)
    if d:
        return dt.datetime(d.year, d.month, d.day)
    try:
        return dt.datetime.fromisoformat(str(v).strip()).replace(tzinfo=None)
    except ValueError:
        return None


def _bool(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1", "हाँ", "हां"):
        return True
    if s in ("false", "no", "n", "0", "नहीं"):
        return False
    return None


def convert(value: Any, typ: str) -> Any:
    if typ == "integer":
        d = _num(value)
        if d is None:
            return None
        return int(d) if d == d.to_integral_value() and abs(d) < 2**62 else None
    if typ == "numeric":
        return _num(value)
    if typ == "date":
        return _date(value)
    if typ == "timestamp":
        return _ts(value)
    if typ == "boolean":
        return _bool(value)
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def table_name_for(item_id: str, sheet_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", sheet_name.lower()).strip("_")[:24] or "sheet"
    return f"t_{item_id.replace('-', '')[:12]}_{slug}"


async def load_sheet(
    conn: AsyncConnection,
    *,
    collection_id: str,
    item_id: str,
    doc_title: str,
    sheet: Sheet,
) -> tuple[str, Chunk]:
    """(Re)create the SQL table for one data sheet and its dataset row. Returns (dataset id, card chunk)."""
    from nebula.ingest import infer_column_types, normalize_column_names, sheet_card

    types = infer_column_types(sheet)
    names = normalize_column_names(sheet.header)
    table = table_name_for(item_id, sheet.name)
    ident = sql.Identifier("data", table)

    await conn.execute(sql.SQL("drop table if exists {}").format(ident))
    cols = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(PG_TYPES.get(t, "text"))) for n, t in zip(names, types)
    )
    await conn.execute(sql.SQL("create table {} ({})").format(ident, cols))

    width = len(names)
    # The parser keeps blank rows so spreadsheet row numbers stay right; they must not become
    # rows of NULLs in the table (they would inflate every count).
    data_rows = [r for r in sheet.rows if any(v not in (None, "") and str(v).strip() not in ("", "-", "#N/A")
                                               for v in r[:width])]
    copy_sql = sql.SQL("copy {} ({}) from stdin").format(ident, sql.SQL(", ").join(map(sql.Identifier, names)))
    async with conn.cursor() as cur:
        async with cur.copy(copy_sql) as copy:
            for row in data_rows:
                vals = list(row[:width]) + [None] * (width - len(row))
                await copy.write_row([convert(v, t) for v, t in zip(vals, types)])

    card = sheet_card(sheet, doc_title, types)
    card += f"\n\nSQL table: data.{table} (use the `query` tool)."
    columns = [{"name": n, "original": o, "type": t} for n, o, t in zip(names, sheet.header, types)]
    row = await db.fetch_row(
        """
        insert into datasets (collection_id, item_id, sheet, table_name, title, card, columns, row_count)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (item_id, sheet) do update set
            table_name = excluded.table_name, title = excluded.title, card = excluded.card,
            columns = excluded.columns, row_count = excluded.row_count, updated_at = now()
        returning id
        """,
        (collection_id, item_id, sheet.name, table, f"{doc_title} › {sheet.name}", card,
         db.jsonb(columns), len(data_rows)),
        conn,
    )
    chunk = Chunk(text=card, heading_path=[doc_title, sheet.name], locator={"sheet": sheet.name}, kind="card")
    return str(row["id"]), chunk


async def drop_for_item(conn: AsyncConnection, item_id: str, keep_sheets: set[str] | None = None) -> None:
    rows = await db.fetch_all("select id, sheet, table_name from datasets where item_id = %s", (item_id,), conn)
    for r in rows:
        if keep_sheets is not None and r["sheet"] in keep_sheets:
            continue
        await conn.execute(sql.SQL("drop table if exists {}").format(sql.Identifier("data", r["table_name"])))
        await conn.execute("delete from chunks where source_type = 'dataset' and source_id = %s", (r["id"],))
        await conn.execute("delete from datasets where id = %s", (r["id"],))


# The `query` tool ---------------------------------------------------------------------
#
# Claude's SQL never runs in Nebula's own database. Each query gets a fresh in-memory DuckDB that holds
# only the tables it names that the person may open, with file and network access switched off and
# the settings locked, so there's nothing else for it to reach.

QUERY_SECONDS = 15
DUCK_TYPES = {"integer": "BIGINT", "numeric": "DOUBLE", "date": "DATE", "timestamp": "TIMESTAMP",
              "boolean": "BOOLEAN", "text": "VARCHAR"}
_TABLE_REF = re.compile(r'\bdata\s*\.\s*"?(t_[a-z0-9_]+)"?', re.I)


class QueryError(Exception):
    pass


async def run_query(person: access.Person, query: str, max_rows: int = 200) -> dict:
    q = query.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", q, re.I):
        raise QueryError("Send one SELECT query (it can start with WITH).")
    max_rows = max(1, min(max_rows, 2000))
    named = {m.lower() for m in _TABLE_REF.findall(q)}
    if not named:
        raise QueryError("Name the dataset's table, e.g. data.t_…. Find datasets with `search` first.")
    visible, params = access.item_filter(person, "d.item_id")
    rows = await db.fetch_all(
        f"""select d.table_name, d.columns from datasets d
            where d.table_name = any(%(named)s) and {visible}""",  # noqa: S608 - fixed fragment
        {**params, "named": sorted(named)})
    allowed = {r["table_name"]: r["columns"] for r in rows}
    missing = sorted(named - set(allowed))
    if missing:
        raise QueryError(f"You can't query data.{missing[0]}. Find datasets with `search` first.")
    with tempfile.TemporaryDirectory(prefix="nebula-query-") as tmp:
        files = {}
        for t in allowed:
            path = Path(tmp) / f"{t}.csv"
            await _export_csv(t, path)
            files[t] = path
        return await asyncio.to_thread(_run_in_duckdb, q, allowed, files, max_rows)


async def _export_csv(table: str, path: Path) -> None:
    async with db.tx() as conn:
        async with conn.cursor() as cur:
            copy_sql = sql.SQL("copy {} to stdout with (format csv)").format(sql.Identifier("data", table))
            with path.open("wb") as f:
                async with cur.copy(copy_sql) as copy:
                    async for chunk in copy:
                        f.write(chunk)


def _run_in_duckdb(q: str, tables: dict[str, list], files: dict[str, Path], max_rows: int) -> dict:
    import duckdb

    con = duckdb.connect(":memory:", config={"autoinstall_known_extensions": False,
                                             "autoload_known_extensions": False,
                                             "memory_limit": "1GB", "threads": 2})
    try:
        con.execute("create schema data")
        for t, columns in tables.items():
            types = {c["name"]: DUCK_TYPES.get(c["type"], "VARCHAR") for c in columns}
            con.execute(f'create table data."{t}" as select * from read_csv(?, header=false, '
                        "allow_quoted_nulls=false, columns=?)", [str(files[t]), types])
        con.execute("set enable_external_access = false")
        con.execute("set lock_configuration = true")
        timer = threading.Timer(QUERY_SECONDS, con.interrupt)
        timer.start()
        try:
            cur = con.execute(f"select * from ({q}) as nebula_q limit {max_rows + 1}")
            names = [d[0] for d in cur.description or []]
            rows = cur.fetchall()
        except duckdb.InterruptException as e:
            raise QueryError(f"The query took longer than {QUERY_SECONDS} seconds. Ask for less.") from e
        except duckdb.Error as e:
            raise QueryError(f"SQL error: {e}") from e
        finally:
            timer.cancel()
    finally:
        con.close()
    truncated = len(rows) > max_rows
    return {"columns": names, "rows": [[_jsonable(v) for v in r] for r in rows[:max_rows]], "truncated": truncated}


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v
