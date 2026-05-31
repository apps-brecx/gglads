"""seo draft verdict column

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_seo_drafts",
        sa.Column("verdict", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("product_seo_drafts", "verdict")
