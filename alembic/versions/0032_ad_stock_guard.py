"""Per-ad out-of-stock guard state (pause OOS ads, alert, auto-resume).

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-08 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "helena_ad_stock_guard",
        sa.Column("ad_id", sa.String(length=64), nullable=False),
        sa.Column("ad_name", sa.String(length=255), nullable=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("product_handle", sa.String(length=255), nullable=True),
        sa.Column("paused_by_guard", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("allow_oos", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oos_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("ad_id"),
    )
    op.create_index("ix_helena_ad_stock_guard_campaign_id",
                    "helena_ad_stock_guard", ["campaign_id"])
    op.create_index("ix_helena_ad_stock_guard_product_handle",
                    "helena_ad_stock_guard", ["product_handle"])


def downgrade() -> None:
    op.drop_index("ix_helena_ad_stock_guard_product_handle",
                  table_name="helena_ad_stock_guard")
    op.drop_index("ix_helena_ad_stock_guard_campaign_id",
                  table_name="helena_ad_stock_guard")
    op.drop_table("helena_ad_stock_guard")
