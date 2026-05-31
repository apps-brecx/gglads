"""keyword_research_runs.source_errors

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "keyword_research_runs",
        sa.Column("source_errors", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("keyword_research_runs", "source_errors")
