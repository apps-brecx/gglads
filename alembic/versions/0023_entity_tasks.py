"""entity_tasks — assignment + completion tracking for SEO/ads work per product or collection

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-01 00:00:00.000000

One row per (entity_type, entity_id, task_slug). Each row can be:
  - unassigned + open (default — task exists but nobody's on it)
  - assigned to a user (assigned_to_user_id set)
  - completed (completed_at + completed_by_user_id set)

Workers tick checkboxes on product / collection pages → we upsert a row,
stamping completed_at and the current user. Admin pages aggregate across
this table for activity reports + 'what's still open' filtering.
"""
from alembic import op
import sqlalchemy as sa


revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entity_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        # 'product' or 'collection' — kept as a string so adding more kinds
        # later (variant, image, etc.) doesn't need a migration.
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        # Task slug: 'meta_title' / 'meta_description' / 'description' /
        # 'image_alts' / 'keywords' / 'ad_campaign'. Allowlist enforced in
        # the service layer, not the DB, so we can add tasks without a migration.
        sa.Column("task_slug", sa.String(length=40), nullable=False),
        sa.Column(
            "assigned_to_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assigned_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "completed_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entity_type", "entity_id", "task_slug",
            name="uq_entity_tasks_entity_task",
        ),
    )
    op.create_index(
        "ix_entity_tasks_assigned_user",
        "entity_tasks", ["assigned_to_user_id"],
    )
    op.create_index(
        "ix_entity_tasks_completed_at",
        "entity_tasks", ["completed_at"],
    )
    op.create_index(
        "ix_entity_tasks_entity",
        "entity_tasks", ["entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_entity_tasks_entity", table_name="entity_tasks")
    op.drop_index("ix_entity_tasks_completed_at", table_name="entity_tasks")
    op.drop_index("ix_entity_tasks_assigned_user", table_name="entity_tasks")
    op.drop_table("entity_tasks")
