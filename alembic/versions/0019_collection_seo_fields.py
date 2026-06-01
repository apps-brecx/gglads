"""shopify_collections: SEO meta + body description (mirror product SEO fields)

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-01 00:00:00.000000

So we can manage collection SEO the same way we manage product SEO:
edit meta_title / meta_description, regenerate from keywords with AI.
The existing `description` column already exists (free-form body HTML).
"""
from alembic import op
import sqlalchemy as sa


revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shopify_collections",
        sa.Column("seo_title", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "shopify_collections",
        sa.Column("seo_meta_description", sa.Text(), nullable=True),
    )
    op.add_column(
        "shopify_collections",
        sa.Column("seo_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shopify_collections", "seo_updated_at")
    op.drop_column("shopify_collections", "seo_meta_description")
    op.drop_column("shopify_collections", "seo_title")
