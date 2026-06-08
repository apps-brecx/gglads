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
        "description": "Generate an on-brand marketing image. For a specific product, pass "
                       "`flavor` (and `variant`) and/or `product_id`: the tool uses the user's "
                       "REAL stored bottle (product-image library or Shopify) and generates "
                       "ONLY the surrounding scene — it never invents a bottle. If no real "
                       "bottle exists it returns an error (don't claim you used one). `concept` "
                       "describes the scene/background, not the bottle.",
        "input_schema": {
            "type": "object",
            "properties": {
                "concept": {"type": "string", "description": "The scene/background to generate around the real product."},
                "flavor": {"type": "string", "description": "Flavor name to fetch the real bottle for."},
                "variant": {"type": "string", "enum": ["regular", "sugar_free"],
                            "description": "Regular or Sugar-Free."},
                "product_image_id": {"type": "integer", "description": "Exact product-image "
                                     "library entry id to use as the bottle (highest priority — "
                                     "pass this when the user @-mentioned a specific product image)."},
                "product_id": {"type": "integer", "description": "Optional Shopify product id to feature."},
                "aspect_ratio": {"type": "string", "enum": ["1:1", "9:16", "16:9"], "default": "1:1"},
                "n": {"type": "integer", "description": "Number of distinct concepts (1-4).", "default": 1},
            },
            "required": ["concept"],
        },
    },
    {
        "name": "adjust_image",
        "description": "Edit/adjust an EXISTING image the user is pointing at — fix, change, "
                       "or refine part of it while keeping the rest the same. Use this (NOT "
                       "generate_image) whenever the user selects an area of an image or asks "
                       "to tweak/fix/adjust a design you already made. Pass the exact "
                       "`image_url` being edited, a clear `instruction`, and the selected "
                       "`region` if one was provided.",
        "input_schema": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string",
                              "description": "URL of the exact image to edit (the one the user selected)."},
                "instruction": {"type": "string", "description": "What to change/fix/adjust."},
                "region": {"type": "object",
                           "description": "Optional selected area as normalized 0-1 fractions.",
                           "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                          "w": {"type": "number"}, "h": {"type": "number"}}},
            },
            "required": ["image_url", "instruction"],
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
        "name": "schedule_recurring_task",
        "description": "Schedule a natural-language instruction to run automatically on a "
                       "recurring or one-off schedule (e.g. 'every day at 3pm prepare an "
                       "Instagram post for tomorrow'). When it runs, you'll execute the "
                       "instruction; anything that publishes or spends still requires the "
                       "user's approval. Use this whenever the user asks for something on a "
                       "schedule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string",
                                "description": "What to do each time it runs, in plain language."},
                "title": {"type": "string", "description": "Short name for the task."},
                "recurrence": {"type": "string",
                               "description": "One of: once, hourly, daily, weekly. May add a "
                                              "time as daily@HH:MM (24h) or a weekday as "
                                              "weekly:mon@HH:MM.",
                               "default": "daily"},
                "at_time": {"type": "string", "description": "Optional HH:MM (24h) time of day."},
            },
            "required": ["instruction"],
        },
    },
    {
        "name": "remember",
        "description": "Save a durable fact, preference, or decision to persistent memory so "
                       "you apply it in all future chats and tasks without being re-told. "
                       "Call this whenever the user shares something worth remembering.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The thing to remember, concise."},
                "category": {"type": "string", "enum": ["preference", "fact", "decision", "general"],
                             "default": "general"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "update_brand_knowledge",
        "description": "Update the brand knowledge base from chat. Set any brand fields "
                       "(name, tone, visual_style, mood, audience, content_themes, guidelines) "
                       "and/or add a titled knowledge document. Available to every future task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "tone": {"type": "string"},
                "visual_style": {"type": "string"}, "mood": {"type": "string"},
                "audience": {"type": "string"}, "content_themes": {"type": "string"},
                "guidelines": {"type": "string"},
                "document_title": {"type": "string", "description": "Title for a new knowledge doc."},
                "document_content": {"type": "string", "description": "Body of the knowledge doc."},
            },
        },
    },
    {
        "name": "find_product_image",
        "description": "Fetch the exact product (bottle) image URL from the product library for "
                       "a flavor and variant, to use when generating or composing content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flavor": {"type": "string"},
                "variant": {"type": "string", "enum": ["regular", "sugar_free"],
                            "description": "Regular or Sugar-Free."},
            },
            "required": ["flavor"],
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
        "name": "get_instagram_post_performance",
        "description": "Read organic performance of recent Instagram posts for the connected "
                       "account — reach, impressions, likes, and comments per post. Use when "
                       "the user asks how a post or their posts performed.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer",
                                     "description": "How many recent posts (1-25).", "default": 5}},
        },
    },
    {
        "name": "get_ad_performance",
        "description": "Read LIVE Meta ad performance from the user's selected ad account, "
                       "broken down per ad, for a date range — real spend, purchases, revenue, "
                       "and ROAS. Use for any 'how are my ads doing' / spend / ROAS question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "range": {"type": "string",
                          "enum": ["today", "yesterday", "last_7d", "last_14d",
                                   "last_30d", "this_month", "last_month"],
                          "default": "last_7d"},
                "since": {"type": "string", "description": "Explicit start date YYYY-MM-DD."},
                "until": {"type": "string", "description": "Explicit end date YYYY-MM-DD."},
            },
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

