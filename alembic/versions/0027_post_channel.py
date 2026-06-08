"""Add channel to helena_posts for the multi-channel content calendar.

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-05 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "helena_posts",
        sa.Column("channel", sa.String(length=20), server_default="instagram", nullable=False),
    )
    op.create_index("ix_helena_posts_channel", "helena_posts", ["channel"])


def downgrade() -> None:
    op.drop_index("ix_helena_posts_channel", table_name="helena_posts")
    op.drop_column("helena_posts", "channel")
