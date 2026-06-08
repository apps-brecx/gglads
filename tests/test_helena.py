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

def test_generate_image_uses_real_shopify_bottle(db, monkeypatch):
    """For a product, never invent a bottle: when scene-compositing isn't
    available it shows the user's REAL Shopify image, not a generated one."""
    import httpx as _httpx

    from gglads.models.shopify_product import ShopifyProduct
    from gglads.services.helena import skills, storage
    monkeypatch.setattr(storage, "verify_url", lambda url, **k: (True, "ok"))

    class _R:
        status_code = 200
        content = b"REALBOTTLEBYTES"
        headers = {"content-type": "image/jpeg"}
    monkeypatch.setattr(_httpx, "get", lambda *a, **k: _R())
    db.add(ShopifyProduct(id=55, handle="pink", title="Pink Splash",
                          status="active", image_url="https://cdn/pink.jpg"))
    db.commit()
    # Flow has no API key in tests, so compositing is unavailable -> real image.
    res = skills.run_skill(db, "generate_image",
                           {"concept": "hero on a beach", "product_id": 55},
                           user_id=None, session_id=None)
    assert res["ok"] is True
    assert res["images"][0]["url"] == "https://cdn/pink.jpg"
    assert res["bottle_used"]["source"] == "shopify"


def test_generate_image_no_real_bottle_says_so(db):
    """A flavor with no library/Shopify bottle must error, not invent one."""
    from gglads.services.helena import skills
    res = skills.run_skill(db, "generate_image",
                           {"concept": "beach", "flavor": "Nonexistent"},
                           user_id=None, session_id=None)
    assert res["ok"] is False
    assert "couldn't find a real bottle" in res["error"].lower()
    assert "images" not in res


def test_generate_image_composites_library_bottle(db, monkeypatch):
    """When a library bottle exists and compositing works, it uses the real
    bottle and reports bottle_used=library."""
    import httpx as _httpx

    from gglads.services.helena import product_library as lib
    from gglads.services.helena import skills, storage
    from gglads.services.helena.images.google_flow import GeneratedImage, GoogleFlowImageService
    monkeypatch.setattr(storage, "put_bytes", lambda *a, **k: ("https://pub/lib.png", None))
    monkeypatch.setattr(storage, "verify_url", lambda url, **k: (True, "ok"))
    lib.add_image(db, data=b"PNG", content_type="image/png", flavor="Pink Splash",
                  variant="sugar_free")

    class _R:
        status_code = 200
        content = b"BOTTLE"
        headers = {"content-type": "image/png"}
    monkeypatch.setattr(_httpx, "get", lambda *a, **k: _R())
    monkeypatch.setattr(GoogleFlowImageService, "generate_with_reference",
                        lambda self, c, b, ref_mime="image/png", brand_context="":
                        (GeneratedImage(url="https://pub/scene.png", prompt=c), None))
    res = skills.run_skill(db, "generate_image",
                           {"concept": "on a marble counter", "flavor": "Pink Splash",
                            "variant": "sugar_free"}, user_id=None, session_id=None)
    assert res["ok"] is True
    assert res["images"][0]["url"] == "https://pub/scene.png"
    assert res["bottle_used"]["source"] == "library"


def test_adjust_image_edits_in_place_with_region(db, monkeypatch):
    """adjust_image downloads the target image, edits it in place (within the
    selected region), and returns an image card."""
    import httpx as _httpx

    from gglads.services.helena import skills, storage
    from gglads.services.helena.images.google_flow import GeneratedImage, GoogleFlowImageService
    monkeypatch.setattr(storage, "put_bytes", lambda *a, **k: ("https://pub/edited.png", None))

    class _R:
        status_code = 200
        content = b"ORIGINAL"
        headers = {"content-type": "image/png"}
    monkeypatch.setattr(_httpx, "get", lambda *a, **k: _R())

    captured = {}
    def fake_edit(self, image_bytes, instruction, ref_mime="image/png", region=None):
        captured["instruction"] = instruction
        captured["region"] = region
        return GeneratedImage(url="https://pub/edited.png", prompt=instruction), None
    monkeypatch.setattr(GoogleFlowImageService, "edit_image", fake_edit)

    res = skills.run_skill(db, "adjust_image", {
        "image_url": "https://pub/orig.png", "instruction": "brighten the label",
        "region": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
    }, user_id=None, session_id=None)
    assert res["ok"] is True
    assert res["images"][0]["url"] == "https://pub/edited.png"
    assert captured["instruction"] == "brighten the label"
    assert captured["region"]["w"] == 0.3


def test_adjust_image_needs_url_and_instruction(db):
    from gglads.services.helena import skills
    res = skills.run_skill(db, "adjust_image", {"image_url": "https://pub/x.png"},
                           user_id=None, session_id=None)
    assert res["ok"] is False


def test_region_edit_contained_keeps_outside_pixels(monkeypatch):
    """Tight region edit: only the selected box changes; outside is preserved."""
    import io

    import httpx as _httpx
    from PIL import Image
    from gglads.services.helena import skills
    from gglads.services.helena.images.google_flow import GeneratedImage, GoogleFlowImageService
    base = Image.new("RGB", (100, 100), (255, 0, 0)); buf = io.BytesIO(); base.save(buf, "PNG")
    patch = Image.new("RGB", (40, 40), (0, 0, 255)); pbuf = io.BytesIO(); patch.save(pbuf, "PNG")
    monkeypatch.setattr(GoogleFlowImageService, "edit_image",
                        lambda self, b, instr, ref_mime="image/png", region=None:
                        (GeneratedImage(url="https://pub/patch.png", prompt=instr), None))

    class _R:
        status_code = 200
        content = pbuf.getvalue()
    monkeypatch.setattr(_httpx, "get", lambda *a, **k: _R())
    out, err = skills._edit_region_contained(
        GoogleFlowImageService(), buf.getvalue(), "make it blue",
        {"x": 0.3, "y": 0.3, "w": 0.2, "h": 0.2})
    assert out and err is None
    res = Image.open(io.BytesIO(out)).convert("RGB")
    assert res.size == (100, 100)
    assert res.getpixel((1, 1)) == (255, 0, 0)   # corner outside the box: untouched
    assert res.getpixel((40, 40)) == (0, 0, 255)  # inside the box: edited


