"""Tests for the Helena module — provider swapping + access-mode enforcement,
the approval-gated task queue, email rendering, and analytics/optimization.

These use an in-memory SQLite DB and never hit external services (Anthropic,
Google Flow, the browser agent, Shopify), so they run offline and fast.
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_SECRET", "test-secret-for-helena")

from gglads.models import (
    Base,
    Integration,
    MetaAdCampaign,
    Post,
    ScheduledTask,
)
from gglads.models.user import User
from gglads.services.helena import analytics as analytics_svc
from gglads.services.helena import calendar as calendar_svc
from gglads.services.helena import dashboard as dashboard_svc
from gglads.services.helena import execution as exec_svc
from gglads.services.helena import optimization as opt_svc
from gglads.services.helena.email.renderer import EmailTemplateRenderer
from gglads.services.helena.meta.factory import get_meta_provider
from gglads.services.helena.specs import CampaignSpec, InstagramPostSpec


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ---- Access-mode enforcement --------------------------------------------

def test_write_blocked_when_not_connected(db):
    provider = get_meta_provider(db)
    res = provider.create_campaign(CampaignSpec(name="x", budget_cents=1000))
    assert res.success is False
    assert "not connected" in res.message


def test_read_only_blocks_publish(db):
    db.add(Integration(name="instagram", config_encrypted="{}",
                       status="connected", access_mode="read_only",
                       auth_type="browser_agent"))
    db.commit()
    provider = get_meta_provider(db)
    res = provider.publish_instagram_post(InstagramPostSpec(caption="hi"))
    assert res.success is False
    assert "Read Only" in res.message


def test_read_write_allows_dispatch_to_backend(db):
    # Connected + read_write reaches the backend; with no browser agent URL the
    # backend itself fails closed (not blocked by the access guard).
    db.add(Integration(name="meta_ads", config_encrypted="{}",
                       status="connected", access_mode="read_write",
                       auth_type="browser_agent"))
    db.commit()
    provider = get_meta_provider(db)
    res = provider.create_campaign(CampaignSpec(name="x", budget_cents=1000))
    assert res.success is False
    assert "browser agent is not configured" in res.message.lower()


# ---- Approval gates + task queue ----------------------------------------

def test_publish_requires_approval(db):
    task = exec_svc.enqueue(db, title="Publish", kind="publish_post",
                            spec={"post_id": 1}, user_id=None)
    assert task.requires_approval is True
    assert task.status == "needs_review"


def test_pause_does_not_require_approval(db):
    task = exec_svc.enqueue(db, title="Pause", kind="pause_campaign",
                            spec={"campaign_id": 1}, user_id=None)
    assert task.requires_approval is False
    assert task.status == "pending"


def test_needs_review_skipped_until_approved(db):
    task = exec_svc.enqueue(db, title="Publish", kind="publish_post",
                            spec={"post_id": 999}, user_id=None)
    # Worker must not run an unapproved task.
    summary = exec_svc.run_due_tasks(db)
    assert summary["ran"] == 0
    exec_svc.approve(db, task.id, user_id=None)
    summary = exec_svc.run_due_tasks(db)
    assert summary["ran"] == 1  # now it runs (and fails: post missing) — but it ran


def test_failed_task_retries_then_fails(db):
    db.add(Post(id=1, caption="hi", status="draft"))
    db.commit()
    # No connected integration -> publish blocked -> task fails and retries.
    task = exec_svc.enqueue(db, title="Publish", kind="publish_post",
                            spec={"post_id": 1}, user_id=None)
    exec_svc.approve(db, task.id, user_id=None)
    exec_svc.run_task(db, db.get(ScheduledTask, task.id))
    refreshed = db.get(ScheduledTask, task.id)
    assert refreshed.attempts == 1
    assert refreshed.status in ("approved", "failed")  # backed off for retry
    assert refreshed.last_error


# ---- Email rendering -----------------------------------------------------

def test_email_render_is_client_safe(db):
    r = EmailTemplateRenderer(["#FF5CA8", "#1A1A1A"])
    html, plain = r.render([
        {"kind": "hero", "headline": "Launch", "subhead": "New"},
        {"kind": "single_product",
         "product": {"title": "Syrup", "price": 7.99, "url": "https://x", "image_url": "https://i"}},
        {"kind": "button", "label": "Shop", "url": "https://x"},
        {"kind": "footer", "brand_name": "Syruvia"},
    ], preheader="hello")
    assert "<table" in html  # table-based layout
    assert "max-width:600px" in html  # ~600px width
    assert "unsubscribe" in html  # unsubscribe placeholder
    assert "Syrup" in plain  # plain-text fallback


# ---- Analytics + optimization -------------------------------------------

def test_analytics_and_optimization(db):
    analytics_svc.ingest_metrics(db, [
        {"platform": "meta_ads", "entity_type": "campaign", "entity_id": 7,
         "metric": "spend", "value": 100, "captured_for": "2026-06-01T00:00:00"},
        {"platform": "meta_ads", "entity_type": "campaign", "entity_id": 7,
         "metric": "revenue", "value": 400, "captured_for": "2026-06-01T00:00:00"},
        {"platform": "meta_ads", "entity_type": "campaign", "entity_id": 7,
         "metric": "clicks", "value": 50, "captured_for": "2026-06-01T00:00:00"},
    ])
    top = analytics_svc.topline(db, days=3650)
    assert top["meta"]["roas"] == 4.0

    db.add(MetaAdCampaign(id=7, name="Winner", budget_cents=5000))
    db.commit()
    recs = opt_svc.recommendations(db, days=3650)
    assert any(r["action"] == "scale" and r["campaign_id"] == 7 for r in recs)


# ---- PERF-DETAIL: customizable dashboard --------------------------------

def test_dashboard_default_and_toggle(db):
    user = User(email="u@x.com", role="admin", is_active=True)
    db.add(user)
    db.commit()
    assert "profit" in dashboard_svc.get_selected(user)

    # toggle a GA4 metric on, then off; persisted to user.preferences
    sel = dashboard_svc.toggle_metric(db, user, "ga4_conversions")
    assert "ga4_conversions" in sel
    sel = dashboard_svc.toggle_metric(db, user, "ga4_conversions")
    assert "ga4_conversions" not in sel


def test_dashboard_cards_compute_profit(db):
    user = User(email="u2@x.com", role="admin", is_active=True)
    db.add(user)
    db.commit()
    analytics_svc.ingest_metrics(db, [
        {"platform": "meta_ads", "entity_type": "account", "metric": "revenue",
         "value": 1000, "captured_for": analytics_svc._now().isoformat()},
        {"platform": "meta_ads", "entity_type": "account", "metric": "spend",
         "value": 300, "captured_for": analytics_svc._now().isoformat()},
    ])
    dashboard_svc.set_selected(db, user, ["profit", "ad_spend"])
    cards = {c["key"]: c for c in dashboard_svc.cards(db, user, days=30)}
    assert cards["ad_spend"]["value"] == 300
    assert cards["profit"]["value"] == 700


def test_chart_series_dual_axis(db):
    user = User(email="u3@x.com", role="admin", is_active=True)
    db.add(user)
    db.commit()
    dashboard_svc.set_selected(db, user, ["profit", "ga4_sessions"])
    chart = dashboard_svc.chart_series(db, user, days=7)
    axes = {s["axis"] for s in chart["series"]}
    assert axes == {"left", "right"}  # currency on left, count on right
    assert all(len(s["points"]) == 7 for s in chart["series"])


def test_data_tables_structure(db):
    tables = {t["key"]: t for t in dashboard_svc.all_tables(db, days=30)}
    assert set(tables) == {"source_medium", "landing_pages", "google_campaigns", "meta_campaigns"}
    for t in tables.values():
        assert "rows" in t and "page_size" in t


# ---- CAL-DETAIL: week/month grid + channel slots ------------------------

def test_calendar_month_grid_has_channel_slots(db):
    from datetime import date
    data = calendar_svc.view_data(db, "month", date(2026, 6, 15))
    assert data["view"] == "month"
    # every day cell carries one slot per channel
    cell = data["weeks"][0][0]
    assert len(cell["slots"]) == len(calendar_svc.CHANNELS)
    assert {s["channel"] for s in cell["slots"]} == calendar_svc.CHANNEL_KEYS


def test_calendar_week_has_seven_days_and_today_weekend(db):
    from datetime import date
    data = calendar_svc.view_data(db, "week", date(2026, 6, 3))  # a Wednesday
    assert len(data["weeks"]) == 1 and len(data["weeks"][0]) == 7
    flags = [(d["is_weekend"], d["date"]) for d in data["weeks"][0]]
    assert sum(1 for w, _ in flags if w) == 2  # Sat + Sun tinted


def test_calendar_add_item_appears_inline(db):
    from datetime import date
    calendar_svc.add_slot_item(db, channel="linkedin", day=date(2026, 6, 10),
                               caption="Launch", user_id=None)
    data = calendar_svc.view_data(db, "month", date(2026, 6, 10))
    found = False
    for week in data["weeks"]:
        for cell in week:
            if cell["date"] == "2026-06-10":
                li = next(s for s in cell["slots"] if s["channel"] == "linkedin")
                found = any(it["title"] == "Launch" for it in li["items"])
    assert found


# ---- Review fixes: generate_image product fallback ----------------------

def test_generate_image_falls_back_to_product_image(db):
    from gglads.models.shopify_product import ShopifyProduct
    from gglads.services.helena import skills
    db.add(ShopifyProduct(id=55, handle="pink", title="Pink Splash",
                          status="active", image_url="https://cdn/pink.jpg"))
    db.commit()
    # Google Flow is unconfigured in tests -> must fall back to product image.
    res = skills.run_skill(db, "generate_image",
                           {"concept": "hero", "product_id": 55},
                           user_id=None, session_id=None)
    assert res["ok"] is True
    assert res["fallback"] is True
    assert res["images"][0]["url"] == "https://cdn/pink.jpg"


# ---- Fixes: email preview link, Shopify-only email, Google Flow auth -----

def test_email_skills_return_preview_url(db):
    from gglads.models.email_campaign import EmailCampaign
    from gglads.services.helena import skills
    db.add(EmailCampaign(id=9, name="Launch", subject="Hi", html="<p>x</p>", status="draft"))
    db.commit()
    r = skills.run_skill(db, "create_email_draft", {"campaign_id": 9}, user_id=None, session_id=None)
    assert r["preview_url"] == "/helena/email/9/preview"
    assert r["status"] == "needs_review"


def test_create_email_draft_is_approval_gated(db):
    task = exec_svc.enqueue(db, title="Draft", kind="create_email_draft",
                            spec={"campaign_id": 1}, user_id=None)
    assert task.requires_approval is True
    assert task.status == "needs_review"


def test_third_party_email_providers_removed():
    from gglads.services.helena.integrations_catalog import all_cards
    cards = set(all_cards())
    assert {"klaviyo", "mailchimp", "instantly", "brevo", "beehiiv"}.isdisjoint(cards)
    assert "shopify" in cards  # Shopify Email path stays


def test_google_flow_unconfigured_reports_clearly():
    from gglads.services.helena.images.google_flow import GoogleFlowImageService
    svc = GoogleFlowImageService()
    assert svc.auth_mode() is None
    ok, detail = svc.test_connection()
    assert ok is False
    assert "GOOGLE_APPLICATION_CREDENTIALS_JSON" in detail or "GOOGLE_FLOW_API_KEY" in detail


# ---- Storage credentials (S3_* or AWS_*) + Flow connect parity ----------

def test_storage_accepts_aws_or_s3_names(monkeypatch):
    import gglads.config as cfg
    from gglads.services.helena import storage
    for k in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_REGION",
              "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("S3_BUCKET", "helena-assets")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    cfg.get_settings.cache_clear()
    try:
        assert storage.is_configured() is True
        assert storage.config_error() is None
        assert storage._resolve()["region"] == "us-west-2"
    finally:
        cfg.get_settings.cache_clear()


def test_storage_config_error_names_missing_vars(monkeypatch):
    import gglads.config as cfg
    from gglads.services.helena import storage
    for k in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
              "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg.get_settings.cache_clear()
    try:
        err = storage.config_error()
        assert err and "S3_BUCKET" in err
    finally:
        cfg.get_settings.cache_clear()


def test_flow_test_connection_uses_generation_path(monkeypatch):
    import gglads.config as cfg
    from gglads.services.helena.images import google_flow as gf
    monkeypatch.setenv("GOOGLE_FLOW_API_KEY", "AIzaTEST")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    cfg.get_settings.cache_clear()
    try:
        svc = gf.GoogleFlowImageService()
        # Discovery picks a model (no network); the test then exercises the same
        # generation path the agent uses.
        monkeypatch.setattr(gf, "discover_image_model",
                            lambda key, pref="": ("models/imagen-4.0", "predict", None))
        monkeypatch.setattr(svc, "_predict_bytes", lambda text, ar: (b"PNGBYTES", None))
        ok, detail = svc.test_connection()
        assert ok is True
        assert "imagen-4.0" in detail
    finally:
        cfg.get_settings.cache_clear()


def test_choose_image_model_prefers_imagen_predict():
    from gglads.services.helena.images.google_flow import choose_image_model
    models = [
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/imagen-3.0-generate-002", "supportedGenerationMethods": ["predict"]},
        {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
    ]
    name, method = choose_image_model(models)
    assert "imagen" in name and method == "predict"
    # Falls back to a Gemini image model via generateContent when no Imagen.
    name2, method2 = choose_image_model(models[:1] + models[2:])
    assert "image" in name2 and method2 == "generateContent"


def test_discover_video_model_picks_veo(monkeypatch):
    from gglads.services.helena.images import veo
    monkeypatch.setattr(veo, "gl_list_models", lambda key: ([
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/veo-3.0-generate-preview", "supportedGenerationMethods": ["predictLongRunning"]},
    ], None))
    name, err = veo.discover_video_model("KEY")
    assert err is None and "veo" in name
