"""keyword research tables

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_keywords",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("keyword", sa.String(length=255), nullable=False),
        sa.Column("intent", sa.String(length=20), nullable=True),
        sa.Column("funnel", sa.String(length=20), nullable=True),
        sa.Column("match_type", sa.String(length=10), nullable=True),
        sa.Column("relevance_score", sa.Integer(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="ai"),
        sa.Column("avg_monthly_searches", sa.BigInteger(), nullable=True),
        sa.Column("competition", sa.String(length=10), nullable=True),
        sa.Column("low_bid_micros", sa.BigInteger(), nullable=True),
        sa.Column("high_bid_micros", sa.BigInteger(), nullable=True),
        sa.Column("sc_clicks", sa.Integer(), nullable=True),
        sa.Column("sc_impressions", sa.Integer(), nullable=True),
        sa.Column("sc_ctr", sa.Float(), nullable=True),
        sa.Column("sc_position", sa.Float(), nullable=True),
        sa.Column(
            "bucket", sa.String(length=15), server_default="unsorted", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["shopify_products.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "keyword", name="uq_product_keyword"),
    )
    op.create_index(
        "ix_product_keywords_product_id", "product_keywords", ["product_id"]
    )
    op.create_index("ix_product_keywords_bucket", "product_keywords", ["bucket"])

    op.create_table(
        "keyword_research_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("sources_used", sa.String(length=255), nullable=True),
        sa.Column("keywords_added", sa.Integer(), nullable=True),
        sa.Column("keywords_total", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("started_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["product_id"], ["shopify_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["started_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_keyword_research_runs_product_id",
        "keyword_research_runs",
        ["product_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_keyword_research_runs_product_id", table_name="keyword_research_runs"
    )
    op.drop_table("keyword_research_runs")
    op.drop_index("ix_product_keywords_bucket", table_name="product_keywords")
    op.drop_index("ix_product_keywords_product_id", table_name="product_keywords")
    op.drop_table("product_keywords")