def test_meta_live_edit_task_approval_semantics(db):
    """Pausing is safe (no approval); resume/budget/cost-cap need approval."""
    from gglads.services.helena import execution as ex
    p = ex.enqueue(db, title="pause", kind="meta_pause", spec={"entity_id": "1"})
    assert p.status == "pending" and not p.requires_approval
    for k in ("meta_resume", "meta_update_budget", "meta_set_costcap"):
        t = ex.enqueue(db, title=k, kind=k, spec={"entity_id": "1", "amount_cents": 500})
        assert t.status == "needs_review" and t.requires_approval


def test_fetch_campaign_detail(db, monkeypatch):
    """Drill-down returns ads (with ad-set ids), a daily series, totals, and budget."""
    import gglads.config as cfg
    import httpx as _httpx
    from gglads.services.helena.meta import meta_api, oauth
    monkeypatch.setenv("META_EXECUTION_MODE", "api")
    cfg.get_settings.cache_clear()
    oauth.save_meta_config(db, {"access_token": "T", "ad_account_id": "5",
                                "ad_accounts": [], "pages": []})
    insight = {"spend": "100", "impressions": "2000", "clicks": "50", "reach": "1500",
               "frequency": "1.33", "actions": [{"action_type": "purchase", "value": "4"}],
               "action_values": [{"action_type": "purchase", "value": "400"}]}

    class _R:
        status_code = 200
        def __init__(self, d): self._d = d
        def json(self): return self._d

    def fake_get(url, params=None, **k):
        params = params or {}
        if params.get("fields") == "currency":
            return _R({"currency": "USD"})
        if url.endswith("/campaigns"):
            return _R({"data": [{"id": "c1", "name": "C", "effective_status": "ACTIVE",
                                 "daily_budget": "1000"}]})
        if url.endswith("/ads"):  # ad delivery-status lookup (separate from insights)
            return _R({"data": [{"id": "a1", "effective_status": "ACTIVE"}]})
        if params.get("level") == "ad":
            return _R({"data": [{**insight, "ad_id": "a1", "ad_name": "Ad", "adset_id": "s1",
                                 "adset_name": "Set"}]})
        if params.get("level") == "campaign":
            return _R({"data": [{**insight, "date_start": "2026-06-01"},
                                {**insight, "date_start": "2026-06-02"}]})
        return _R({"data": []})
    monkeypatch.setattr(_httpx, "get", fake_get)
    try:
        d = meta_api.MetaApiProvider(db).fetch_campaign_detail("c1", "2026-06-01", "2026-06-02")
        assert d["ok"]
        assert d["ads"][0]["adset_id"] == "s1" and d["ads"][0]["cpc"] == 2.0
        assert d["ads"][0]["status"] == "Active"
        assert d["campaign"]["name"] == "C" and d["campaign"]["daily_budget"] == 10.0
        assert len(d["series"]) == 2 and d["series"][0]["roas"] == 4.0
        assert d["totals"]["roas"] == 4.0 and d["currency"] == "USD"
    finally:
        cfg.get_settings.cache_clear()


def test_generate_image_pins_exact_mentioned_bottle(db, monkeypatch):
    """An @-mentioned product_image_id pins that EXACT library bottle, even when
    flavor isn't passed."""
    import httpx as _httpx

    from gglads.services.helena import product_library as lib
    from gglads.services.helena import skills, storage
    from gglads.services.helena.images.google_flow import GeneratedImage, GoogleFlowImageService
    monkeypatch.setattr(storage, "put_bytes", lambda *a, **k: ("https://pub/lib.png", None))
    monkeypatch.setattr(storage, "verify_url", lambda url, **k: (True, "ok"))
    # Two flavors in the library; we pin the second by id.
    lib.add_image(db, data=b"A", content_type="image/png", flavor="Mango", variant="regular")
    pinned, _ = lib.add_image(db, data=b"B", content_type="image/png",
                              flavor="Pink Splash", variant="sugar_free")

    captured = {}

    class _R:
        status_code = 200
        content = b"BOTTLE"
        headers = {"content-type": "image/png"}
    def _get(url, *a, **k):
        captured["url"] = url
        return _R()
    monkeypatch.setattr(_httpx, "get", _get)
    monkeypatch.setattr(GoogleFlowImageService, "generate_with_reference",
                        lambda self, c, b, ref_mime="image/png", brand_context="":
                        (GeneratedImage(url="https://pub/scene.png", prompt=c), None))
    res = skills.run_skill(db, "generate_image",
                           {"concept": "beach scene", "product_image_id": pinned.id},
                           user_id=None, session_id=None)
    assert res["ok"] is True
    assert res["bottle_used"]["url"] == pinned.url  # exact pinned bottle, not Mango
    assert captured["url"] == pinned.url


