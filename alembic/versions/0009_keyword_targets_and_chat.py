"""keyword seo_targets + product chat messages

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_keywords",
        sa.Column("seo_targets", sa.Text(), nullable=True),
    )

    op.create_table(
        "product_chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "topic", sa.String(length=20), server_default="seo", nullable=False
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["product_id"], ["shopify_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_product_chat_messages_product_id",
        "product_chat_messages",
        ["product_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_chat_messages_product_id", table_name="product_chat_messages"
    )
    op.drop_table("product_chat_messages")
    op.drop_column("product_keywords", "seo_targets")
