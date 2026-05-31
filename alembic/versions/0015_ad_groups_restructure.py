"""ad_groups: split campaign into match-type ad groups; move ad copy there

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ad_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("google_ads_ad_group_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("match_type", sa.String(length=10), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="enabled", nullable=False
        ),
        sa.Column("headlines_json", sa.Text(), nullable=True),
        sa.Column("descriptions_json", sa.Text(), nullable=True),
        sa.Column("path1", sa.String(length=15), nullable=True),
        sa.Column("path2", sa.String(length=15), nullable=True),
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
            ["campaign_id"], ["ad_campaigns.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("google_ads_ad_group_id"),
    )
    op.create_index("ix_ad_groups_campaign_id", "ad_groups", ["campaign_id"])

    # Add ad_group_id to ad_campaign_keywords; nullable for the data-migration step
    op.add_column(
        "ad_campaign_keywords",
        sa.Column("ad_group_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ad_campaign_keywords_ad_group_id",
        "ad_campaign_keywords",
        "ad_groups",
        ["ad_group_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_ad_campaign_keywords_ad_group_id",
        "ad_campaign_keywords",
        ["ad_group_id"],
    )

    # Data migration: for each existing campaign, create a default ad group
    # that inherits the campaign's headlines/descriptions, then attach all
    # keywords to it.
    conn = op.get_bind()
    campaigns = conn.execute(
        sa.text(
            "SELECT id, name, headlines_json, descriptions_json FROM ad_campaigns"
        )
    ).fetchall()
    for camp in campaigns:
        result = conn.execute(
            sa.text(
                "INSERT INTO ad_groups "
                "(campaign_id, name, match_type, headlines_json, descriptions_json) "
                "VALUES (:cid, :name, 'phrase', :h, :d) RETURNING id"
            ),
            {
                "cid": camp.id,
                "name": f"{camp.name} — Phrase",
                "h": camp.headlines_json,
                "d": camp.descriptions_json,
            },
        ).fetchone()
        ag_id = result.id
        conn.execute(
            sa.text(
                "UPDATE ad_campaign_keywords SET ad_group_id = :ag WHERE campaign_id = :cid"
            ),
            {"ag": ag_id, "cid": camp.id},
        )

    # Now NOT NULL
    op.alter_column(
        "ad_campaign_keywords",
        "ad_group_id",
        existing_type=sa.Integer(),
        nullable=False,
    )

    # Drop the campaign-level ad copy columns
    op.drop_column("ad_campaigns", "headlines_json")
    op.drop_column("ad_campaigns", "descriptions_json")


def downgrade() -> None:
    op.add_column(
        "ad_campaigns",
        sa.Column("descriptions_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "ad_campaigns",
        sa.Column("headlines_json", sa.Text(), nullable=True),
    )
    op.drop_index(
        "ix_ad_campaign_keywords_ad_group_id", table_name="ad_campaign_keywords"
    )
    op.drop_constraint(
        "fk_ad_campaign_keywords_ad_group_id",
        "ad_campaign_keywords",
        type_="foreignkey",
    )
    op.drop_column("ad_campaign_keywords", "ad_group_id")
    op.drop_index("ix_ad_groups_campaign_id", table_name="ad_groups")
    op.drop_table("ad_groups")