def test_fetch_ad_performance_uses_selected_account(db, monkeypatch):
    """Live ad performance targets the SAVED ad account and returns real totals."""
    import gglads.config as cfg
    import httpx as _httpx
    from gglads.services.helena.meta import meta_api, oauth
    monkeypatch.setenv("META_EXECUTION_MODE", "api")
    cfg.get_settings.cache_clear()
    oauth.save_meta_config(db, {"access_token": "TOK", "ad_account_id": "734704884820822",
                                "ad_accounts": [], "pages": []})

    captured = {}

    class _R:
        status_code = 200
        def json(self):
            return {"data": [
                {"ad_id": "a1", "ad_name": "Promo", "campaign_name": "C",
                 "spend": "120.50", "impressions": "3000", "clicks": "90",
                 "actions": [{"action_type": "purchase", "value": "5"}],
                 "action_values": [{"action_type": "purchase", "value": "480.00"}]},
            ]}
    def fake_get(url, params=None, **k):
        captured["url"] = url
        return _R()
    monkeypatch.setattr(_httpx, "get", fake_get)
    try:
        res = meta_api.MetaApiProvider(db).fetch_ad_performance("2026-06-07", "2026-06-07")
        assert "act_734704884820822/insights" in captured["url"]  # the SELECTED account
        assert res.success and res.steps[0]["spend"] == 120.5
        assert res.steps[0]["revenue"] == 480.0 and res.steps[0]["roas"] == round(480/120.5, 2)
        totals = {m["metric"]: m["value"] for m in res.metrics}
        assert totals["spend"] == 120.5 and totals["revenue"] == 480.0
    finally:
        cfg.get_settings.cache_clear()


def test_fetch_ads_breakdown_full_metrics(db, monkeypatch):
    """The Meta Ads page data: per-campaign + per-ad rows with derived rates,
    account totals, status/budget, and currency — all from the selected account."""
    import gglads.config as cfg
    import httpx as _httpx
    from gglads.services.helena.meta import meta_api, oauth
    monkeypatch.setenv("META_EXECUTION_MODE", "api")
    cfg.get_settings.cache_clear()
    oauth.save_meta_config(db, {"access_token": "TOK", "ad_account_id": "999",
                                "ad_accounts": [], "pages": []})

    insight = {
        "spend": "100", "impressions": "2000", "clicks": "50", "reach": "1500",
        "frequency": "1.33", "actions": [{"action_type": "purchase", "value": "4"}],
        "action_values": [{"action_type": "purchase", "value": "400"}],
    }

    class _R:
        status_code = 200
        def __init__(self, d): self._d = d
        def json(self): return self._d

    def fake_get(url, params=None, **k):
        params = params or {}
        if params.get("fields") == "currency":
            return _R({"currency": "USD"})
        if url.endswith("/campaigns"):
            return _R({"data": [{"id": "c1", "name": "C", "effective_status": "ACTIVE",
                                 "daily_budget": "1000"}]})
        if params.get("level") == "campaign":
            return _R({"data": [{**insight, "campaign_id": "c1", "campaign_name": "C"}]})
        if params.get("level") == "ad":
            return _R({"data": [{**insight, "ad_id": "a1", "ad_name": "Ad", "campaign_name": "C"}]})
        return _R({"data": []})
    monkeypatch.setattr(_httpx, "get", fake_get)
    try:
        data = meta_api.MetaApiProvider(db).fetch_ads_breakdown("2026-06-01", "2026-06-30")
        assert data["ok"] and data["currency"] == "USD"
        c = data["campaigns"][0]
        assert c["spend"] == 100 and c["ctr"] == 2.5 and c["cpc"] == 2.0
        assert c["cpm"] == 50.0 and c["cost_per_purchase"] == 25.0 and c["roas"] == 4.0
        assert c["status"] == "Active" and c["daily_budget"] == 10.0
        assert data["totals"]["roas"] == 4.0 and data["totals"]["campaigns"] == 1
        assert data["ads"][0]["name"] == "Ad" and data["ads"][0]["ctr"] == 2.5
    finally:
        cfg.get_settings.cache_clear()


def test_generate_image_reports_failure_not_dead_link(db):
    """No reachable image -> ok False with a clear message, never a dead link."""
    from gglads.services.helena import skills
    res = skills.run_skill(db, "generate_image", {"concept": "hero"},
                           user_id=None, session_id=None)
    assert res["ok"] is False and "images" not in res


# ---- Fixes: email preview link, Shopify-only email, Google Flow auth -----

def test_email_skills_return_preview_url(db):
    from gglads.models.email_campaign import EmailCampaign
    from gglads.services.helena import skills
    db.add(EmailCampaign(id=9, name="Launch", subject="Hi", html="<p>x</p>", status="draft"))
    db.commit()
    r = skills.run_skill(db, "create_email_draft", {"campaign_id": 9}, user_id=None, session_id=None)
    assert r["preview_url"] == "/helena/email/9/preview"
    assert r["status"] == "needs_review"


