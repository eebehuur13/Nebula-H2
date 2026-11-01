"""The Nebula web pages: Google sign-in, and a plain list of the files someone can open.

People mostly use Nebula through Claude. These pages are for looking around. Google sign-in is
kept in a signed cookie, shared with /me and /admin. Every page checks access with the same rules
as the MCP tools.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
import uuid
from urllib.parse import quote, urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from nebula import access, activity
from nebula.config import settings
from nebula.knowledge import inventory, items, render
from nebula.server import mcp

log = logging.getLogger(__name__)

COOKIE = "nebula_session"
STATE_COOKIE = "nebula_login_state"
SESSION_DAYS = 14


# Sessions -------------------------------------------------------------------------------------


def _sign(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(settings().jwt_signing_key.encode(), ("web:" + raw).encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{mac}"


def _unsign(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    raw, mac = token.rsplit(".", 1)
    good = hmac.new(settings().jwt_signing_key.encode(), ("web:" + raw).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, good):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return data if isinstance(data, dict) and float(data.get("exp", 0)) > time.time() else None
    except (ValueError, TypeError):
        return None


async def viewer(request: Request) -> access.Person | None:
    """Who's looking at a page. Pages poll every few seconds, so this only reads: the person row is
    written when they sign in (or, the first time, here)."""
    s = settings()
    if s.auth_mode == "dev":
        email, name = s.dev_user_email, "Dev User"
    else:
        sess = _unsign(request.cookies.get(COOKIE))
        if not sess or not s.email_allowed(sess.get("email", "")):
            return None
        email, name = sess["email"], sess.get("name", "")
    known = await access.get_person(email)
    if known is not None:
        return known
    try:
        return await access.ensure_person(email, name)
    except access.AccessDenied:  # deactivated
        return None


def _login_redirect(request: Request) -> Response:
    return RedirectResponse("/login?next=" + quote(request.url.path), status_code=302)


@mcp.custom_route("/login", methods=["GET"], include_in_schema=False)
async def login(request: Request) -> Response:
    s = settings()
    nxt = request.query_params.get("next", "/me")
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or any(ord(c) < 32 for c in nxt):
        nxt = "/me"
    state = _sign({"next": nxt, "n": secrets.token_urlsafe(8), "exp": time.time() + 600})
    params = {
        "client_id": s.google_client_id, "redirect_uri": f"{s.public_url}/web/callback", "response_type": "code",
        "scope": "openid email profile", "state": state, "prompt": "select_account",
    }
    if len(s.allowed_domains) == 1 and not s.allowed_email_set and not s.public_signup:
        params["hd"] = next(iter(s.allowed_domains))
    resp = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), status_code=302)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                    secure=s.public_url.startswith("https"), samesite="lax", path="/web/callback")
    return resp


@mcp.custom_route("/web/callback", methods=["GET"], include_in_schema=False)
async def callback(request: Request) -> Response:
    s = settings()
    raw_state = request.query_params.get("state", "")
    cookie_state = request.cookies.get(STATE_COOKIE, "")
    state = _unsign(raw_state) if cookie_state and hmac.compare_digest(raw_state, cookie_state) else None
    code = request.query_params.get("code")
    if not state or not code:
        return _page("Sign-in failed", "<p>That sign-in link expired. <a href='/login'>Try again</a>.</p>")
    async with httpx.AsyncClient(timeout=10) as client:
        tok = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "redirect_uri": f"{s.public_url}/web/callback", "grant_type": "authorization_code",
        })
        if tok.status_code != 200:
            return _page("Sign-in failed", "<p>Google didn't accept the sign-in. <a href='/login'>Try again</a>.</p>")
        info = await client.get("https://openidconnect.googleapis.com/v1/userinfo",
                                headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
    user = info.json()
    email = (user.get("email") or "").lower()
    if not user.get("email_verified") or not s.email_allowed(email):
        return _page("Not allowed", "<p>This account cannot sign in to Nebula. Contact the administrator.</p>")
    try:
        person = await access.ensure_person(email, user.get("name", ""), user.get("sub"))
    except access.AccessDenied:
        return _page("Not allowed", "<p>This Nebula account is deactivated. Contact the administrator.</p>")
    try:  # for the admin page's activity feed; never let it stop a sign-in
        await activity.audit(None, person, "web_sign_in", "person", person.id, {"to": state["next"]})
    except Exception:
        log.warning("could not record a web sign-in", exc_info=True)
    resp = RedirectResponse(state["next"], status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/web/callback")
    resp.set_cookie(COOKIE, _sign({"email": email, "name": user.get("name", ""), "exp": time.time() + SESSION_DAYS * 86400}),
                    max_age=SESSION_DAYS * 86400, httponly=True, secure=s.public_url.startswith("https"), samesite="lax")
    return resp


@mcp.custom_route("/logout", methods=["GET"], include_in_schema=False)
async def logout(request: Request) -> Response:
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp


# Layout ---------------------------------------------------------------------------------------

_HEAD = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{title} · Nebula</title>
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel=stylesheet>
<style>
:root{color-scheme:light;--bg:#f7f8fa;--panel:#ffffff;--fg:#101828;--muted:#667085;--line:#e4e7ec;--accent:#1c5cab;
}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 "Inter",system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
header{border-bottom:1px solid var(--line);background:var(--panel)}
.bar{max-width:980px;margin:0 auto;padding:12px 16px;display:flex;gap:18px;align-items:center;flex-wrap:wrap;font-size:14px}
.bar a{color:var(--muted);text-decoration:none;font-weight:500}.bar a:hover{color:var(--fg)}
.bar .brand{color:var(--fg);font-weight:650}.bar .sp{flex:1}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.7rem;line-height:1.25;margin:0 0 6px}h2{font-size:1.15rem;margin:28px 0 8px}
a{color:var(--accent)}.meta{color:var(--muted);font-size:.92rem}
ul.plain{list-style:none;padding:0;margin:0}ul.plain li{padding:6px 0;border-bottom:1px solid var(--line)}
.body{overflow-wrap:anywhere}.body table{border-collapse:collapse;display:block;overflow-x:auto}
.body td,.body th{border:1px solid var(--line);padding:4px 8px}.body pre{overflow-x:auto;background:var(--panel);padding:12px}
</style>
<header><div class=bar><a class=brand href="/">Nebula</a><a href="/me">My files</a><a href="/web">All files</a>
<span class=sp></span>{who}</div></header><main>"""


