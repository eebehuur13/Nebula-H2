"""HTTP routes beside MCP: health and the home page."""

from __future__ import annotations

import html
import logging

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from nebula import db
from nebula.server import mcp

log = logging.getLogger(__name__)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(request: Request) -> Response:
    try:
        await db.fetch_one("select 1 as ok")
        return JSONResponse({"ok": True})
    except Exception:  # pragma: no cover - the details go to the log, not to whoever asked
        log.exception("health check failed")
        return JSONResponse({"ok": False}, status_code=503)


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def home(request: Request) -> Response:
    from pathlib import Path

    from nebula.config import settings
    from nebula.connectors.gdrive import service_account_email

    s = settings()
    base = s.public_url.rstrip("/")
    domains = sorted(s.allowed_domains)
    account = (f"<b>@{html.escape(domains[0])}</b> Google account" if len(domains) == 1 and not s.public_signup
               else "work Google account")
    bot = service_account_email()
    share = (
        "<p>In Google Drive, share a folder or a file with this address, and give it <b>Editor</b> access.</p>"
        f"<div class=copyrow><code id=bot>{html.escape(bot)}</code>"
        "<button class='btn sm' type=button data-copy=bot>Copy</button></div>"
        "<p>Nebula only reads your files and never changes them. It asks for Editor because Drive shows who else "
        "a file is shared with only to editors, and Nebula uses that list to decide who can find the file "
        "through Claude.</p>"
        "<p class=note>Nebula notices a new share within a few minutes. Anyone who can open the file in Drive can "
        "find it through Nebula, and nobody else can.</p>"
    ) if bot else "<p>Drive isn't connected on this server yet, so there's nothing to share with.</p>"
    page = (Path(__file__).parent / "admin_ui" / "home.html").read_text()
    links = s.footer_links()
    footer = "".join(f'<span class="dot-sep"></span>{link}' for link in links.split(" · ")) if links else ""
    for key, value in (("{{MCP_URL}}", html.escape(base + "/mcp")), ("{{ACCOUNT}}", account),
                       ("{{SHARE_BLOCK}}", share), ("{{HOST}}", html.escape(base.split("//")[-1])),
                       ("{{FOOTER_LINKS}}", footer)):
        page = page.replace(key, value)
    return HTMLResponse(page)