def _resolve_real_bottle(db, args):
    """Find the user's REAL product bottle image: (a) the product-image library
    by flavor + Regular/Sugar-Free, else (b) the connected Shopify product image.
    Returns (url, source, mime) or (None, None, None)."""
    from gglads.models.helena import ProductImage
    from gglads.services.helena import product_library as library_svc
    # Highest priority: an exact library entry the user @-mentioned/selected.
    if args.get("product_image_id"):
        img = db.get(ProductImage, int(args["product_image_id"]))
        if img and img.url:
            return img.url, "library", (img.content_type or "image/png")
    flavor = (args.get("flavor") or "").strip()
    variant = args.get("variant")
    if flavor:
        img = library_svc.find_image(db, flavor, variant)
        if img and img.url:
            return img.url, "library", (img.content_type or "image/png")
    if args.get("product_id"):
        products = brand_svc.ShopifyProductProvider(db)
        pid = int(args["product_id"])
        urls = [i["url"] for i in products.get_product_images(pid) if i.get("url")]
        if not urls:
            prod = products.get_product(pid)
            if prod and prod.get("image_url"):
                urls = [prod["image_url"]]
        if urls:
            return urls[0], "shopify", "image/png"
    return None, None, None


def _generate_image(db, args, user_id, session_id):
    import httpx

    from gglads.services.helena import storage
    svc = GoogleFlowImageService()
    flavor = (args.get("flavor") or "").strip()
    wants_product = bool(flavor or args.get("product_id") or args.get("product_image_id"))

    # When the content is for a specific flavor/product, we MUST use the real
    # stored bottle and only generate the scene around it — never invent a bottle.
    if wants_product:
        bottle_url, source, mime = _resolve_real_bottle(db, args)
        if not bottle_url:
            who = (flavor or (f"library image #{args['product_image_id']}"
                              if args.get("product_image_id")
                              else f"product #{args.get('product_id')}"))
            return {"ok": False, "error": (
                f"I couldn't find a real bottle image for {who}. Upload it to the "
                "Product images library (labeled with flavor + Regular/Sugar-Free) or "
                "connect the Shopify product. I won't invent a fake bottle.")}
        ok, _ = storage.verify_url(bottle_url)
        if not ok:
            return {"ok": False, "error": "Your stored bottle image isn't publicly reachable; "
                    "re-upload it to the Product images library."}
        # Download the real bottle bytes and composite it into a generated scene.
        try:
            r = httpx.get(bottle_url, timeout=30.0, follow_redirects=True)
            ref_bytes = r.content if r.status_code == 200 else b""
            mime = r.headers.get("content-type", mime) or mime
        except httpx.HTTPError:
            ref_bytes = b""
        img, err = (None, "no bytes")
        if ref_bytes:
            img, err = svc.generate_with_reference(
                args["concept"], ref_bytes, ref_mime=mime,
                brand_context=brand_svc.brand_context_text(db))
        if img:
            asset = brand_svc.save_asset(
                db, url=img.url, kind="generated", title=f"{flavor or 'Product'} creative",
                prompt=args["concept"], product_id=args.get("product_id"), user_id=user_id)
            return {"ok": True, "images": [{"asset_id": asset.id, "url": img.url}],
                    "bottle_used": {"source": source, "url": bottle_url},
                    "note": f"Used your real {source} bottle image and generated only the scene."}
        # Scene compositing unavailable/failed — show the REAL bottle as-is rather
        # than a fabricated lookalike.
        asset = brand_svc.save_asset(
            db, url=bottle_url, kind="product", title=f"{flavor or 'Product'} (real image)",
            prompt=args["concept"], product_id=args.get("product_id"), user_id=user_id)
        return {"ok": True, "images": [{"asset_id": asset.id, "url": bottle_url, "fallback": True}],
                "bottle_used": {"source": source, "url": bottle_url},
                "note": (f"Showing your real {source} bottle image (couldn't generate a scene "
                         f"around it: {err}). I did not invent a bottle.")}

    # No specific product → a generic generated scene is fine (no real bottle to keep).
    prompt = ImagePrompt(
        concept=args["concept"], brand_context=brand_svc.brand_context_text(db),
        aspect_ratio=args.get("aspect_ratio", "1:1"),
        n=min(4, max(1, int(args.get("n", 1)))),
    )
    images, err = svc.generate(prompt)
    if not images and err:
        images, err = svc.generate(prompt)
    saved = []
    for img in images:
        asset = brand_svc.save_asset(db, url=img.url, kind="generated",
                                     prompt=img.prompt, user_id=user_id)
        saved.append({"asset_id": asset.id, "url": img.url})
    if not saved:
        return {"ok": False, "error": err or "Couldn't produce a usable image — please try again."}
    return {"ok": True, "images": saved, "note": err}