def _page(title: str, body: str, person: access.Person | None = None, scripts: str = "") -> HTMLResponse:
    who = (f"<span class=meta>{html.escape(person.email)}</span> <a href='/logout'>Sign out</a>" if person
           else "<a href='/login'>Sign in</a>")
    head = _HEAD.replace("{title}", html.escape(title)).replace("{who}", who)
    links = settings().footer_links()
    footer = f"<p class=meta>{links}</p>" if links else ""
    return HTMLResponse(head + body + footer + "</main>" + scripts + "</html>")


_MD_SCRIPTS = """<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<script>
document.querySelectorAll('[data-md]').forEach(el=>{
  el.innerHTML=DOMPurify.sanitize(marked.parse(el.textContent));el.hidden=false;});
</script>"""


# Pages ----------------------------------------------------------------------------------------


@mcp.custom_route("/web", methods=["GET"], include_in_schema=False)
async def web_home(request: Request) -> Response:
    """Every file the person can open, by folder, a page at a time."""
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    q = request.query_params.get("q", "").strip()
    res = await inventory.list_documents(person, title=q or None, page=page, page_size=100)
    rows = "".join(
        f"<li><a href='/item/{d['id'].split(':', 1)[1]}'>{html.escape(d['title'] or 'Untitled')}</a> "
        f"<span class=meta>{html.escape(' · '.join(x for x in (d['format'], d['folder'] or d['source'], d['state']) if x))}"
        "</span></li>" for d in res["documents"])
    if not rows:
        rows = f"<li class=meta>{html.escape(res.get('note') or 'Nothing here yet.')}</li>"
    nav = []
    if page > 1:
        nav.append(f"<a href='/web?page={page - 1}&q={quote(q)}'>Previous</a>")
    if res.get("next_page"):
        nav.append(f"<a href='/web?page={page + 1}&q={quote(q)}'>Next</a>")
    form = (f"<form method=get action='/web'><input name=q value='{html.escape(q)}' placeholder='Title contains…' "
            "aria-label='Filter by title'> <button type=submit>Filter</button></form>")
    body = (f"<h1>All files</h1><p class=meta>{res['total']:,} files you can open in Nebula. To follow the files you "
            f"shared, open <a href='/me'>My files</a>.</p>{form}<ul class=plain>{rows}</ul>"
            + (f"<p>{' · '.join(nav)}</p>" if nav else ""))
    return _page("All files", body, person)


@mcp.custom_route("/item/{iid}", methods=["GET"], include_in_schema=False)
async def item_view(request: Request) -> Response:
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    iid = request.path_params["iid"]
    try:
        uuid.UUID(iid)
        await access.require(person, "item", iid)
    except (access.AccessDenied, ValueError):
        return _page("Not found", "<h1>Not found</h1>", person)
    it = await items.get(iid)
    if it is None:
        return _page("Not found", "<h1>Not found</h1>", person)
    doc = items.document_of(it)
    text = render.document_markdown(doc)[0][:200_000] if doc else "(no readable text)"
    src = f" · <a href='{html.escape(it['url'])}' rel=noopener>open in Drive</a>" if it["url"] else ""
    path = " / ".join(it["path"] or [])
    body = (f"<h1>{html.escape(it['title'])}</h1><div class=meta>{html.escape(it['kind'])}"
            f"{' · ' + html.escape(path) if path else ''}{src}</div>"
            f"<div class=body data-md hidden>{html.escape(text)}</div>")
    return _page(it["title"], body, person, _MD_SCRIPTS)
