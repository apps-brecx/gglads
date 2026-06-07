"""Helena persistent memory + product image library.

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-07 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "helena_memory_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=20), server_default="general", nullable=False),
        sa.Column("source", sa.String(length=10), server_default="chat", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "helena_product_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=12), server_default="product", nullable=False),
        sa.Column("flavor", sa.String(length=120), nullable=True),
        sa.Column("variant", sa.String(length=12), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_product_images_flavor", "helena_product_images", ["flavor"])


def downgrade() -> None:
    op.drop_index("ix_helena_product_images_flavor", table_name="helena_product_images")
    op.drop_table("helena_product_images")
    op.drop_table("helena_memory_items")
