"""shopify_products: is_ignored flag (excludes from default views + AI ops)

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-01 00:00:00.000000

User-set flag, persistent across syncs. When true, the product is:
  - Hidden from the default /products list (visible in the Ignored view).
  - Skipped by bulk operations: research_all_products, etc.
Catalog sync STILL refreshes its data — so if the user un-ignores it later,
title/inventory/price are up to date.
"""
from alembic import op
import sqlalchemy as sa


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shopify_products",
        sa.Column(
            "is_ignored",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_shopify_products_is_ignored",
        "shopify_products",
        ["is_ignored"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shopify_products_is_ignored", table_name="shopify_products"
    )
    op.drop_column("shopify_products", "is_ignored")
