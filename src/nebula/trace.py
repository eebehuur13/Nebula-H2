"""A notebook for one tool call, so admins can see how a search reached its answer.

The MCP middleware opens one per search; `search()` writes each step into it with its timing. Outside a
tool call (tests, the CLI) nothing is open and every function here does nothing."""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

_current: ContextVar[dict | None] = ContextVar("nebula_trace", default=None)


def open_trace() -> tuple[dict, Any]:
    t: dict[str, Any] = {"steps": []}
    return t, _current.set(t)


def close_trace(token: Any) -> None:
    _current.reset(token)


def active() -> bool:
    return _current.get() is not None


def begin() -> None:
    """Start timing steps from now."""
    t = _current.get()
    if t is not None:
        t["_at"] = time.perf_counter()


def step(name: str) -> None:
    """The step called `name` has just finished: record how long it took since the last one."""
    t = _current.get()
    if t is None:
        return
    now = time.perf_counter()
    t["steps"].append({"step": name, "ms": round((now - t.get("_at", now)) * 1000, 1)})
    t["_at"] = now


def note(**values: Any) -> None:
    t = _current.get()
    if t is not None:
        t.update(values)


def finished(t: dict | None) -> dict | None:
    if not t or not t.get("steps"):
        return None
    return {k: v for k, v in t.items() if not k.startswith("_")}
