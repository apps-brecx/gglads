"""ad_groups: stale-copy tracking, pending copy approval, prev-ad pause queue;
ad_campaigns: budget id, last-pushed timestamp; ad_campaign_keywords: gads sync

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ad_groups",
        sa.Column("keywords_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ad_groups",
        sa.Column("ad_copy_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ad_groups",
        sa.Column(
            "ad_copy_version", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "ad_groups",
        sa.Column("ad_copy_pending_headlines_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "ad_groups",
        sa.Column("ad_copy_pending_descriptions_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "ad_groups",
        sa.Column("ad_copy_pending_path1", sa.String(length=15), nullable=True),
    )
    op.add_column(
        "ad_groups",
        sa.Column("ad_copy_pending_path2", sa.String(length=15), nullable=True),
    )
    op.add_column(
        "ad_groups",
        sa.Column(
            "ad_copy_pending_generated_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "ad_groups",
        sa.Column("ad_copy_pending_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "ad_groups",
        sa.Column("google_ads_ad_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "ad_groups",
        sa.Column("google_ads_prev_ad_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "ad_groups",
        sa.Column(
            "google_ads_prev_ad_pause_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    # Initialize ad_copy_generated_at from updated_at so existing copy isn't
    # immediately flagged stale.
    op.execute(
        "UPDATE ad_groups SET ad_copy_generated_at = updated_at "
        "WHERE headlines_json IS NOT NULL"
    )

    op.add_column(
        "ad_campaigns",
        sa.Column("google_ads_budget_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "ad_campaigns",
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ad_campaigns",
        sa.Column("last_push_error", sa.Text(), nullable=True),
    )

    op.add_column(
        "ad_campaign_keywords",
        sa.Column("google_ads_resource_name", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ad_campaign_keywords", "google_ads_resource_name")

    op.drop_column("ad_campaigns", "last_push_error")
    op.drop_column("ad_campaigns", "last_pushed_at")
    op.drop_column("ad_campaigns", "google_ads_budget_id")

    op.drop_column("ad_groups", "google_ads_prev_ad_pause_at")
    op.drop_column("ad_groups", "google_ads_prev_ad_id")
    op.drop_column("ad_groups", "google_ads_ad_id")
    op.drop_column("ad_groups", "ad_copy_pending_reason")
    op.drop_column("ad_groups", "ad_copy_pending_generated_at")
    op.drop_column("ad_groups", "ad_copy_pending_path2")
    op.drop_column("ad_groups", "ad_copy_pending_path1")
    op.drop_column("ad_groups", "ad_copy_pending_descriptions_json")
    op.drop_column("ad_groups", "ad_copy_pending_headlines_json")
    op.drop_column("ad_groups", "ad_copy_version")
    op.drop_column("ad_groups", "ad_copy_generated_at")
    op.drop_column("ad_groups", "keywords_changed_at")
