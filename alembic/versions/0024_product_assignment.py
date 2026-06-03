"""shopify_products: product-level worker assignment

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-03 00:00:00.000000

Worker ownership is now product-level, not task-level. Assigning a worker
to a product implicitly makes them responsible for every task type on it.
The entity_tasks table still tracks completion per (product, task_slug) so
we can report 'X of N done'.
"""
from alembic import op
import sqlalchemy as sa


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shopify_products",
        sa.Column(
            "assigned_to_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "shopify_products",
        sa.Column(
            "assigned_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "shopify_products",
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_shopify_products_assigned_to_user",
        "shopify_products",
        ["assigned_to_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shopify_products_assigned_to_user", table_name="shopify_products"
    )
    op.drop_column("shopify_products", "assigned_at")
    op.drop_column("shopify_products", "assigned_by_user_id")
    op.drop_column("shopify_products", "assigned_to_user_id")
