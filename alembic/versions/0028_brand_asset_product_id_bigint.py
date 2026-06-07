"""Widen brand_assets.product_id to BIGINT.

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-07 00:00:00.000000

brand_assets.product_id holds a Shopify product id, which is BIGINT. It was
created as a 32-bit INTEGER, so saving a generated asset tied to a product
failed with NumericValueOutOfRange ("integer out of range").
"""
import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "brand_assets",
        "product_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "brand_assets",
        "product_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
