from functools import lru_cache

from pydantic import field_validator
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

    # ---- Helena module (Instagram/Meta + Email marketing agent) ----------
    # Swappable execution backends. The chat agent and the rest of the app
    # only ever talk to the MetaExecutionProvider / EmailDeliveryProvider
    # interfaces; these flags pick the concrete implementation.
    meta_execution_mode: str = "browser"  # "browser" | "api"
    email_delivery_mode: str = "browser"  # "browser" | "api"

    # Browser-agent connection (Claude-driven Chrome automation). Until we
    # have official push access, every Meta/Instagram/Shopify-Email write and
    # read-back is performed through this agent.
    browser_agent_url: str = ""
    browser_agent_token: str = ""

    # Google Flow (Imagen / Veo) image generation.
    google_flow_api_key: str = ""
    google_flow_project_id: str = ""
    google_flow_base_url: str = "https://aisandbox-pa.googleapis.com"
    google_flow_image_model: str = ""  # preferred image model; auto-discovered if blank
    google_flow_video_model: str = ""  # preferred Veo model; auto-discovered if blank
    google_flow_api_version: str = "v1beta"  # Generative Language API version
    google_flow_video_timeout_seconds: int = 180  # max wait for a Veo render
    # Vertex AI auth (service-account path). Provide the SA key JSON inline.
    google_vertex_location: str = "us-central1"
    google_application_credentials_json: str = ""

    # S3-compatible storage for generated images + email assets.
    s3_endpoint_url: str = ""
    s3_region: str = ""  # falls back to AWS_REGION, then us-east-1 (see storage)
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_public_base_url: str = ""  # CDN / public host for stored objects
    # Conventional AWS names, accepted as fallbacks so either naming works.
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = ""

    # Future MetaApiProvider — left as empty placeholders until we have
    # approved Meta Marketing API / Instagram Graph API access.
    meta_app_id: str = ""
    meta_app_secret: str = ""
    instagram_app_id: str = ""
    instagram_app_secret: str = ""

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
