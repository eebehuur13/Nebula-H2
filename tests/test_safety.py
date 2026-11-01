"""Settings and inputs that used to make Nebula unsafe: dev sign-in, the placeholder signing key, a
superuser database login, admins that couldn't be removed, outsiders' Drive shares, and zip bombs."""

from __future__ import annotations

import io

import pytest

from helpers import MEMBER, call
from nebula import access
from nebula.config import DEV_SIGNING_KEY, Settings, settings


def test_unsafe_settings_stop_the_web_app():
    assert Settings(auth_mode="dev", public_url="http://localhost:8000").startup_problems() == []
    assert Settings(auth_mode="dev", public_url="https://nebula.example.org").startup_problems()
    assert Settings(auth_mode="dev", env="prod", public_url="http://localhost:8000").startup_problems()
    assert Settings(auth_mode="google", jwt_signing_key=DEV_SIGNING_KEY).startup_problems()
    assert Settings(auth_mode="google", jwt_signing_key="short").startup_problems()
    assert Settings(auth_mode="google", jwt_signing_key="k" * 64).startup_problems() == []
    with pytest.raises(RuntimeError, match="won't start"):
        from nebula import app

        settings_before = settings().public_url
        try:
            settings().public_url = "https://nebula.example.org"
            app.build_app()
        finally:
            settings().public_url = settings_before


async def test_a_superuser_database_login_is_refused_in_prod(mcp, monkeypatch):
    from nebula import app, db

    row = await db.fetch_one("select rolsuper from pg_roles where rolname = current_user")
    if not row["rolsuper"]:
        pytest.skip("the test database user isn't a superuser")
    monkeypatch.setattr(settings(), "env", "prod")
    with pytest.raises(RuntimeError, match="superuser"):
        await app._check_database_user()
    monkeypatch.setattr(settings(), "env", "dev")
    await app._check_database_user()  # only a warning on a laptop


async def test_admin_follows_the_setting_and_the_command(mcp, monkeypatch):
    from nebula import db

    who = "temp-admin@example.org"
    monkeypatch.setattr(settings(), "admin_emails", f"admin@example.org,{who}")
    assert (await access.ensure_person(who)).is_admin
    monkeypatch.setattr(settings(), "admin_emails", "admin@example.org")
    assert not (await access.ensure_person(who)).is_admin  # removed from the setting: no longer an admin
    await db.execute("update people set is_admin = true where email = %s", (who,))  # `nebula admin`
    assert (await access.ensure_person(who)).is_admin
    await db.execute("update people set is_admin = false where email = %s", (who,))  # `nebula admin --remove`
    assert not (await access.ensure_person(who)).is_admin


async def test_only_rl_accounts_bring_files_in(mcp, monkeypatch):
    from nebula import sync

    monkeypatch.setattr(settings(), "public_signup", True)
    r = await call(mcp, "stranger@gmail.com", "sources", action="connect",
                   link="https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345")
    assert "error" in r
    assert sync._accepted_share({"kind": "folder", "shared_by": MEMBER})
    assert not sync._accepted_share({"kind": "folder", "shared_by": "someone@gmail.com"})
    assert not sync._accepted_share({"kind": "file", "shared_by": ""})
    assert not sync._accepted_share({"kind": "drive", "shared_by": ""})  # connect Shared Drives by link


def test_zip_files_that_unpack_too_large_are_not_parsed(monkeypatch):
    import importlib

    import docx

    from nebula.ingest import images

    parse = importlib.import_module("nebula.ingest.parse")  # the module; nebula.ingest.parse is also a function

    d = docx.Document()
    d.add_paragraph("A perfectly ordinary letter. " * 200)
    buf = io.BytesIO()
    d.save(buf)
    data = buf.getvalue()
    assert parse.parse(data, filename="letter.docx").blocks
    monkeypatch.setattr(parse, "MAX_UNPACKED_BYTES", 10_000)
    doc = parse.parse(data, filename="letter.docx")
    assert not doc.blocks and "unpacks to more than" in doc.metadata["parse_error"]
    with pytest.raises(images.NoImage):
        images.render_part(data, "letter.docx", "image:1")


async def test_malformed_ids_are_not_found(mcp):
    from starlette.testclient import TestClient

    from nebula import server

    r = await call(mcp, MEMBER, "job", id="not-a-job")
    assert r["error"] == "No such job of yours."
    settings().dev_user_email = MEMBER
    server.forget_person()
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    with TestClient(app) as c:
        r = c.get("/item/not-an-id")
        assert r.status_code == 200 and "Not found" in r.text
