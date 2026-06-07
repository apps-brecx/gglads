"""Helena's skills — the tools the chat agent can call.

Each skill performs its DB work and, crucially, routes every Meta/Instagram/
email action through the provider interfaces and the approval-gated task queue.
Money/publish/send skills DO NOT execute immediately: they create the draft
record and enqueue an approval-required ScheduledTask, then tell the user it's
waiting for approval. Read skills (get_analytics) return data inline.

TOOLS is the Anthropic tool schema list; run_skill dispatches a call.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from gglads.models.email_campaign import EmailAsset, EmailCampaign
from gglads.models.helena import MetaAdCampaign, Post
from gglads.services.helena import analytics as analytics_svc
from gglads.services.helena import brand as brand_svc
from gglads.services.helena import execution as exec_svc
from gglads.services.helena import optimization as opt_svc
from gglads.services.helena.email.copy import EmailCopyService
from gglads.services.helena.email.renderer import EmailTemplateRenderer
from gglads.services.helena.images.google_flow import GoogleFlowImageService, ImagePrompt

logger = logging.getLogger("gglads.helena.skills")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "generate_image",
        "description": "Generate on-brand marketing image concept(s) via Google Flow. "
                       "Returns image URLs. Optionally tie to a Shopify product.",
        "input_schema": {
            "type": "object",
            "properties": {
                "concept": {"type": "string", "description": "Creative concept / scene to depict."},
                "product_id": {"type": "integer", "description": "Optional Shopify product id to feature."},
                "aspect_ratio": {"type": "string", "enum": ["1:1", "9:16", "16:9"], "default": "1:1"},
                "n": {"type": "integer", "description": "Number of distinct concepts (1-4).", "default": 1},
            },
            "required": ["concept"],
        },
    },
    {
        "name": "generate_video",
        "description": "Generate a short on-brand marketing video via Veo (Google Flow). "
                       "Returns a video URL. Optionally tie to a Shopify product. "
                       "Rendering can take up to a few minutes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "concept": {"type": "string", "description": "Creative concept / scene for the video."},
                "product_id": {"type": "integer", "description": "Optional Shopify product id to feature."},
                "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"], "default": "16:9"},
            },
            "required": ["concept"],
        },
    },
    {
        "name": "create_post",
        "description": "Create an Instagram post DRAFT (image + caption + hashtags).",
        "input_schema": {
            "type": "object",
            "properties": {
                "caption": {"type": "string"},
                "hashtags": {"type": "string"},
                "image_url": {"type": "string"},
                "account_handle": {"type": "string"},
            },
            "required": ["caption"],
        },
    },
    {
        "name": "schedule_post",
        "description": "Schedule a previously-created post for a future datetime. Requires approval before it publishes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "post_id": {"type": "integer"},
                "datetime": {"type": "string", "description": "ISO 8601 datetime."},
            },
            "required": ["post_id", "datetime"],
        },
    },
    {
        "name": "publish_post",
        "description": "Publish a post now. Requires approval (publishes publicly).",
        "input_schema": {
            "type": "object",
            "properties": {"post_id": {"type": "integer"}},
            "required": ["post_id"],
        },
    },
    {
        "name": "create_ad_campaign",
        "description": "Create a Meta ad campaign DRAFT and queue it to go live. Requires approval (spends money).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "objective": {"type": "string", "default": "traffic"},
                "budget_type": {"type": "string", "enum": ["daily", "lifetime"], "default": "daily"},
                "budget_dollars": {"type": "number"},
                "audience": {"type": "string", "description": "Audience targeting description."},
                "creative_image_url": {"type": "string"},
                "creative_copy": {"type": "string"},
            },
            "required": ["name", "budget_dollars"],
        },
    },
    {
        "name": "update_budget",
        "description": "Change a campaign's budget. Requires approval (spends money).",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "budget_dollars": {"type": "number"},
            },
            "required": ["campaign_id", "budget_dollars"],
        },
    },
    {
        "name": "pause_campaign",
        "description": "Pause a running Meta campaign.",
        "input_schema": {
            "type": "object",
            "properties": {"campaign_id": {"type": "integer"}},
            "required": ["campaign_id"],
        },
    },
    {
        "name": "resume_campaign",
        "description": "Resume a paused Meta campaign. Requires approval (spends money).",
        "input_schema": {
            "type": "object",
            "properties": {"campaign_id": {"type": "integer"}},
            "required": ["campaign_id"],
        },
    },
    {
        "name": "get_analytics",
        "description": "Get current performance metrics and spend-optimization recommendations.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
        },
    },
    {
        "name": "list_products",
        "description": "List Shopify products (best sellers first) to feature in posts/ads/emails.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "plan_email_campaign",
        "description": "Plan an email campaign: goal, audience, subject/preheader variants, and content outline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "audience": {"type": "string"},
                "product_ids": {"type": "array", "items": {"type": "integer"}},
                "name": {"type": "string"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "generate_email_copy",
        "description": "Generate subject/preheader/body copy for an email campaign with brand + product context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "goal": {"type": "string"},
                "audience": {"type": "string"},
                "product_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["campaign_id"],
        },
    },
    {
        "name": "render_email_html",
        "description": "Render a campaign's block layout into final responsive, inline-CSS HTML + plain text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "product_ids": {"type": "array", "items": {"type": "integer"}},
                "hero_image_url": {"type": "string"},
            },
            "required": ["campaign_id"],
        },
    },
    {
        "name": "create_email_draft",
        "description": "Push the rendered email to Shopify Email as a DRAFT. Requires approval.",
        "input_schema": {
            "type": "object",
            "properties": {"campaign_id": {"type": "integer"}},
            "required": ["campaign_id"],
        },
    },
    {
        "name": "schedule_email",
        "description": "Schedule the email campaign in Shopify Email. Requires approval before any send.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "datetime": {"type": "string"},
            },
            "required": ["campaign_id", "datetime"],
        },
    },
    {
        "name": "get_email_analytics",
        "description": "Get email campaign metrics (sends, opens, clicks, CTR).",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
        },
    },
]


def run_skill(
    db: Session,
    name: str,
    args: dict[str, Any],
    *,
    user_id: int | None,
    session_id: int | None,
) -> dict[str, Any]:
    """Execute a skill, return a JSON-serializable result for the model."""
    try:
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"ok": False, "error": f"Unknown skill: {name}"}
        return handler(db, args, user_id, session_id)
    except Exception as exc:
        logger.exception("skill %s failed", name)
        # Roll back so a failed write (e.g. a DB error) doesn't leave the
        # session in a broken state and cascade into PendingRollbackError when
        # the agent next uses it (to persist the tool/assistant message).
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _generate_image(db, args, user_id, session_id):
    svc = GoogleFlowImageService()
    products = brand_svc.ShopifyProductProvider(db)
    product_ctx = ""
    if args.get("product_id"):
        product_ctx = products.product_context_text(int(args["product_id"]))
    prompt = ImagePrompt(
        concept=args["concept"],
        brand_context=brand_svc.brand_context_text(db),
        product_context=product_ctx,
        aspect_ratio=args.get("aspect_ratio", "1:1"),
        n=min(4, max(1, int(args.get("n", 1)))),
    )
    images, err = svc.generate(prompt)
    saved = []
    for img in images:
        asset = brand_svc.save_asset(
            db, url=img.url, kind="generated", prompt=img.prompt,
            product_id=args.get("product_id"), user_id=user_id,
        )
        saved.append({"asset_id": asset.id, "url": img.url})

    # Fallback: when Google Flow isn't configured (or returned nothing) but a
    # product was referenced, use that product's existing Shopify image so the
    # chat still shows a usable on-brand visual.
    fallback = False
    if not saved and args.get("product_id"):
        pid = int(args["product_id"])
        urls = [i["url"] for i in products.get_product_images(pid) if i.get("url")]
        if not urls:
            prod = products.get_product(pid)
            if prod and prod.get("image_url"):
                urls = [prod["image_url"]]
        for url in urls[:1]:
            asset = brand_svc.save_asset(
                db, url=url, kind="product", title="Product image (fallback)",
                prompt=args["concept"], product_id=pid, user_id=user_id,
            )
            saved.append({"asset_id": asset.id, "url": url, "fallback": True})
            fallback = True

    if not saved:
        return {"ok": False, "error": err or "No image could be generated."}
    note = err
    if fallback:
        note = ("Google Flow isn't configured — showing the product's existing "
                "Shopify image instead.")
    return {"ok": True, "images": saved, "fallback": fallback, "note": note}


def _generate_video(db, args, user_id, session_id):
    from gglads.services.helena.images.veo import VeoVideoService

    products = brand_svc.ShopifyProductProvider(db)
    concept = args["concept"]
    if args.get("product_id"):
        ctx = products.product_context_text(int(args["product_id"]))
        if ctx:
            concept = f"{concept}\n\n{ctx}"
    concept = f"{concept}\n\nBrand guidelines:\n{brand_svc.brand_context_text(db)}"

    res = VeoVideoService().generate(concept, aspect_ratio=args.get("aspect_ratio", "16:9"))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "Video generation failed.")}
    if res.get("status") == "processing":
        return {"ok": True, "status": "processing", "operation": res.get("operation"),
                "note": res.get("note")}
    asset = brand_svc.save_asset(
        db, url=res["url"], kind="generated", title="Generated video",
        prompt=args["concept"], product_id=args.get("product_id"), user_id=user_id,
    )
    return {"ok": True, "videos": [{"asset_id": asset.id, "url": res["url"]}],
            "model": res.get("model")}


def _create_post(db, args, user_id, session_id):
    post = Post(
        caption=args.get("caption", ""),
        hashtags=args.get("hashtags"),
        image_url=args.get("image_url"),
        account_handle=args.get("account_handle"),
        status="draft",
        created_by_user_id=user_id,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return {"ok": True, "post_id": post.id, "status": "draft"}


def _schedule_post(db, args, user_id, session_id):
    post = db.get(Post, int(args["post_id"]))
    if post is None:
        return {"ok": False, "error": "Post not found."}
    when = datetime.fromisoformat(args["datetime"])
    post.status = "scheduled"
    post.scheduled_at = when
    db.commit()
    task = exec_svc.enqueue(
        db, title=f"Publish IG post #{post.id}", kind="schedule_post",
        spec={"post_id": post.id, "when": when.isoformat()},
        run_after=when, user_id=user_id,
    )
    return {"ok": True, "task_id": task.id, "status": "needs_review",
            "note": "Scheduled — requires your approval before it publishes."}


def _publish_post(db, args, user_id, session_id):
    post = db.get(Post, int(args["post_id"]))
    if post is None:
        return {"ok": False, "error": "Post not found."}
    task = exec_svc.enqueue(
        db, title=f"Publish IG post #{post.id}", kind="publish_post",
        spec={"post_id": post.id}, user_id=user_id,
    )
    return {"ok": True, "task_id": task.id, "status": "needs_review",
            "note": "Queued — requires your approval before publishing."}


def _create_ad_campaign(db, args, user_id, session_id):
    camp = MetaAdCampaign(
        name=args["name"],
        objective=args.get("objective", "traffic"),
        budget_type=args.get("budget_type", "daily"),
        budget_cents=int(round(float(args["budget_dollars"]) * 100)),
        audience_json=json.dumps({"description": args.get("audience", "")}),
        creative_image_url=args.get("creative_image_url"),
        creative_copy=args.get("creative_copy"),
        status="pending_approval",
        created_by_user_id=user_id,
    )
    db.add(camp)
    db.commit()
    db.refresh(camp)
    task = exec_svc.enqueue(
        db, title=f"Launch Meta campaign '{camp.name}'", kind="create_ad_campaign",
        spec={"campaign_id": camp.id}, user_id=user_id,
    )
    return {"ok": True, "campaign_id": camp.id, "task_id": task.id, "status": "needs_review",
            "note": "Campaign drafted — requires your approval before it spends."}


def _update_budget(db, args, user_id, session_id):
    cents = int(round(float(args["budget_dollars"]) * 100))
    task = exec_svc.enqueue(
        db, title=f"Update budget for campaign #{args['campaign_id']}", kind="update_budget",
        spec={"campaign_id": int(args["campaign_id"]), "amount_cents": cents}, user_id=user_id,
    )
    return {"ok": True, "task_id": task.id, "status": "needs_review",
            "note": "Budget change queued — requires approval."}


def _pause_campaign(db, args, user_id, session_id):
    task = exec_svc.enqueue(
        db, title=f"Pause campaign #{args['campaign_id']}", kind="pause_campaign",
        spec={"campaign_id": int(args["campaign_id"])}, user_id=user_id,
    )
    return {"ok": True, "task_id": task.id, "status": "pending", "note": "Queued to pause."}


def _resume_campaign(db, args, user_id, session_id):
    task = exec_svc.enqueue(
        db, title=f"Resume campaign #{args['campaign_id']}", kind="resume_campaign",
        spec={"campaign_id": int(args["campaign_id"])}, user_id=user_id,
    )
    return {"ok": True, "task_id": task.id, "status": "needs_review",
            "note": "Resume queued — requires approval (spends money)."}


def _get_analytics(db, args, user_id, session_id):
    days = int(args.get("days", 30))
    return {
        "ok": True,
        "topline": analytics_svc.topline(db, days=days),
        "per_campaign": analytics_svc.per_campaign(db, days=days),
        "recommendations": opt_svc.recommendations(db, days=days),
    }


def _list_products(db, args, user_id, session_id):
    provider = brand_svc.ShopifyProductProvider(db)
    return {"ok": True, "products": provider.list_products(
        limit=int(args.get("limit", 20)), query=args.get("query"))}


def _plan_email_campaign(db, args, user_id, session_id):
    copy = EmailCopyService(db)
    data, err = copy.generate(
        goal=args["goal"], audience=args.get("audience"),
        product_ids=args.get("product_ids"),
    )
    camp = EmailCampaign(
        name=args.get("name") or args["goal"][:80],
        goal=args["goal"],
        audience=args.get("audience"),
        subject=(data or {}).get("subject_variants", [None])[0] if data else None,
        preheader=(data or {}).get("preheader_variants", [None])[0] if data else None,
        variants_json=json.dumps(data) if data else None,
        status="draft",
        created_by_user_id=user_id,
    )
    db.add(camp)
    db.commit()
    db.refresh(camp)
    return {"ok": err is None, "campaign_id": camp.id, "plan": data, "error": err,
            "preview_url": f"/helena/email/{camp.id}/preview"}


def _generate_email_copy(db, args, user_id, session_id):
    camp = db.get(EmailCampaign, int(args["campaign_id"]))
    if camp is None:
        return {"ok": False, "error": "Campaign not found."}
    copy = EmailCopyService(db)
    data, err = copy.generate(
        goal=args.get("goal") or camp.goal or camp.name,
        audience=args.get("audience") or camp.audience,
        product_ids=args.get("product_ids"),
    )
    if err:
        return {"ok": False, "error": err}
    camp.variants_json = json.dumps(data)
    camp.subject = data.get("subject_variants", [camp.subject])[0]
    camp.preheader = data.get("preheader_variants", [camp.preheader])[0]
    db.commit()
    return {"ok": True, "copy": data, "campaign_id": camp.id,
            "preview_url": f"/helena/email/{camp.id}/preview"}


def _render_email_html(db, args, user_id, session_id):
    camp = db.get(EmailCampaign, int(args["campaign_id"]))
    if camp is None:
        return {"ok": False, "error": "Campaign not found."}
    copy_data = json.loads(camp.variants_json) if camp.variants_json else {}
    brand = brand_svc.get_or_create_brand(db)
    palette = json.loads(brand.palette_json) if brand.palette_json else []
    renderer = EmailTemplateRenderer(brand_palette=palette)

    products = brand_svc.ShopifyProductProvider(db)
    product_dicts = []
    for pid in (args.get("product_ids") or []):
        p = products.get_product(int(pid))
        if p:
            product_dicts.append({
                "title": p["title"], "price": p.get("price_min"),
                "url": p.get("url") or "#", "image_url": p.get("image_url"),
            })

    layout: list[dict[str, Any]] = [
        {"kind": "hero", "image_url": args.get("hero_image_url"),
         "headline": copy_data.get("headline", camp.name),
         "subhead": copy_data.get("subhead", "")},
        {"kind": "text", "text": copy_data.get("body", "")},
    ]
    if len(product_dicts) == 1:
        layout.append({"kind": "single_product", "product": product_dicts[0],
                       "cta": copy_data.get("cta", "Shop now")})
    elif product_dicts:
        layout.append({"kind": "product_grid", "products": product_dicts})
    layout.append({"kind": "button", "label": copy_data.get("cta", "Shop now"),
                   "url": (product_dicts[0]["url"] if product_dicts else "#")})
    layout.append({"kind": "divider"})
    layout.append({"kind": "footer", "brand_name": brand.name})

    html, plain = renderer.render(layout, preheader=camp.preheader or "")
    camp.layout_json = json.dumps(layout)
    camp.html = html
    camp.plain_text = plain
    db.commit()
    if args.get("hero_image_url"):
        db.add(EmailAsset(campaign_id=camp.id, role="hero",
                          url=args["hero_image_url"], position=0))
        db.commit()
    return {"ok": True, "campaign_id": camp.id, "html_length": len(html),
            "preview_url": f"/helena/email/{camp.id}/preview"}


def _create_email_draft(db, args, user_id, session_id):
    task = exec_svc.enqueue(
        db, title=f"Create Shopify Email draft #{args['campaign_id']}",
        kind="create_email_draft", spec={"campaign_id": int(args["campaign_id"])},
        user_id=user_id,
    )
    cid = int(args["campaign_id"])
    return {"ok": True, "task_id": task.id, "status": "needs_review", "campaign_id": cid,
            "preview_url": f"/helena/email/{cid}/preview",
            "note": "Draft push queued — requires approval. Never auto-sends."}


def _schedule_email(db, args, user_id, session_id):
    when = datetime.fromisoformat(args["datetime"])
    task = exec_svc.enqueue(
        db, title=f"Schedule Shopify Email #{args['campaign_id']}",
        kind="schedule_email",
        spec={"campaign_id": int(args["campaign_id"]), "when": when.isoformat()},
        run_after=when, user_id=user_id,
    )
    cid = int(args["campaign_id"])
    return {"ok": True, "task_id": task.id, "status": "needs_review", "campaign_id": cid,
            "preview_url": f"/helena/email/{cid}/preview",
            "note": "Scheduled — requires approval before any send."}


def _get_email_analytics(db, args, user_id, session_id):
    days = int(args.get("days", 30))
    t = analytics_svc.topline(db, days=days)
    return {"ok": True, "email": t["email"]}


_HANDLERS = {
    "generate_image": _generate_image,
    "generate_video": _generate_video,
    "create_post": _create_post,
    "schedule_post": _schedule_post,
    "publish_post": _publish_post,
    "create_ad_campaign": _create_ad_campaign,
    "update_budget": _update_budget,
    "pause_campaign": _pause_campaign,
    "resume_campaign": _resume_campaign,
    "get_analytics": _get_analytics,
    "list_products": _list_products,
    "plan_email_campaign": _plan_email_campaign,
    "generate_email_copy": _generate_email_copy,
    "render_email_html": _render_email_html,
    "create_email_draft": _create_email_draft,
    "schedule_email": _schedule_email,
    "get_email_analytics": _get_email_analytics,
}
