"""campaigns + per-campaign keywords

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ad_campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("google_ads_campaign_id", sa.BigInteger(), nullable=True),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column("collection_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="draft", nullable=False
        ),
        sa.Column(
            "daily_budget_cents", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "bid_strategy",
            sa.String(length=40),
            server_default="maximize_conversions",
            nullable=False,
        ),
        sa.Column("target_cpa_cents", sa.Integer(), nullable=True),
        sa.Column("landing_page_url", sa.Text(), nullable=True),
        sa.Column("headlines_json", sa.Text(), nullable=True),
        sa.Column("descriptions_json", sa.Text(), nullable=True),
        sa.Column(
            "ai_managed", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("ai_target_cpa_cents", sa.Integer(), nullable=True),
        sa.Column("ai_max_daily_budget_cents", sa.Integer(), nullable=True),
        sa.Column("ai_min_daily_budget_cents", sa.Integer(), nullable=True),
        sa.Column(
            "ai_min_data_clicks", sa.Integer(), server_default="20", nullable=False
        ),
        sa.Column("ai_actions_allowed_json", sa.Text(), nullable=True),
        sa.Column("ai_paused_until", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["product_id"], ["shopify_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["shopify_collections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("google_ads_campaign_id"),
    )
    op.create_index("ix_ad_campaigns_status", "ad_campaigns", ["status"])
    op.create_index("ix_ad_campaigns_product_id", "ad_campaigns", ["product_id"])
    op.create_index("ix_ad_campaigns_collection_id", "ad_campaigns", ["collection_id"])

    op.create_table(
        "ad_campaign_keywords",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("google_ads_keyword_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.String(length=255), nullable=False),
        sa.Column(
            "match_type", sa.String(length=10), server_default="phrase", nullable=False
        ),
        sa.Column(
            "is_negative", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("cpc_bid_cents", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default="enabled", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["ad_campaigns.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ad_campaign_keywords_campaign_id",
        "ad_campaign_keywords",
        ["campaign_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ad_campaign_keywords_campaign_id", table_name="ad_campaign_keywords"
    )
    op.drop_table("ad_campaign_keywords")
    op.drop_index("ix_ad_campaigns_collection_id", table_name="ad_campaigns")
    op.drop_index("ix_ad_campaigns_product_id", table_name="ad_campaigns")
    op.drop_index("ix_ad_campaigns_status", table_name="ad_campaigns")
    op.drop_table("ad_campaigns")
