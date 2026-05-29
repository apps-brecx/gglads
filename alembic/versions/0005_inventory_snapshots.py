"""inventory snapshots

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shopify_inventory_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("inventory", sa.Integer(), nullable=False),
        sa.Column("is_in_stock", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"], ["shopify_products.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id", "snapshot_date", name="uq_inventory_snapshot_product_date"
        ),
    )
    op.create_index(
        "ix_inventory_snapshots_product_id",
        "shopify_inventory_snapshots",
        ["product_id"],
    )
    op.create_index(
        "ix_inventory_snapshots_snapshot_date",
        "shopify_inventory_snapshots",
        ["snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inventory_snapshots_snapshot_date", table_name="shopify_inventory_snapshots"
    )
    op.drop_index(
        "ix_inventory_snapshots_product_id", table_name="shopify_inventory_snapshots"
    )
    op.drop_table("shopify_inventory_snapshots")