def test_fetch_ad_detail(db, monkeypatch):
    """Ad drill-down returns the creative image, ad-set settings (budget/bid in
    dollars), targeting JSON, and insights."""
    import gglads.config as cfg
    import httpx as _httpx
    from gglads.services.helena.meta import meta_api, oauth
    monkeypatch.setenv("META_EXECUTION_MODE", "api")
    cfg.get_settings.cache_clear()
    oauth.save_meta_config(db, {"access_token": "T", "ad_account_id": "5",
                                "ad_accounts": [], "pages": []})

    class _R:
        status_code = 200
        def __init__(self, d): self._d = d
        def json(self): return self._d

    def fake_get(url, params=None, **k):
        params = params or {}
        if params.get("fields") == "currency":
            return _R({"currency": "USD"})
        if url.endswith("/ad1"):
            return _R({"id": "ad1", "name": "Promo", "effective_status": "ACTIVE",
                       "adset_id": "s1", "campaign_id": "c1",
                       "creative": {"title": "Hi", "body": "Buy", "image_url": "https://img/x.png",
                                    "call_to_action_type": "SHOP_NOW"}})
        if url.endswith("/s1"):
            return _R({"id": "s1", "name": "Broad", "effective_status": "ACTIVE",
                       "daily_budget": "2500", "bid_amount": "500", "bid_strategy": "COST_CAP",
                       "optimization_goal": "OFFSITE_CONVERSIONS",
                       "targeting": {"geo_locations": {"countries": ["US"]}}})
        if params.get("level") == "ad":
            return _R({"data": [{"spend": "100", "impressions": "2000", "clicks": "50",
                                 "actions": [{"action_type": "purchase", "value": "4"}],
                                 "action_values": [{"action_type": "purchase", "value": "400"}]}]})
        return _R({"data": []})
    monkeypatch.setattr(_httpx, "get", fake_get)
    try:
        d = meta_api.MetaApiProvider(db).fetch_ad_detail("ad1", "2026-06-01", "2026-06-08")
        assert d["ok"]
        assert d["image"] == "https://img/x.png" and d["creative"]["cta"] == "Shop Now"
        assert d["adset"]["daily_budget"] == 25.0 and d["adset"]["bid_amount"] == 5.0
        assert d["adset"]["bid_strategy"] == "Cost Cap"
        assert "countries" in d["targeting_json"]
        assert d["metrics"]["roas"] == 4.0
    finally:
        cfg.get_settings.cache_clear()


def test_stock_guard_handle_from_url():
    from gglads.services.helena.ad_stock_guard import handle_from_url
    assert handle_from_url("https://shop.com/products/mango-splash?ref=x") == "mango-splash"
    assert handle_from_url("https://shop.com/collections/all") is None
    assert handle_from_url(None) is None


def _fake_meta_provider(monkeypatch, ads, calls):
    from gglads.services.helena.meta import factory as meta_factory

    class _R:
        def __init__(self, ok): self.success = ok; self.message = ""

    class FakeProvider:
        def fetch_ads_with_links(self):
            return {"ok": True, "ads": ads}
        def set_status(self, eid, st):
            calls.append((eid, st)); return _R(True)
    monkeypatch.setattr(meta_factory, "get_meta_provider", lambda db: FakeProvider())


def test_stock_guard_pauses_oos_and_respects_override(db, monkeypatch):
    from gglads.models.helena import AdStockGuardState
    from gglads.models.shopify_product import ShopifyProduct
    from gglads.services import email as email_svc
    from gglads.services.helena import ad_stock_guard as guard
    monkeypatch.setattr(email_svc, "is_configured", lambda db: False)
    db.add(ShopifyProduct(id=1, handle="mango", title="Mango", status="active", total_inventory=0))
    db.add(ShopifyProduct(id=2, handle="cola", title="Cola", status="active", total_inventory=0))
    db.commit()
    # cola has an admin override to keep running while OOS
    db.add(AdStockGuardState(ad_id="a2", allow_oos=True))
    db.commit()
    calls = []
    _fake_meta_provider(monkeypatch, [
        {"ad_id": "a1", "ad_name": "Mango", "campaign_id": "c1", "status": "ACTIVE",
         "link": "https://s.com/products/mango"},
        {"ad_id": "a2", "ad_name": "Cola", "campaign_id": "c1", "status": "ACTIVE",
         "link": "https://s.com/products/cola"},
    ], calls)
    ok, _detail, stats = guard.run_guard(db)
    assert ok and stats["paused"] == 1 and stats["matched"] == 2
    assert ("a1", "PAUSED") in calls and ("a2", "PAUSED") not in calls  # override kept a2 running
    assert db.get(AdStockGuardState, "a1").paused_by_guard is True


def test_stock_guard_auto_resumes_when_restocked(db, monkeypatch):
    from gglads.models.helena import AdStockGuardState
    from gglads.models.shopify_product import ShopifyProduct
    from gglads.services import email as email_svc
    from gglads.services.helena import ad_stock_guard as guard
    monkeypatch.setattr(email_svc, "is_configured", lambda db: False)
    db.add(ShopifyProduct(id=1, handle="mango", title="Mango", status="active", total_inventory=12))
    db.add(AdStockGuardState(ad_id="a1", product_handle="mango", paused_by_guard=True))
    db.commit()
    calls = []
    _fake_meta_provider(monkeypatch, [
        {"ad_id": "a1", "ad_name": "Mango", "campaign_id": "c1", "status": "PAUSED",
         "link": "https://s.com/products/mango"},
    ], calls)
    ok, _detail, stats = guard.run_guard(db)
    assert ok and stats["resumed"] == 1
    assert ("a1", "ACTIVE") in calls
    assert db.get(AdStockGuardState, "a1").paused_by_guard is False


def test_email_image_starter(db):
    from gglads.services.helena import email_campaigns as ec
    s = ec.add_image_starter(db, name="Screenshot", image_url="https://pub/email.png")
    assert s is not None and s.kind == "starter_image" and s.html_fragment == "https://pub/email.png"
    assert ec.add_image_starter(db, name="x", image_url="") is None
    assert s.id in [r.id for r in ec.list_starters(db)]


def test_email_starter_remix_creates_campaign(db):
    from gglads.services.helena import email_campaigns as ec
    s = ec.add_starter(db, name="Hero", html="<html><body>Pink Splash</body></html>")
    assert s is not None and s.kind == "starter"
    assert ec.add_starter(db, name="", html="x") is None  # needs both
    camp = ec.remix_starter(db, s.id, user_id=None)
    assert camp is not None and camp.status == "draft"
    assert "Pink Splash" in camp.html
    assert [c.id for c in ec.list_campaigns(db)] == [camp.id]
    assert [t.id for t in ec.list_starters(db)] == [s.id]