def _adjust_image(db, args, user_id, session_id):
    """Edit an existing image in place (optionally within a selected region),
    keeping the rest unchanged. Returns an image card like generate_image."""
    import httpx

    url = (args.get("image_url") or "").strip()
    instruction = (args.get("instruction") or "").strip()
    if not url or not instruction:
        return {"ok": False, "error": "I need both the image and what to change in it."}
    try:
        r = httpx.get(url, timeout=30.0, follow_redirects=True)
        img_bytes = r.content if r.status_code == 200 else b""
        mime = r.headers.get("content-type", "image/png") or "image/png"
    except httpx.HTTPError:
        img_bytes, mime = b"", "image/png"
    if not img_bytes:
        return {"ok": False, "error": "I couldn't load that image to edit it."}
    region = args.get("region") if isinstance(args.get("region"), dict) else None
    out, err = GoogleFlowImageService().edit_image(
        img_bytes, instruction, ref_mime=mime, region=region)
    if not out:
        return {"ok": False, "error": err or "Couldn't adjust the image — please try again."}
    asset = brand_svc.save_asset(db, url=out.url, kind="generated",
                                 title="Adjusted creative", prompt=instruction, user_id=user_id)
    return {"ok": True, "images": [{"asset_id": asset.id, "url": out.url}],
            "note": "Adjusted the image as requested — everything outside the edited area "
                    "is unchanged."}


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


def _schedule_recurring_task(db, args, user_id, session_id):
    instruction = (args.get("instruction") or "").strip()
    if not instruction:
        return {"ok": False, "error": "No instruction to schedule."}
    recurrence = (args.get("recurrence") or "daily").strip().lower()
    at_time = (args.get("at_time") or "").strip()
    if at_time and "@" not in recurrence and recurrence in ("daily", "weekly"):
        recurrence = f"{recurrence}@{at_time}"
    title = (args.get("title") or instruction)[:80]

    run_after = exec_svc.compute_next_run(recurrence, None)
    rec_store = None if recurrence in ("once", "") else recurrence
    task = exec_svc.enqueue(
        db, title=title, kind="agent_prompt",
        spec={"prompt": instruction, "title": title, "user_id": user_id},
        run_after=run_after, recurrence=rec_store, user_id=user_id,
    )
    when = run_after.isoformat() if run_after else "as soon as possible"
    return {"ok": True, "task_id": task.id, "recurrence": rec_store or "once",
            "next_run": when, "tasks_url": "/helena/tasks",
            "note": f"Scheduled '{title}' ({rec_store or 'once'}). First run: {when}. "
                    "Manage it on the Tasks page. Publishing/spending still needs approval."}


def _remember(db, args, user_id, session_id):
    from gglads.services.helena import memory as memory_svc
    item = memory_svc.add_item(
        db, content=args.get("content", ""), category=args.get("category", "general"),
        source="chat", user_id=user_id,
    )
    if item is None:
        return {"ok": False, "error": "Nothing to remember."}
    return {"ok": True, "memory_id": item.id, "memory_url": "/helena/memory",
            "note": "Saved to memory — I'll apply this going forward."}


