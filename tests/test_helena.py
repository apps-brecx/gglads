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

from gglads.models import (  # noqa: E402
    Base,
    Integration,
    MetaAdCampaign,
    Post,
    ScheduledTask,
)
from gglads.services.helena import analytics as analytics_svc  # noqa: E402
from gglads.services.helena import brand as brand_svc  # noqa: E402
from gglads.services.helena import execution as exec_svc  # noqa: E402
from gglads.services.helena import optimization as opt_svc  # noqa: E402
from gglads.services.helena.email.renderer import EmailTemplateRenderer  # noqa: E402
from gglads.services.helena.meta.factory import get_meta_provider  # noqa: E402
from gglads.services.helena.specs import CampaignSpec, InstagramPostSpec  # noqa: E402


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
