"""users.preferences

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("preferences", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "preferences")
