"""make product_chat_messages.product_id nullable so we can have global ("all products") chats

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "product_chat_messages",
        "product_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "product_chat_messages",
        "product_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
