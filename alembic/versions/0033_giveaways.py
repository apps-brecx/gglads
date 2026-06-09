"""Instagram giveaways: giveaways, entries, and reference samples.

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-08 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "helena_giveaways",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("flavor", sa.String(length=120), nullable=True),
        sa.Column("variant", sa.String(length=12), nullable=True),
        sa.Column("product_handle", sa.String(length=255), nullable=True),
        sa.Column("rules_text", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("post_id", sa.Integer(),
                  sa.ForeignKey("helena_posts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("media_external_id", sa.String(length=64), nullable=True),
        sa.Column("permalink", sa.Text(), nullable=True),
        sa.Column("recurrence", sa.String(length=12), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entries_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("winner_username", sa.String(length=120), nullable=True),
        sa.Column("drawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("created_by_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_giveaways_status", "helena_giveaways", ["status"])
    op.create_table(
        "helena_giveaway_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("giveaway_id", sa.Integer(),
                  sa.ForeignKey("helena_giveaways.id", ondelete="CASCADE"), nullable=False),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("tagged", sa.String(length=120), nullable=True),
        sa.Column("source", sa.String(length=10), server_default="tag", nullable=False),
        sa.Column("comment_id", sa.String(length=64), nullable=True),
        sa.Column("eligible", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_giveaway_entries_giveaway_id",
                    "helena_giveaway_entries", ["giveaway_id"])
    op.create_index("ix_helena_giveaway_entries_comment_id",
                    "helena_giveaway_entries", ["comment_id"])
    op.create_table(
        "helena_giveaway_samples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("helena_giveaway_samples")
    op.drop_index("ix_helena_giveaway_entries_comment_id",
                  table_name="helena_giveaway_entries")
    op.drop_index("ix_helena_giveaway_entries_giveaway_id",
                  table_name="helena_giveaway_entries")
    op.drop_table("helena_giveaway_entries")
    op.drop_index("ix_helena_giveaways_status", table_name="helena_giveaways")
    op.drop_table("helena_giveaways")
