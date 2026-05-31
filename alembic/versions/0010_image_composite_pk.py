"""composite PK on shopify_product_images so the same image can attach to multiple products

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the single-column PK and recreate as (product_id, id) composite.
    # Existing data is safe — any duplicates would have prevented inserts
    # before this migration, so there can't be any.
    op.execute(
        "ALTER TABLE shopify_product_images DROP CONSTRAINT shopify_product_images_pkey"
    )
    op.create_primary_key(
        "shopify_product_images_pkey",
        "shopify_product_images",
        ["product_id", "id"],
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE shopify_product_images DROP CONSTRAINT shopify_product_images_pkey"
    )
    op.create_primary_key(
        "shopify_product_images_pkey",
        "shopify_product_images",
        ["id"],
    )
