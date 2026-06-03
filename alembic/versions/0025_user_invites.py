"""users: invite token + role-management columns

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-03 00:00:00.000000

invite_token: when set, this is a fresh invite waiting to be accepted.
  Cleared when the user submits the accept-invite form (sets password).
invite_token_expires_at: short-lived (~7 days). Cron / on-demand cleanup
  can purge stale invites.
invited_by_user_id: who sent the invite — surfaced on the users list.
"""
from alembic import op
import sqlalchemy as sa


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("invite_token", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("invite_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "invited_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_users_invite_token", "users", ["invite_token"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_users_invite_token", table_name="users")
    op.drop_column("users", "invited_by_user_id")
    op.drop_column("users", "invite_token_expires_at")
    op.drop_column("users", "invite_token")