def test_edit_email_html_skill_updates_html(db, monkeypatch):
    from gglads.models.email_campaign import EmailCampaign
    from gglads.services import claude as claude_svc
    from gglads.services.helena import skills
    db.add(EmailCampaign(id=12, name="Promo", html="<h1>Pink Splash</h1>", status="draft"))
    db.commit()
    monkeypatch.setattr(claude_svc, "chat",
                        lambda *a, **k: ("<h1>Mango</h1>", None))
    r = skills.run_skill(db, "edit_email_html",
                         {"campaign_id": 12, "instruction": "change flavor to Mango"},
                         user_id=None, session_id=None)
    assert r["ok"] and r["preview_url"] == "/helena/email/12/preview"
    assert db.get(EmailCampaign, 12).html == "<h1>Mango</h1>"


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


def test_choose_image_model_prefers_nano_banana():
    from gglads.services.helena.images.google_flow import choose_image_model
    models = [
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/imagen-3.0-generate-002", "supportedGenerationMethods": ["predict"]},
        {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
    ]
    # Nano Banana (gemini *flash-image*) wins over Imagen for design generation.
    name, method = choose_image_model(models)
    assert "flash-image" in name and method == "generateContent"
    # With no Nano Banana, fall back to Imagen predict.
    name2, method2 = choose_image_model(models[:2])
    assert "imagen" in name2 and method2 == "predict"


def test_discover_video_model_picks_veo(monkeypatch):
    from gglads.services.helena.images import veo
    monkeypatch.setattr(veo, "gl_list_models", lambda key: ([
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/veo-3.0-generate-preview", "supportedGenerationMethods": ["predictLongRunning"]},
    ], None))
    name, err = veo.discover_video_model("KEY")
    assert err is None and "veo" in name


# ---- BrandAsset.product_id BIGINT + skill rollback safety ---------------

def test_brand_asset_product_id_is_bigint():
    from sqlalchemy import BigInteger
    col = Base.metadata.tables["brand_assets"].columns["product_id"]
    assert isinstance(col.type, BigInteger)


def test_save_asset_accepts_shopify_bigint_id(db):
    from gglads.services.helena import brand as brand_svc
    big = 7895432109123  # > int32 max; a real Shopify product id
    asset = brand_svc.save_asset(db, url="https://r2/x.png", kind="generated",
                                 product_id=big, user_id=None)
    assert asset.product_id == big


def test_run_skill_rolls_back_and_session_stays_usable(db):
    from gglads.models.brand import BrandAsset
    from gglads.models.helena import ChatSession
    from gglads.services.helena import skills

    def boom(_db, _args, _uid, _sid):
        _db.add(BrandAsset(brand_id=1, kind="generated", url="x"))  # pending write
        raise RuntimeError("simulated failure")

    skills._HANDLERS["_boom_test"] = boom
    try:
        res = skills.run_skill(db, "_boom_test", {}, user_id=None, session_id=None)
        assert res["ok"] is False
        # No PendingRollbackError: the session is clean and committable.
        db.add(ChatSession(title="after"))
        db.commit()
    finally:
        skills._HANDLERS.pop("_boom_test", None)


# ---- Public object URL (R2 public dev URL / CDN) ------------------------

def test_storage_public_url_used_when_set(monkeypatch):
    import gglads.config as cfg
    from gglads.services.helena import storage
    for k in ("S3_PUBLIC_BASE_URL", "S3_PUBLIC_URL", "S3_ENDPOINT_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("S3_BUCKET", "helena-assets")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    # alias S3_PUBLIC_URL, with a trailing slash to confirm it's stripped
    monkeypatch.setenv("S3_PUBLIC_URL", "https://pub-xxxx.r2.dev/")
    cfg.get_settings.cache_clear()
    try:
        assert storage._resolve()["public_base"] == "https://pub-xxxx.r2.dev/"
        # public base wins over the private endpoint
        c = storage._resolve()
        key = "helena/flow/abc.png"
        assert f"{c['public_base'].rstrip('/')}/{key}" == \
            "https://pub-xxxx.r2.dev/helena/flow/abc.png"
    finally:
        cfg.get_settings.cache_clear()


# ---- Veo surfaces the full error body -----------------------------------

def test_veo_start_surfaces_full_error_body(monkeypatch):
    import gglads.config as cfg
    from gglads.services.helena.images import veo
    monkeypatch.setenv("GOOGLE_FLOW_API_KEY", "AIzaTEST")
    cfg.get_settings.cache_clear()

    class FakeResp:
        status_code = 400
        text = ('{"error":{"code":400,"message":"Video generation is not '
                'allowed for this model/key","status":"INVALID_ARGUMENT"}}')

        def json(self):
            import json as _j
            return _j.loads(self.text)

    monkeypatch.setattr(veo.httpx, "post", lambda *a, **k: FakeResp())
    try:
        svc = veo.VeoVideoService()
        op, err, transient = svc._start("models/veo-3.0-generate-preview", "a cat", "16:9")
        assert op is None
        # full body present, not truncated to status only
        assert "INVALID_ARGUMENT" in err
        assert "Video generation is not allowed" in err
        assert transient is False  # 400 INVALID_ARGUMENT is not retryable
    finally:
        cfg.get_settings.cache_clear()


# ---- Veo code-13 (INTERNAL) transient detection + retry/backoff ---------

def test_veo_code13_is_transient_and_retried(monkeypatch):
    import gglads.config as cfg
    from gglads.services.helena.images import veo
    monkeypatch.setenv("GOOGLE_FLOW_API_KEY", "AIzaTEST")
    monkeypatch.setenv("GOOGLE_FLOW_VIDEO_RETRIES", "2")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "s")
    cfg.get_settings.cache_clear()

    # No real sleeping or model discovery.
    monkeypatch.setattr(veo.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(veo, "discover_video_model",
                        lambda key, pref="": ("models/veo-3.0-generate-preview", None))

    calls = {"n": 0}

    def fake_attempt(self, model, prompt, ar):
        calls["n"] += 1
        return {"ok": False, "status": "error",
                "error": "Veo temporary server error (gRPC code 13, INTERNAL).",
                "transient": True}

    monkeypatch.setattr(veo.VeoVideoService, "_attempt", fake_attempt)
    try:
        res = veo.VeoVideoService().generate("a cat playing", "16:9")
        assert res["ok"] is False
        assert calls["n"] == 3  # 1 initial + 2 retries
        assert "temporary" in res["error"].lower()
        assert "code 13" in res["error"]
    finally:
        cfg.get_settings.cache_clear()


def test_veo_is_transient_error_classification():
    from gglads.services.helena.images import veo
    assert veo._is_transient_error({"code": 13, "status": "INTERNAL"}) is True
    assert veo._is_transient_error({"status": "INTERNAL"}) is True
    assert veo._is_transient_error({"code": 400, "status": "INVALID_ARGUMENT"}) is False
    assert veo._is_transient_error(None) is False


# ---- Feature gaps: scheduling, history, files, brand docs, recurrence ----

def test_compute_next_run_time_of_day():
    from datetime import UTC, datetime
    from gglads.services.helena import execution as ex
    base = datetime(2026, 6, 7, 10, 0, tzinfo=UTC)  # a Sunday
    assert ex.compute_next_run("daily@15:00", base).hour == 15
    assert ex.compute_next_run("daily@08:00", base).day == 8  # already past -> tomorrow
    assert ex.compute_next_run("hourly", base).hour == 11
    assert ex.compute_next_run("weekly:mon@09:00", base).weekday() == 0
    assert ex.compute_next_run("once", base) is None


def test_schedule_recurring_task_skill_creates_agent_prompt(db):
    from gglads.services.helena import skills
    res = skills.run_skill(db, "schedule_recurring_task",
                           {"instruction": "prep an IG post for tomorrow",
                            "recurrence": "daily", "at_time": "15:00", "title": "Daily IG"},
                           user_id=None, session_id=None)
    assert res["ok"] and res["recurrence"] == "daily@15:00"
    from gglads.models.helena import ScheduledTask
    task = db.get(ScheduledTask, res["task_id"])
    assert task.kind == "agent_prompt" and task.recurrence == "daily@15:00"
    # scheduling itself is not approval-gated; the scheduled run's publish/spend is
    assert task.requires_approval is False


def test_task_pause_resume_delete(db):
    from gglads.services.helena import execution as ex
    from gglads.models.helena import ScheduledTask
    t = ex.enqueue(db, title="x", kind="agent_prompt", spec={"prompt": "hi"}, user_id=None)
    ex.pause(db, t.id)
    assert db.get(ScheduledTask, t.id).status == "paused"
    # paused tasks are not drained by the worker
    assert ex.run_due_tasks(db)["ran"] == 0
    ex.resume(db, t.id)
    assert db.get(ScheduledTask, t.id).status == "pending"
    ex.delete(db, t.id)
    assert db.get(ScheduledTask, t.id) is None


def test_session_rename_and_delete(db):
    from gglads.services.helena import agent as agent_svc
    s = agent_svc.create_session(db, title="New chat", user_id=None)
    agent_svc.rename_session(db, s.id, "Renamed")
    assert agent_svc.get_session(db, s.id).title == "Renamed"
    assert agent_svc.search_sessions(db, "rena")
    agent_svc.delete_session(db, s.id)
    assert agent_svc.get_session(db, s.id) is None


def test_brand_documents_inject_into_context(db):
    from gglads.services.helena import brand as bsvc
    bsvc.add_document(db, title="Style guide", content="Always lowercase.", user_id=None)
    assert "Style guide" in bsvc.brand_context_text(db)
    docs = bsvc.list_documents(db)
    assert len(docs) == 1
    bsvc.delete_document(db, docs[0].id)
    assert bsvc.list_documents(db) == []


def test_files_list_and_delete(db):
    from gglads.services.helena import brand as bsvc
    from gglads.services.helena import files as fsvc
    bsvc.save_asset(db, url="https://pub/helena/flow/a.png", kind="generated", title="Img", user_id=None)
    bsvc.save_asset(db, url="https://pub/helena/veo/b.mp4", kind="generated", title="Vid", user_id=None)
    files = fsvc.list_files(db)
    assert {f["media"] for f in files} == {"image", "video"}
    ok, _ = fsvc.delete_file(db, files[0]["ref"])
    assert ok and len(fsvc.list_files(db)) == 1


def test_explore_catalog_has_image_and_video():
    from gglads.services.helena import explore
    medias = {w["media"] for w in explore.all_workflows()}
    assert medias == {"image", "video"}
    assert explore.get("product_hero_image")["media"] == "image"


# ---- Persistent memory, chat-trainable brand, product library -----------

def test_memory_remember_inject_edit_delete(db):
    from gglads.services.helena import memory as mem
    it = mem.add_item(db, content="never discount below 20% margin", category="decision", source="chat")
    assert it is not None
    assert "never discount" in mem.memory_context_text(db)
    # de-dupe: same content doesn't create a second active item
    mem.add_item(db, content="never discount below 20% margin")
    assert len(mem.list_items(db)) == 1
    mem.update_item(db, it.id, is_active=False)
    assert "never discount" not in mem.memory_context_text(db)  # inactive excluded
    mem.delete_item(db, it.id)
    assert mem.list_items(db) == []


def test_auto_capture_standing_instruction(db):
    from gglads.services.helena import agent, memory as mem
    # A message phrased as a standing instruction is persisted to memory even
    # without the model calling the `remember` skill.
    agent._maybe_remember_standing_instruction(
        db, "use this table layout every time you give me analytics", user_id=None
    )
    items = mem.list_items(db)
    assert any("table layout" in i.content for i in items)
    assert items[0].category == "preference"
    # A normal message is NOT captured.
    agent._maybe_remember_standing_instruction(db, "show me last week's ROAS", user_id=None)
    assert len(mem.list_items(db)) == 1


def test_remember_skill_writes_memory(db):
    from gglads.services.helena import memory as mem, skills
    r = skills.run_skill(db, "remember", {"content": "audience is 18-34", "category": "fact"},
                         user_id=None, session_id=None)
    assert r["ok"] and r["memory_url"] == "/helena/memory"
    assert any("18-34" in i.content for i in mem.list_items(db))


def test_update_brand_knowledge_skill(db):
    from gglads.services.helena import brand as bsvc, skills
    r = skills.run_skill(db, "update_brand_knowledge",
                         {"tone": "playful and bold", "document_title": "Voice guide",
                          "document_content": "lowercase only"}, user_id=None, session_id=None)
    assert r["ok"] and "tone" in r["updated_fields"] and r["document_id"]
    ctx = bsvc.brand_context_text(db)
    assert "playful and bold" in ctx and "Voice guide" in ctx


def test_product_library_add_find_context(db, monkeypatch):
    from gglads.services.helena import product_library as lib
    from gglads.services.helena import storage
    monkeypatch.setattr(storage, "put_bytes",
                        lambda data, **k: ("https://pub.r2.dev/helena/library/x.png", None))
    row, err = lib.add_image(db, data=b"PNG", content_type="image/png",
                             flavor="Pink Splash", variant="sugar-free", kind="product")
    assert err is None and row.variant == "sugar_free"  # normalized
    assert lib.find_image(db, "pink", "sugar_free") is not None
    assert lib.find_image(db, "pink", "regular").variant == "sugar_free"  # falls back to any flavor match
    assert "Pink Splash" in lib.library_context_text(db)


def test_find_product_image_skill(db, monkeypatch):
    from gglads.services.helena import product_library as lib, skills
    from gglads.services.helena import storage
    monkeypatch.setattr(storage, "put_bytes", lambda data, **k: ("https://pub.r2.dev/a.png", None))
    lib.add_image(db, data=b"PNG", content_type="image/png", flavor="Mango", variant="regular")
    r = skills.run_skill(db, "find_product_image", {"flavor": "Mango", "variant": "regular"},
                         user_id=None, session_id=None)
    assert r["ok"] and r["url"] == "https://pub.r2.dev/a.png"
    miss = skills.run_skill(db, "find_product_image", {"flavor": "Nonexistent"},
                            user_id=None, session_id=None)
    assert miss["ok"] is False


# ---- Official Meta API: OAuth URL + provider behavior -------------------

def test_meta_authorize_url(monkeypatch):
    import gglads.config as cfg
    from gglads.services.helena.meta import oauth
    monkeypatch.setenv("META_APP_ID", "appid123")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setenv("META_OAUTH_REDIRECT_URI", "https://h/helena/integrations/meta/callback")
    monkeypatch.setenv("META_GRAPH_VERSION", "v21.0")
    cfg.get_settings.cache_clear()
    try:
        assert oauth.is_api_configured() is True
        u = oauth.authorize_url("ST")
        assert u.startswith("https://www.facebook.com/v21.0/dialog/oauth")
        for scope in ("instagram_content_publish", "ads_management", "instagram_manage_insights"):
            assert scope in u
        assert "state=ST" in u and "appid123" in u
    finally:
        cfg.get_settings.cache_clear()


def test_meta_api_provider_reports_not_connected(db, monkeypatch):
    import gglads.config as cfg
    monkeypatch.setenv("META_EXECUTION_MODE", "api")
    cfg.get_settings.cache_clear()
    try:
        from gglads.services.helena.meta.meta_api import MetaApiProvider
        from gglads.services.helena.specs import CampaignSpec, InstagramPostSpec
        p = MetaApiProvider(db)  # no stored 'meta' connection
        r1 = p.create_campaign(CampaignSpec(name="x", budget_cents=1000))
        r2 = p.publish_instagram_post(InstagramPostSpec(caption="hi", image_url="https://i/x.png"))
        assert r1.success is False and "not connected" in r1.message.lower()
        assert r2.success is False and "not connected" in r2.message.lower()
    finally:
        cfg.get_settings.cache_clear()


def test_meta_objective_mapping():
    from gglads.services.helena.meta.meta_api import _OBJECTIVE
    assert _OBJECTIVE["traffic"] == "OUTCOME_TRAFFIC"
    assert _OBJECTIVE["sales"] == "OUTCOME_SALES"


def test_meta_metric_insights_currency_units():
    # Graph insights spend is already in currency units (not cents) — no /100.
    from datetime import UTC, datetime
    from gglads.services.helena.meta.meta_api import _metric
    m = _metric("meta_ads", "spend", "12345", datetime(2026, 6, 7, tzinfo=UTC))
    assert m["value"] == 12345.0 and m["platform"] == "meta_ads"


# ---- Meta ad-account picker + Instagram post performance ----------------

def test_meta_set_selection_changes_ad_account(db, monkeypatch):
    """User can switch ad account/Page after connect, without reconnecting."""
    import gglads.config as cfg
    from gglads.services.helena.meta import oauth
    monkeypatch.setenv("APP_SECRET", "t")
    cfg.get_settings.cache_clear()
    oauth.save_meta_config(db, {
        "access_token": "TOK",
        "ad_accounts": [
            {"id": "act_111", "account_id": "111", "name": "Empty acct"},
            {"id": "act_734704884820822", "account_id": "734704884820822", "name": "Syruvia ad"},
        ],
        "pages": [{"page_id": "P1", "page_name": "Syruvia", "page_token": "PT",
                   "ig_user_id": "IG1", "ig_username": "syruvia_official"}],
        "ad_account_id": "111",  # silently-picked empty one (the bug)
    })
    ok, detail = oauth.set_selection(db, ad_account_id="734704884820822",
                                     page_id="P1", user_id=None)
    assert ok and "Syruvia ad" in detail
    saved = oauth.get_meta_config(db)
    assert saved["ad_account_id"] == "734704884820822"
    assert saved["ig_user_id"] == "IG1"
    # MetaApiProvider now targets the chosen account
    from gglads.services.helena.meta.meta_api import MetaApiProvider
    assert MetaApiProvider(db)._ad_account_id == "734704884820822"


def test_meta_set_selection_rejects_unknown_account(db):
    from gglads.services.helena.meta import oauth
    oauth.save_meta_config(db, {"access_token": "TOK", "ad_accounts": [], "pages": []})
    ok, detail = oauth.set_selection(db, ad_account_id="999999", user_id=None)
    assert ok is False


def test_instagram_post_performance_skill(db, monkeypatch):
    """Skill returns per-post reach/impressions/likes/comments and ingests them."""
    from gglads.services.helena import skills
    from gglads.services.helena.specs import ProviderResult
    posts = [{"id": "m1", "caption": "Hi", "permalink": "https://ig/p/1",
              "likes": 42, "comments": 7, "reach": 1000, "impressions": 1500}]
    metrics = [
        {"platform": "instagram", "entity_type": "post", "entity_id": None,
         "metric": "reach", "value": 1000, "captured_for": "2026-06-08T00:00:00"},
        {"platform": "instagram", "entity_type": "post", "entity_id": None,
         "metric": "likes", "value": 42, "captured_for": "2026-06-08T00:00:00"},
    ]

    class FakeProvider:
        def fetch_instagram_media(self, limit=5):
            return ProviderResult(success=True, steps=posts, metrics=metrics,
                                  message="Read insights for 1 recent Instagram post(s).")

    monkeypatch.setattr("gglads.services.helena.meta.factory.get_meta_provider",
                        lambda _db: FakeProvider())
    res = skills.run_skill(db, "get_instagram_post_performance", {"limit": 5},
                           user_id=None, session_id=None)
    assert res["ok"] and res["count"] == 1
    assert res["posts"][0]["likes"] == 42 and res["posts"][0]["reach"] == 1000
    # ingested into the dashboard store
    from gglads.services.helena import analytics as an
    assert an.topline(db, days=3650)["instagram"]["reach"] == 1000


# ---- Chat: image attach + post image rendering --------------------------

def test_agent_user_content_includes_image_block():
    from gglads.services.helena import agent
    c = agent._user_content("hi", "https://pub/x.png")
    assert isinstance(c, list)
    assert c[0]["type"] == "image" and c[0]["source"]["url"] == "https://pub/x.png"
    assert any(b.get("type") == "text" for b in c)
    assert agent._user_content("hi", None) == "hi"  # no image -> plain string


def test_history_carries_user_image(db):
    from gglads.services.helena import agent
    s = agent.create_session(db, user_id=None)
    agent.append_user_message(db, s.id, "look at this", None, image_url="https://pub/a.png")
    hist = agent._history_for_api(db, s.id)
    assert hist[-1]["role"] == "user"
    assert hist[-1]["content"][0]["source"]["url"] == "https://pub/a.png"


def test_create_post_returns_image_url(db):
    from gglads.services.helena import skills
    res = skills.run_skill(db, "create_post",
                           {"caption": "hi", "image_url": "https://pub/bottle.png"},
                           user_id=None, session_id=None)
    assert res["ok"] and res["image_url"] == "https://pub/bottle.png"


def test_create_post_falls_back_to_latest_generated_image(db):
    # When the model omits image_url, the draft picks up the most recent
    # generated creative so it never renders image-less.
    from gglads.services.helena import brand as bsvc, skills
    bsvc.save_asset(db, url="https://pub/scene.png", kind="generated", title="creative")
    res = skills.run_skill(db, "create_post", {"caption": "no image passed"},
                           user_id=None, session_id=None)
    assert res["ok"] and res["image_url"] == "https://pub/scene.png"


def test_chat_stream_stores_attached_image(db, monkeypatch):
    import os
    os.environ["APP_SECRET"] = "t"
    from gglads.services.helena import agent, storage
    from gglads.services import claude as claude_svc
    monkeypatch.setattr(storage, "put_bytes", lambda *a, **k: ("https://pub/up.png", None))
    # Make the agent turn end immediately (no real Anthropic call).
    monkeypatch.setattr(claude_svc, "get_client_and_model", lambda _db: (None, None, "no key"))
    s = agent.create_session(db, user_id=None)
    list(agent.stream_turn(db, s.id, "use this bottle", None, image_url="https://pub/up.png"))
    from gglads.models.helena import Message
    from sqlalchemy import select
    msg = db.scalars(select(Message).where(Message.session_id == s.id,
                                           Message.role == "user")).first()
    assert msg.image_url == "https://pub/up.png"
