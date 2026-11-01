"""Assemble the HTTP app: MCP endpoint at /mcp, Google sign-in, routes, startup work."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from nebula import db, models
from nebula.config import settings

log = logging.getLogger(__name__)


def build_app():
    from nebula import admin, me, server, web, webui  # noqa: F401  (they register routes on the server)

    s = settings()
    problems = s.startup_problems()
    if problems:
        raise RuntimeError("Nebula won't start: " + " ".join(problems))
    if s.auth_mode == "google":
        server.mcp.auth = _google_auth()

    # No Host allowlist: that guard protects servers bound to localhost from DNS rebinding.
    # Nebula sits behind CloudFront (origin secret header), and the load balancer's health
    # checks arrive with the task's IP as Host, which an allowlist would reject.
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        applied = await db.migrate()
        if applied:
            log.info("migrations applied: %s", applied)
        await db.pool()
        await _check_database_user()
        asyncio.get_running_loop().run_in_executor(None, models.warm)
        async with inner_lifespan(a):
            yield
        await db.close()

    app.router.lifespan_context = lifespan
    return app


async def _check_database_user() -> None:
    """Nebula's database user shouldn't be a Postgres superuser: a superuser can read files on the database
    server, so any mistake in Nebula's own SQL would reach further than the database. Create the pgvector
    extension once as a superuser, then run Nebula as an ordinary user that owns its database."""
    row = await db.fetch_one("select rolsuper from pg_roles where rolname = current_user")
    if row and row["rolsuper"]:
        if settings().env == "prod":
            raise RuntimeError("Nebula won't start: its database user is a Postgres superuser. Connect as an "
                               "ordinary user that owns Nebula's database (see README, Run it on a server).")
        log.warning("Nebula's database user is a Postgres superuser; fine on a laptop, not on a server")


def _signin_store():
    """FastMCP's sign-in store, in Nebula's database, with a small connection pool. The library's own
    default is an asyncpg pool of 10 connections in every process, on top of Nebula's pool."""
    from key_value.aio.stores.postgresql import PostgreSQLStore

    class SignInStore(PostgreSQLStore):
        async def _create_pool(self):  # replaces the library's default pool (key_value 0.4)
            import asyncpg

            s = settings()
            return await asyncpg.create_pool(self._url, min_size=1, max_size=s.db_signin_pool_size,
                                             statement_cache_size=0 if s.db_pooler else 100)

    return SignInStore(url=settings().dsn, table_name="oauth_kv")


def _google_auth():
    from fastmcp.server.auth.providers.google import GoogleProvider

    s = settings()
    storage = _signin_store()
    if not s.google_client_id:
        # Until the Google OAuth client is set in the secret, sign-in can't complete, so no one
        # can get a token. The service still starts and /healthz works.
        log.warning("NEBULA_GOOGLE_CLIENT_ID is not set: sign-in will fail until it is configured")
    return GoogleProvider(
        client_id=s.google_client_id or "not-configured.apps.googleusercontent.com",
        client_secret=s.google_client_secret or "not-configured",
        base_url=s.public_url,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        client_storage=storage,
        jwt_signing_key=s.jwt_signing_key,
        cache_ttl_seconds=300,
        require_authorization_consent="remember",  # one click per client app, then remembered
        extra_authorize_params={"access_type": "offline", "prompt": "consent",
                                **({"hd": next(iter(s.allowed_domains))} if len(s.allowed_domains) == 1
                                   and not s.allowed_email_set and not s.public_signup else {})},
        allowed_client_redirect_uris=["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback",
                                      "http://localhost:*", "http://127.0.0.1:*"],
    )
