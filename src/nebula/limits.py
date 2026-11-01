"""Requests per person per minute, counted in Postgres so every API process shares the count."""
from __future__ import annotations

import datetime as dt

from nebula import db
from nebula.config import settings


class LimitExceeded(ValueError):
    pass


async def consume(person_id: str, metric: str, *, amount: int = 1) -> int:
    if metric != "tool_calls":
        raise ValueError("Unknown usage metric")
    if amount < 1:
        raise ValueError("Usage must be positive")
    start = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
    cap = settings().tool_calls_per_minute
    row = await db.fetch_one(
        """insert into usage_counters(person_id,metric,window_start,used) select %s,%s,%s,%s where %s <= %s
           on conflict(person_id,metric,window_start) do update set used=usage_counters.used+excluded.used
           where usage_counters.used+excluded.used <= %s returning used""",
        (person_id, metric, start, amount, amount, cap, cap),
    )
    if not row:
        raise LimitExceeded("Your account's request limit is reached for this minute. Try again shortly.")
    return row["used"]
