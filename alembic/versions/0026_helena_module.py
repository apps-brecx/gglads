"""Helena module — brand KB, chat, Instagram posts, Meta campaigns, metrics,
scheduled-task queue + execution log, email campaigns, and the extended
Integration model with linked accounts.

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-05 00:00:00.000000

All additive — the pre-existing 5 credential-form integrations keep working
because the new Integration columns have server defaults.
"""
from alembic import op
import sqlalchemy as sa


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- Extend integrations + linked accounts -------------------------
    op.add_column(
        "integrations",
        sa.Column("status", sa.String(length=20), server_default="not_connected", nullable=False),
    )
    op.add_column(
        "integrations",
        sa.Column("access_mode", sa.String(length=12), server_default="read_only", nullable=False),
    )
    op.add_column(
        "integrations",
        sa.Column("auth_type", sa.String(length=15), server_default="api_key", nullable=False),
    )
    op.create_table(
        "integration_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("integration_name", sa.String(length=50), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("handle", sa.String(length=255), nullable=False),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="connected", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["integration_name"], ["integrations.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_integration_accounts_integration_name",
        "integration_accounts", ["integration_name"],
    )

    # ---- Brand KB ------------------------------------------------------
    op.create_table(
        "brands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("tone", sa.Text(), nullable=True),
        sa.Column("visual_style", sa.Text(), nullable=True),
        sa.Column("mood", sa.Text(), nullable=True),
        sa.Column("audience", sa.Text(), nullable=True),
        sa.Column("content_themes", sa.Text(), nullable=True),
        sa.Column("palette_json", sa.Text(), nullable=True),
        sa.Column("guidelines", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "brand_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), server_default="generated", nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_brand_assets_brand_id", "brand_assets", ["brand_id"])

    # ---- Chat ----------------------------------------------------------
    op.create_table(
        "helena_chat_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), server_default="New chat", nullable=False),
        sa.Column("channel", sa.String(length=20), server_default="general", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "helena_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("tool_name", sa.String(length=60), nullable=True),
        sa.Column("tool_payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["helena_chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_messages_session_id", "helena_messages", ["session_id"])

    # ---- Instagram posts ----------------------------------------------
    op.create_table(
        "helena_posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("caption", sa.Text(), server_default="", nullable=False),
        sa.Column("hashtags", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("brand_asset_id", sa.Integer(), sa.ForeignKey("brand_assets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("permalink", sa.Text(), nullable=True),
        sa.Column("account_handle", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_posts_status", "helena_posts", ["status"])
    op.create_index("ix_helena_posts_scheduled_at", "helena_posts", ["scheduled_at"])

    # ---- Meta ad campaigns --------------------------------------------
    op.create_table(
        "helena_meta_campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("objective", sa.String(length=40), server_default="traffic", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("budget_type", sa.String(length=10), server_default="daily", nullable=False),
        sa.Column("budget_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("audience_json", sa.Text(), nullable=True),
        sa.Column("creative_image_url", sa.Text(), nullable=True),
        sa.Column("creative_copy", sa.Text(), nullable=True),
        sa.Column("brand_asset_id", sa.Integer(), sa.ForeignKey("brand_assets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_meta_campaigns_status", "helena_meta_campaigns", ["status"])

    # ---- Metrics -------------------------------------------------------
    op.create_table(
        "helena_metric_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("metric", sa.String(length=40), nullable=False),
        sa.Column("value", sa.Numeric(16, 4), server_default="0", nullable=False),
        sa.Column("captured_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_metric_snapshots_platform", "helena_metric_snapshots", ["platform"])
    op.create_index("ix_helena_metric_snapshots_entity_id", "helena_metric_snapshots", ["entity_id"])
    op.create_index("ix_helena_metric_snapshots_captured_for", "helena_metric_snapshots", ["captured_for"])

    # ---- Scheduled tasks + execution log ------------------------------
    op.create_table(
        "helena_scheduled_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("requires_approval", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recurrence", sa.String(length=40), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_scheduled_tasks_kind", "helena_scheduled_tasks", ["kind"])
    op.create_index("ix_helena_scheduled_tasks_status", "helena_scheduled_tasks", ["status"])
    op.create_index("ix_helena_scheduled_tasks_run_after", "helena_scheduled_tasks", ["run_after"])

    op.create_table(
        "helena_execution_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("helena_scheduled_tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("backend", sa.String(length=10), server_default="browser", nullable=False),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("input_spec_json", sa.Text(), nullable=True),
        sa.Column("steps_json", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("artifacts_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_execution_runs_task_id", "helena_execution_runs", ["task_id"])
    op.create_index("ix_helena_execution_runs_action", "helena_execution_runs", ["action"])
    op.create_index("ix_helena_execution_runs_status", "helena_execution_runs", ["status"])

    # ---- Email campaigns ----------------------------------------------
    op.create_table(
        "helena_email_campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("audience", sa.Text(), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("preheader", sa.String(length=255), nullable=True),
        sa.Column("variants_json", sa.Text(), nullable=True),
        sa.Column("layout_json", sa.Text(), nullable=True),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("plain_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_email_campaigns_status", "helena_email_campaigns", ["status"])

    op.create_table(
        "helena_email_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("html_fragment", sa.Text(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_email_templates_kind", "helena_email_templates", ["kind"])

    op.create_table(
        "helena_email_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("helena_email_campaigns.id", ondelete="CASCADE"), nullable=True),
        sa.Column("role", sa.String(length=20), server_default="hero", nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_helena_email_assets_campaign_id", "helena_email_assets", ["campaign_id"])


def downgrade() -> None:
    op.drop_table("helena_email_assets")
    op.drop_table("helena_email_templates")
    op.drop_table("helena_email_campaigns")
    op.drop_table("helena_execution_runs")
    op.drop_table("helena_scheduled_tasks")
    op.drop_table("helena_metric_snapshots")
    op.drop_table("helena_meta_campaigns")
    op.drop_table("helena_posts")
    op.drop_table("helena_messages")
    op.drop_table("helena_chat_sessions")
    op.drop_table("brand_assets")
    op.drop_table("brands")
    op.drop_index("ix_integration_accounts_integration_name", table_name="integration_accounts")
    op.drop_table("integration_accounts")
    op.drop_column("integrations", "auth_type")
    op.drop_column("integrations", "access_mode")
    op.drop_column("integrations", "status")
