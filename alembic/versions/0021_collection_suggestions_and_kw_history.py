"""collection_suggestions table + product_keyword_history table

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-01 00:00:00.000000

collection_suggestions: AI-generated proposals for new Shopify collections to
  create, anchored on organic-search clusters that don't already have one.
  Pending → user can dismiss or mark-as-created (no Shopify mutation in v1;
  user creates the collection in Shopify admin, the next sync picks it up).

product_keyword_history: per-day per-product per-keyword snapshot of Search
  Console position/clicks/impressions/CTR. Powers growth alerts: 'this
  keyword moved from #14 to #6', 'product X gained 5 organic keywords this
  week', etc.
"""
from alembic import op
import sqlalchemy as sa


revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("handle", sa.String(length=255), nullable=False),
        sa.Column("theme_keywords_json", sa.Text(), nullable=False),
        sa.Column("seo_title", sa.String(length=255), nullable=True),
        sa.Column("seo_meta_description", sa.Text(), nullable=True),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "opportunity_score", sa.Integer(), nullable=False, server_default="50"
        ),
        # 'pending' | 'dismissed' | 'created'
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("created_collection_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "generated_at",
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("handle", "status", name="uq_collection_suggestion_handle_status"),
    )
    op.create_index(
        "ix_collection_suggestions_status",
        "collection_suggestions",
        ["status"],
    )

    op.create_table(
        "product_keyword_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column(
            "product_id",
            sa.BigInteger(),
            sa.ForeignKey("shopify_products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("keyword", sa.String(length=255), nullable=False),
        sa.Column("sc_position", sa.Float(), nullable=True),
        sa.Column("sc_clicks", sa.Integer(), nullable=True),
        sa.Column("sc_impressions", sa.Integer(), nullable=True),
        sa.Column("sc_ctr", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date", "product_id", "keyword",
            name="uq_pkh_date_product_keyword",
        ),
    )
    op.create_index(
        "ix_pkh_product_id_date",
        "product_keyword_history",
        ["product_id", "snapshot_date"],
    )
    op.create_index(
        "ix_pkh_snapshot_date", "product_keyword_history", ["snapshot_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_pkh_snapshot_date", table_name="product_keyword_history")
    op.drop_index("ix_pkh_product_id_date", table_name="product_keyword_history")
    op.drop_table("product_keyword_history")
    op.drop_index(
        "ix_collection_suggestions_status", table_name="collection_suggestions"
    )
    op.drop_table("collection_suggestions")
