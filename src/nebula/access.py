"""Who someone is, and which files they may open.

Rules, in one place:
- Drive decides. Each file is open to the people Drive shares it with, and to everyone in a
  Workspace domain Nebula serves when Drive shares it with that domain (see `item_access`).
- A spreadsheet table and every search passage follow the file they came from.
- Admins can open everything. An admin is anyone in NEBULA_ADMIN_EMAILS (checked each time, so
  removing someone there takes effect) or anyone made one with `nebula admin` (the people.is_admin flag).
- Nobody else sees a file whose sharing Nebula couldn't read.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection

from nebula import db
from nebula.config import settings


class AccessDenied(Exception):
    pass


@dataclass
class Person:
    id: str
    email: str
    name: str
    is_admin: bool

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower()

    @property
    def is_rl_member(self) -> bool:
        return self.is_admin or self.domain in settings().allowed_domains

    def principals(self) -> list[str]:
        """The `item_access` entries that let this person open a file."""
        out = [f"email:{self.email}"]
        if self.domain in settings().allowed_domains:
            out.append(f"domain:{self.domain}")
        return out


# Identity ------------------------------------------------------------------------


async def ensure_person(email: str, name: str = "", google_sub: str | None = None) -> Person:
    """Create or refresh the person behind a signed-in email."""
    email = email.strip().lower()
    row = await db.fetch_row(
        """
        insert into people (email, name, google_sub, last_seen_at)
        values (%s, %s, %s, now())
        on conflict (email) do update set
            name = case when excluded.name <> '' then excluded.name else people.name end,
            google_sub = coalesce(people.google_sub, excluded.google_sub),
            last_seen_at = now()
        returning id, email, name, is_admin, status
        """,
        (email, name or "", google_sub),
    )
    if row["status"] != "active":
        raise AccessDenied("This Nebula account is deactivated.")
    return _person(row)


async def get_person(email: str) -> Person | None:
    """An active person by email, without writing anything (unlike `ensure_person`)."""
    row = await db.fetch_one("select id, email, name, is_admin, status from people where email = %s",
                             (email.strip().lower(),))
    return _person(row) if row and row["status"] == "active" else None


def _person(row: dict) -> Person:
    return Person(id=str(row["id"]), email=row["email"], name=row["name"], is_admin=is_admin(row))


def is_admin(row: dict) -> bool:
    """A people row's admin status: made admin with `nebula admin`, or named in NEBULA_ADMIN_EMAILS now."""
    return bool(row["is_admin"]) or row["email"] in settings().admin_email_set


# Access checks ---------------------------------------------------------------------


def item_filter(person: Person, item_col: str = "i.id") -> tuple[str, dict]:
    """A SQL condition (and its parameters) that keeps only items the person may open."""
    if person.is_admin:
        return "true", {}
    return (f"exists (select 1 from item_access a where a.item_id = {item_col} "
            "and a.principal = any(%(principals)s))"), {"principals": person.principals()}


async def can_open(person: Person, item_id: str, conn: AsyncConnection | None = None) -> bool:
    if person.is_admin:
        return True
    row = await db.fetch_one("select 1 as ok from item_access where item_id = %s and principal = any(%s)",
                             (item_id, person.principals()), conn)
    return row is not None


async def require(person: Person, kind: str, rid: str, conn: AsyncConnection | None = None) -> str:
    """Raise unless the person may open this item or dataset. Returns the item id behind it."""
    item_id = rid
    if kind == "dataset":
        row = await db.fetch_one("select item_id from datasets where id = %s", (rid,), conn)
        if row is None:
            raise AccessDenied("You don't have permission to see this dataset.")
        item_id = str(row["item_id"])
    elif kind != "item":
        raise ValueError("Nebula ids look like item:… or dataset:….")
    if not await can_open(person, item_id, conn):
        raise AccessDenied(f"You don't have permission to see this {kind}.")
    return item_id


def principals_for(audience: dict) -> list[str]:
    """The `item_access` entries for one file, from who Drive shares it with. Google Groups and
    public links aren't mirrored: Nebula can't see a group's members, and a link isn't a person."""
    out = [f"email:{e.lower()}" for e in audience.get("emails", [])]
    out += [f"domain:{d.lower()}" for d in audience.get("domains", []) if d.lower() in settings().allowed_domains]
    return out
