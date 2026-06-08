"""Attach an optional image to chat messages (paste / upload in chat).

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-08 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("helena_messages", sa.Column("image_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("helena_messages", "image_url")