def _update_brand_knowledge(db, args, user_id, session_id):
    fields = {k: v for k, v in args.items()
              if k in ("name", "tone", "visual_style", "mood", "audience",
                       "content_themes", "guidelines") and v}
    updated = []
    if fields:
        brand_svc.update_brand(db, fields, user_id)
        updated = list(fields.keys())
    doc = None
    if args.get("document_title"):
        doc = brand_svc.add_document(
            db, title=args["document_title"], content=args.get("document_content", ""),
            user_id=user_id,
        )
    if not updated and doc is None:
        return {"ok": False, "error": "Nothing to update."}
    return {"ok": True, "updated_fields": updated,
            "document_id": (doc.id if doc else None), "brand_url": "/helena/brand",
            "note": "Brand knowledge updated — available for every task now."}


def _find_product_image(db, args, user_id, session_id):
    from gglads.services.helena import product_library as library_svc
    img = library_svc.find_image(db, args.get("flavor", ""), args.get("variant"))
    if img is None:
        return {"ok": False, "error": "No matching product image in the library. "
                "Upload one on the Product images page."}
    return {"ok": True, "url": img.url, "image_url": img.url, "flavor": img.flavor,
            "variant": img.variant, "label": img.label}


def _create_post(db, args, user_id, session_id):
    image_url = (args.get("image_url") or "").strip() or None
    # Backstop: the model often generates the image (which saves a BrandAsset)
    # but forgets to pass its URL here, leaving the draft — and its inline
    # render — image-less. Fall back to the most recent generated/product
    # creative so a drafted post always carries its image.
    if not image_url:
        for a in brand_svc.list_assets(db):
            if a.kind in ("generated", "product") and a.url:
                image_url = a.url
                break
    post = Post(
        caption=args.get("caption", ""),
        hashtags=args.get("hashtags"),
        image_url=image_url,
        account_handle=args.get("account_handle"),
        status="draft",
        created_by_user_id=user_id,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    # Return image_url so the chat renders the drafted post's image inline.
    return {"ok": True, "post_id": post.id, "status": "draft", "image_url": post.image_url}


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


def _get_instagram_post_performance(db, args, user_id, session_id):
    from gglads.services.helena.meta.factory import get_meta_provider
    res = get_meta_provider(db).fetch_instagram_media(int(args.get("limit", 5)))
    if not res.success:
        return {"ok": False, "error": res.message}
    # Also persist so these show up on the analytics dashboard like other metrics.
    analytics_svc.ingest_metrics(db, res.metrics)
    return {"ok": True, "posts": res.steps, "count": len(res.steps),
            "analytics_url": "/helena/analytics", "note": res.message}


def _get_ad_performance(db, args, user_id, session_id):
    from gglads.services.helena import daterange
    from gglads.services.helena.meta.factory import get_meta_provider
    since, until = daterange.resolve_range(args.get("range"), args.get("since"), args.get("until"))
    res = get_meta_provider(db).fetch_ad_performance(since.isoformat(), until.isoformat())
    if not res.success:
        return {"ok": False, "error": res.message}
    # Surface on the dashboard too, like other analytics.
    analytics_svc.ingest_metrics(db, res.metrics)
    return {"ok": True, "range": f"{since.isoformat()} → {until.isoformat()}",
            "ads": res.steps, "summary": res.message, "analytics_url": "/helena/analytics"}


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
    "adjust_image": _adjust_image,
    "generate_video": _generate_video,
    "schedule_recurring_task": _schedule_recurring_task,
    "remember": _remember,
    "update_brand_knowledge": _update_brand_knowledge,
    "find_product_image": _find_product_image,
    "create_post": _create_post,
    "schedule_post": _schedule_post,
    "publish_post": _publish_post,
    "create_ad_campaign": _create_ad_campaign,
    "update_budget": _update_budget,
    "pause_campaign": _pause_campaign,
    "resume_campaign": _resume_campaign,
    "get_analytics": _get_analytics,
    "get_instagram_post_performance": _get_instagram_post_performance,
    "get_ad_performance": _get_ad_performance,
    "list_products": _list_products,
    "plan_email_campaign": _plan_email_campaign,
    "generate_email_copy": _generate_email_copy,
    "render_email_html": _render_email_html,
    "create_email_draft": _create_email_draft,
    "schedule_email": _schedule_email,
    "get_email_analytics": _get_email_analytics,
}
