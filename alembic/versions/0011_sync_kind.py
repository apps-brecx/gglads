"""shopify_sync_runs.kind column

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shopify_sync_runs",
        sa.Column(
            "kind",
            sa.String(length=20),
            server_default="full",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_shopify_sync_runs_kind", "shopify_sync_runs", ["kind"]
    )


def downgrade() -> None:
    op.drop_index("ix_shopify_sync_runs_kind", table_name="shopify_sync_runs")
    op.drop_column("shopify_sync_runs", "kind")
