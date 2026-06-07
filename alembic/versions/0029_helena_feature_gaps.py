"""Helena feature gaps: scheduled-task last_run_at + brand knowledge documents.

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-07 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "helena_scheduled_tasks",
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "brand_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_brand_documents_brand_id", "brand_documents", ["brand_id"])


def downgrade() -> None:
    op.drop_index("ix_brand_documents_brand_id", table_name="brand_documents")
    op.drop_table("brand_documents")
    op.drop_column("helena_scheduled_tasks", "last_run_at")
