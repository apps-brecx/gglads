from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from gglads.models.base import Base


class Integration(Base):
    __tablename__ = "integrations"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    config_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Helena Integrations page fields -------------------------------
    # Connection lifecycle status shown on the card. Defaults keep the
    # pre-existing credential-form integrations working unchanged.
    # 'not_connected' | 'connected' | 'reconnect_required'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="not_connected"
    )
    # Access mode gate enforced by the provider factory.
    # 'read_only' | 'read_write'
    access_mode: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="read_only"
    )
    # How this integration authenticates / executes.
    # 'oauth' | 'browser_agent' | 'api_key'
    auth_type: Mapped[str] = mapped_column(
        String(15), nullable=False, server_default="api_key"
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class IntegrationAccount(Base):
    """One linked account/property under an Integration. Supports multiple per
    platform (e.g. several Google Analytics properties, or @handle_one and
    @handle_two on Instagram)."""

    __tablename__ = "integration_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_name: Mapped[str] = mapped_column(
        ForeignKey("integrations.name", ondelete="CASCADE"), nullable=False, index=True
    )
    # External account id / property id.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Display handle/name shown as a chip, e.g. "@syruvia_official".
    handle: Mapped[str] = mapped_column(String(255), nullable=False)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'connected' | 'reconnect_required'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="connected"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
