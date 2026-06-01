"""shopify_products: oos_since + oos_ignored — out-of-stock tracking

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-01 00:00:00.000000

oos_since: timestamp set when a product transitions in-stock → out-of-stock,
  cleared when it's restocked. Lets us show 'OOS for 5 days' in the UI.
oos_ignored: user-set flag that hides the product from the OOS list. The
  sync clears this whenever the product is in stock, so the next time it
  goes OOS the product naturally reappears in the list.
"""
from alembic import op
import sqlalchemy as sa


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shopify_products",
        sa.Column("oos_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "shopify_products",
        sa.Column(
            "oos_ignored",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Backfill oos_since for anything already OOS at migration time, so the
    # 'OOS for N days' column doesn't show 0 days for stuff that's been out
    # for weeks.
    op.execute(
        "UPDATE shopify_products SET oos_since = NOW() "
        "WHERE total_inventory = 0 AND oos_since IS NULL"
    )
    op.create_index(
        "ix_shopify_products_oos_since", "shopify_products", ["oos_since"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shopify_products_oos_since", table_name="shopify_products"
    )
    op.drop_column("shopify_products", "oos_ignored")
    op.drop_column("shopify_products", "oos_since")
