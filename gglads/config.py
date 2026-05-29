from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "local"
    app_secret: str = "dev-only-change-me"
    log_level: str = "INFO"

    database_url: str = ""

    dry_run: bool = True
    autonomous_mode: bool = False

    worker_interval_seconds: int = 600

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-7"

    shopify_store_domain: str = ""
    shopify_admin_api_token: str = ""
    shopify_api_version: str = "2025-01"

    google_ads_developer_token: str = ""
    google_ads_client_id: str = ""
    google_ads_client_secret: str = ""
    google_ads_refresh_token: str = ""
    google_ads_customer_id: str = ""
    google_ads_login_customer_id: str = ""

    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    @field_validator("database_url")
    @classmethod
    def normalize_db_url(cls, v: str) -> str:
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://") :]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
