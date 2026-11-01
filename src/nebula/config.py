"""Settings, read from NEBULA_* environment variables.

Everything that differs between a laptop and AWS lives here. Secrets arrive as
environment variables too; on AWS, ECS injects them from Secrets Manager.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEV_SIGNING_KEY = "dev-only-signing-key-change-me"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEBULA_", env_file=".env", extra="ignore")

    # Core
    env: Literal["dev", "prod"] = "dev"
    database_url: str = "postgresql://nebula:nebula@localhost:5433/nebula"
    # On AWS the database secret is injected as separate fields; if db_host is set
    # these win over database_url.
    db_host: str = ""
    db_port: int = 5432
    db_user: str = ""
    db_password: str = ""
    db_name: str = "nebula"
    # Connections each process keeps: Nebula's own pool, plus a small one for sign-in data (FastMCP's OAuth
    # store). Set db_pooler when connecting through PgBouncer or RDS Proxy in transaction mode: it turns off
    # the prepared statements those poolers can't carry between connections.
    db_pool_size: int = Field(default=10, ge=1)
    db_signin_pool_size: int = Field(default=2, ge=1)
    db_pooler: bool = False
    public_url: str = "http://localhost:8000"
    aws_region: str = "us-east-2"

    # Sign-in. "dev" skips OAuth and trusts NEBULA_DEV_USER_EMAIL (local only).
    auth_mode: Literal["google", "dev"] = "dev"
    dev_user_email: str = "dev@example.org"
    google_client_id: str = ""
    google_client_secret: str = ""
    jwt_signing_key: str = DEV_SIGNING_KEY
    # Public sign-in is independent of RL membership and content permissions.
    public_signup: bool = False
    # Accounts in these domains are members of the organisation. Set your own domain.
    allowed_email_domains: str = "example.org"
    allowed_emails: str = ""
    admin_emails: str = ""

    # Links shown in the footer of the web pages; left out when empty.
    about_url: str = ""
    privacy_url: str = ""
    terms_url: str = ""

    # Models
    embed_provider: Literal["bedrock-cohere", "bedrock-titan", "local", "fake"] = "local"
    embed_dim: int = 1024
    local_embed_model: str = "intfloat/multilingual-e5-large"
    rerank_provider: Literal["bedrock-cohere", "local", "none"] = "none"
    local_rerank_model: str = "BAAI/bge-reranker-v2-m3-int8"
    # Bound each process's ONNX thread pool when several API workers share one VM.
    local_model_threads: int | None = Field(default=None, ge=1)

    # Nebula's Google identity for reading Drive: workload federation config or a service-account key
    google_service_account_json: str = ""

    # Limits that keep tool results inside Claude's limits (~150k chars per result)
    max_result_chars: int = 60_000
    search_default_limit: int = 8
    search_rerank_candidates: int = Field(default=30, ge=1, le=40)
    search_rerank_chars: int = Field(default=2000, ge=256, le=2000)

    # Requests per person per minute, independent of the number of API replicas.
    tool_calls_per_minute: int = Field(default=120, ge=1)
    # How many days the daily clean-up keeps finished jobs, and activity (requests, search traces, sign-ins,
    # indexing events) for the admin page.
    jobs_keep_days: int = Field(default=14, ge=1)
    activity_keep_days: int = Field(default=90, ge=1)

    worker_poll_seconds: float = 2.0

    @field_validator("google_client_id", "google_client_secret", "google_service_account_json", mode="before")
    @classmethod
    def _unset_is_empty(cls, v: object) -> object:
        # The AWS secret starts with "unset" placeholders until someone fills them in.
        return "" if isinstance(v, str) and v.strip().lower() in ("unset", "none", "-") else v

    @property
    def dsn(self) -> str:
        if self.db_host:
            from urllib.parse import quote

            return (
                f"postgresql://{quote(self.db_user)}:{quote(self.db_password)}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return self.database_url

    @property
    def allowed_domains(self) -> set[str]:
        return {d.strip().lower() for d in self.allowed_email_domains.split(",") if d.strip()}

    @property
    def allowed_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()}

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    def footer_links(self) -> str:
        """HTML links to the about, privacy and terms pages that are configured."""
        import html

        pairs = [("About", self.about_url), ("Privacy", self.privacy_url), ("Terms", self.terms_url)]
        return " · ".join(f"<a href='{html.escape(url)}'>{label}</a>" for label, url in pairs if url)

    def is_member(self, email: str) -> bool:
        """Someone at RL: an account in one of RL's domains, or an email the settings name. Only they can
        bring files into Nebula; public sign-in doesn't change that."""
        email = (email or "").strip().lower()
        if email in self.allowed_email_set or email in self.admin_email_set:
            return True
        return "@" in email and email.rsplit("@", 1)[-1] in self.allowed_domains

    def startup_problems(self) -> list[str]:
        """Settings that would make a public Nebula unsafe. The web app refuses to start with any of them."""
        problems = []
        local = (urlsplit(self.public_url).hostname or "").lower() in LOCAL_HOSTS
        if self.auth_mode == "dev" and (self.env == "prod" or not local):
            problems.append("NEBULA_AUTH_MODE=dev signs every visitor in as one user, so it's only allowed when "
                            "NEBULA_PUBLIC_URL is localhost and NEBULA_ENV is dev.")
        if self.auth_mode == "google" and (self.jwt_signing_key == DEV_SIGNING_KEY or len(self.jwt_signing_key) < 32):
            problems.append("NEBULA_JWT_SIGNING_KEY must be a random secret of at least 32 characters. It signs "
                            "every sign-in, so the built-in placeholder would let anyone sign in as anyone.")
        return problems

    def email_allowed(self, email: str) -> bool:
        email = email.strip().lower()
        if self.public_signup:
            return bool(email.count("@") == 1 and all(email.split("@")))
        if email in self.allowed_email_set or email in self.admin_email_set:
            return True
        return email.rsplit("@", 1)[-1] in self.allowed_domains


@lru_cache
def settings() -> Settings:
    return Settings()


# Re-exported for type hints elsewhere
SettingsT = Settings
__all__ = ["Settings", "settings", "Field"]
