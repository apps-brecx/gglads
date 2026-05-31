"""Keyword research orchestration: Claude generation + KP + Search Console."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gglads.models.product_keywords import KeywordResearchRun, ProductKeyword
from gglads.models.shopify_product import (
    ShopifyCollection,
    ShopifyProduct,
    ShopifyProductCollection,
)
from gglads.services import claude as claude_svc
from gglads.services import google_ads_keywords as kp_svc
from gglads.services import integrations as integrations_svc
from gglads.services import search_console as sc_svc
from gglads.services import seo_chat as chat_svc

logger = logging.getLogger("gglads.kw_research")


VALID_INTENTS = {"branded", "generic", "long-tail", "question", "comparison", "discount", "local"}
VALID_FUNNELS = {"awareness", "consideration", "conversion"}
VALID_MATCH = {"exact", "phrase", "broad"}
VALID_BUCKETS = {"primary", "secondary", "negative", "ignore"}


SYSTEM_PROMPT = """You are a Google Ads keyword research expert. Given a product, \
generate 30-50 highly relevant keyword candidates for Google Ads search campaigns.

CRITICAL: If the user has provided chat context with rules ("only product-specific \
keywords, no category-wide terms" / "never mention competitor X" / etc.), those \
rules OVERRIDE everything else. Apply them strictly.

For each keyword, classify:
- intent: branded | generic | long-tail | question | comparison | discount | local
- funnel: awareness | consideration | conversion
- match_type: exact | phrase | broad
- relevance_score: 0-100 (higher = more relevant to this product)
- rationale: ONE short sentence explaining why
- suggested_bucket: primary | secondary | negative

Guidelines:
- primary (5-10) = high-relevance, conversion-intent must-bids
- secondary (10-20) = worth testing, moderate relevance
- negative (5-10) = block these (wrong product, wrong intent like \"free\"/\"tutorial\", \
  competitor names if not bidding, irrelevant materials/colors)
- Skip terms that are too generic to convert (like \"shop\", \"buy stuff\")
- Include synonyms and varied phrasings of the product type
- Include 2-3 question-intent terms (awareness)
- Do not invent claims the product can't make
- Respect any banned terms or competitor restrictions in the brand training

Return JSON only, no commentary. Format exactly:
{
  "keywords": [
    {"keyword": "...", "intent": "...", "funnel": "...", "match_type": "...",
     "relevance_score": 92, "rationale": "...", "suggested_bucket": "primary"}
  ]
}
"""


def _build_product_brief(db: Session, product: ShopifyProduct) -> str:
    collection_titles = db.execute(
        select(ShopifyCollection.title)
        .join(
            ShopifyProductCollection,
            ShopifyProductCollection.collection_id == ShopifyCollection.id,
        )
        .where(ShopifyProductCollection.product_id == product.id)
    ).scalars().all()
    description_excerpt = (product.description_html or "")[:1500]
    return (
        f"Product title: {product.title}\n"
        f"Vendor: {product.vendor or '—'}\n"
        f"Product type: {product.product_type or '—'}\n"
        f"Price range: ${product.price_min or '—'} - ${product.price_max or '—'}\n"
        f"Collections: {', '.join(collection_titles) or '—'}\n"
        f"Status: {product.status}\n"
        f"Description excerpt: {description_excerpt}\n"
    )


def _existing_keywords(db: Session, product_id: int) -> list[str]:
    return db.execute(
        select(ProductKeyword.keyword)
        .where(ProductKeyword.product_id == product_id)
        .where(ProductKeyword.bucket != "ignore")
    ).scalars().all()


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object from a Claude response, even if wrapped in markdown."""
    if not text:
        return None
    # Try fenced code block first
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence_match.group(1) if fence_match else text
    # Find first balanced { ... }
    brace_start = candidate.find("{")
    if brace_start == -1:
        return None
    depth = 0
    end = -1
    for i in range(brace_start, len(candidate)):
        c = candidate[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None
    try:
        return json.loads(candidate[brace_start:end])
    except json.JSONDecodeError:
        return None


def apply_chat_to_keywords(
    db: Session, product_id: int, started_by_user_id: int | None
) -> tuple[bool, str]:
    """Re-evaluate existing product_keywords against the user's chat rules.

    Cheaper than full research — Claude only, no Keyword Planner, no Search
    Console. Useful when the user has just sent a chat message and wants the
    current keyword set rewritten to obey it. Can remove / re-bucket
    keywords but does NOT add new ones.
    """
    product = db.get(ShopifyProduct, product_id)
    if product is None:
        return False, "Product not found."

    existing = db.execute(
        select(ProductKeyword).where(ProductKeyword.product_id == product_id)
    ).scalars().all()
    if not existing:
        return False, "No keywords yet. Run full research first."

    chat_rows = chat_svc.list_context_for_product(
        db, product_id, topics=("seo", "general", "keywords")
    )
    if not chat_rows:
        return False, "No chat rules to apply. Send a chat message first."

    chat_lines = "\n".join(
        f"  [{('GLOBAL' if m.product_id is None else 'product')}/{m.role}] {m.content[:500]}"
        for m in chat_rows[-20:]
    )

    # Build a numbered list Claude can refer to by index
    rows_str = "\n".join(
        f"  {i}. \"{kw.keyword}\" | bucket={kw.bucket} | "
        f"score={kw.relevance_score or '?'} | source={kw.source}"
        for i, kw in enumerate(existing)
    )

    system = (
        "You are a Google Ads keyword reviewer. Apply the user's chat rules "
        "strictly to the keyword batch provided. For each keyword, decide an "
        "action. DO NOT invent new keywords — only process the listed ones. "
        "If a keyword obviously violates the chat rules, remove or "
        "negative-match it. Return JSON only — no commentary, no markdown.\n\n"
        "Actions:\n"
        '  - "keep"       — meets all rules, leave alone\n'
        '  - "remove"     — violates rules, delete entirely\n'
        '  - "negative"   — should be a negative match\n'
        '  - "secondary"  — keep but lower priority\n'
        '  - "primary"    — keep at top priority\n\n'
        "Format (exact JSON, one entry per keyword you receive):\n"
        '{ "decisions": [{"index": 0, "action": "keep"}, '
        '{"index": 1, "action": "remove"}, ...] }\n'
    )

    run = KeywordResearchRun(
        product_id=product_id, started_by_user_id=started_by_user_id
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    # Batch: a single Claude call can't return 200+ decisions within the token
    # budget — JSON gets truncated and unparseable. 50 per batch is a safe sweet
    # spot. Each batch is independent: a failure in one doesn't stop the others.
    BATCH_SIZE = 50
    removed = 0
    rebucketed = 0
    failed_batches: list[str] = []

    for batch_start in range(0, len(existing), BATCH_SIZE):
        batch = existing[batch_start : batch_start + BATCH_SIZE]
        rows_str = "\n".join(
            f"  {batch_start + i}. \"{kw.keyword}\" | bucket={kw.bucket} | "
            f"source={kw.source}"
            for i, kw in enumerate(batch)
        )
        user_msg = (
            f"Product: {product.title}\n"
            f"Type: {product.product_type or '—'}\n"
            f"Vendor: {product.vendor or '—'}\n\n"
            f"User chat rules (apply these strictly):\n{chat_lines}\n\n"
            f"Keyword batch {batch_start // BATCH_SIZE + 1} "
            f"(indices {batch_start}-{batch_start + len(batch) - 1}, "
            f"{len(batch)} keywords). Return one decision per index.\n"
            f"{rows_str}"
        )
        text, err = claude_svc.chat(
            db, system=system, user_message=user_msg, max_tokens=8000
        )
        if err or not text:
            failed_batches.append(
                f"batch {batch_start // BATCH_SIZE + 1}: {err or 'no response'}"
            )
            continue
        parsed = _extract_json(text)
        decisions = (parsed or {}).get("decisions") if isinstance(parsed, dict) else None
        if not decisions or not isinstance(decisions, list):
            preview = text.strip()[:160].replace("\n", " ")
            failed_batches.append(
                f"batch {batch_start // BATCH_SIZE + 1}: unparseable — "
                f"response started with: {preview!r}"
            )
            continue

        for d in decisions:
            if not isinstance(d, dict):
                continue
            try:
                idx = int(d.get("index"))
            except (TypeError, ValueError):
                continue
            if idx < batch_start or idx >= batch_start + len(batch):
                continue
            action = (d.get("action") or "").strip().lower()
            kw = existing[idx]
            if action == "remove":
                db.delete(kw)
                removed += 1
            elif action == "negative":
                if kw.bucket != "negative":
                    kw.bucket = "negative"
                    kw.updated_at = datetime.now(timezone.utc)
                    rebucketed += 1
            elif action == "secondary":
                if kw.bucket != "secondary":
                    kw.bucket = "secondary"
                    kw.updated_at = datetime.now(timezone.utc)
                    rebucketed += 1
            elif action == "primary":
                if kw.bucket != "primary":
                    kw.bucket = "primary"
                    kw.updated_at = datetime.now(timezone.utc)
                    rebucketed += 1
            # 'keep' and unknown actions → no change

        db.commit()  # flush each batch so partial progress is durable

    total = db.scalar(
        select(func.count(ProductKeyword.id))
        .where(ProductKeyword.product_id == product_id)
    ) or 0
    total_batches = (len(existing) + BATCH_SIZE - 1) // BATCH_SIZE
    succeeded_batches = total_batches - len(failed_batches)

    detail = (
        f"Applied chat rules: {removed} removed, {rebucketed} re-bucketed, "
        f"{total} keywords now total"
    )
    if failed_batches:
        detail += f" — {succeeded_batches}/{total_batches} batches succeeded"

    run.finished_at = datetime.now(timezone.utc)
    run.ok = succeeded_batches > 0  # partial success still counts
    run.sources_used = "chat_apply"
    run.keywords_added = 0
    run.keywords_total = total
    run.detail = detail
    if failed_batches:
        run.source_errors = json.dumps({"ai": " | ".join(failed_batches)[:1500]})
    db.commit()
    return run.ok, detail

    keyword = (raw.get("keyword") or "").strip().lower()
    if not keyword or len(keyword) > 250:
        return None
    intent = (raw.get("intent") or "").lower().strip()
    if intent not in VALID_INTENTS:
        intent = "generic"
    funnel = (raw.get("funnel") or "").lower().strip()
    if funnel not in VALID_FUNNELS:
        funnel = "consideration"
    match_type = (raw.get("match_type") or "").lower().strip()
    if match_type not in VALID_MATCH:
        match_type = "phrase"
    bucket = (raw.get("suggested_bucket") or "").lower().strip()
    if bucket not in VALID_BUCKETS:
        bucket = "secondary"
    try:
        score = int(raw.get("relevance_score") or 50)
    except (TypeError, ValueError):
        score = 50
    score = max(0, min(100, score))
    return {
        "keyword": keyword,
        "intent": intent,
        "funnel": funnel,
        "match_type": match_type,
        "relevance_score": score,
        "rationale": (raw.get("rationale") or "")[:500],
        "bucket": bucket,
    }


def _product_public_url(db: Session, product: ShopifyProduct) -> str | None:
    """Compose the public product URL using the Search Console site_url + handle."""
    sc_cfg = integrations_svc.get_config(db, "google_search_console")
    return sc_svc.page_url_from_site(sc_cfg.get("site_url") or "", product.handle)


def research_keywords(
    db: Session, product_id: int, started_by_user_id: int | None
) -> tuple[bool, str]:
    product = db.get(ShopifyProduct, product_id)
    if product is None:
        return False, "Product not found."

    run = KeywordResearchRun(
        product_id=product_id, started_by_user_id=started_by_user_id
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    sources_used: list[str] = []
    source_errors: dict[str, str] = {}
    candidates: dict[str, dict] = {}  # keyword → candidate

    # 1) Claude generation
    brief = _build_product_brief(db, product)
    existing = _existing_keywords(db, product_id)
    existing_str = ", ".join(existing[:50]) or "(none)"

    # Chat context: product-scoped + global ("all products") messages.
    # Anything the user typed in the floating chat is rules Claude must follow.
    chat_rows = chat_svc.list_context_for_product(
        db, product_id, topics=("seo", "general", "keywords")
    )
    if chat_rows:
        chat_lines = "\n".join(
            f"  [{('GLOBAL' if m.product_id is None else 'product')}/{m.role}] {m.content[:500]}"
            for m in chat_rows[-20:]
        )
    else:
        chat_lines = "  (no chat rules yet)"

    user_msg = (
        f"{brief}\n"
        f"Existing keywords already on this product (don't repeat exactly): {existing_str}\n\n"
        f"User chat rules (MUST be followed strictly):\n{chat_lines}\n\n"
        "Generate keyword candidates."
    )
    text, err = claude_svc.chat(
        db,
        system=SYSTEM_PROMPT,
        user_message=user_msg,
        max_tokens=6000,
    )
    claude_error = None
    if err or text is None:
        claude_error = err or "no text returned"
    else:
        parsed = _extract_json(text)
        if parsed and isinstance(parsed.get("keywords"), list):
            sources_used.append("ai")
            for item in parsed["keywords"]:
                if not isinstance(item, dict):
                    continue
                norm = _normalize_candidate(item)
                if norm is None:
                    continue
                norm["source"] = "ai"
                candidates[norm["keyword"]] = norm
        else:
            claude_error = "Claude returned unparseable response"
    if claude_error:
        source_errors["ai"] = claude_error

    # 2) Google Keyword Planner enrichment (use the top AI candidates as seeds)
    if candidates:
        seeds = sorted(candidates.values(), key=lambda c: -c["relevance_score"])
        seed_terms = [c["keyword"] for c in seeds[:10]]
        kp_rows, kp_err = kp_svc.generate_keyword_ideas(db, seed_terms)
        if kp_err is None and kp_rows is not None:
            sources_used.append("keyword_planner")
            for row in kp_rows:
                kw = (row.get("keyword") or "").lower().strip()
                if not kw:
                    continue
                existing_c = candidates.get(kw)
                if existing_c is not None:
                    existing_c["avg_monthly_searches"] = row["avg_monthly_searches"]
                    existing_c["competition"] = row["competition"]
                    existing_c["low_bid_micros"] = row["low_bid_micros"]
                    existing_c["high_bid_micros"] = row["high_bid_micros"]
                else:
                    candidates[kw] = {
                        "keyword": kw,
                        "intent": "generic",
                        "funnel": "consideration",
                        "match_type": "phrase",
                        "relevance_score": 50,
                        "rationale": "Suggested by Google Keyword Planner",
                        "bucket": "unsorted",
                        "source": "keyword_planner",
                        "avg_monthly_searches": row["avg_monthly_searches"],
                        "competition": row["competition"],
                        "low_bid_micros": row["low_bid_micros"],
                        "high_bid_micros": row["high_bid_micros"],
                    }
        else:
            source_errors["keyword_planner"] = kp_err or "no rows returned"
            logger.warning("Keyword Planner skipped: %s", kp_err)
    else:
        source_errors["keyword_planner"] = "no seed candidates (Claude failed)"

    # 3) Search Console enrichment (organic queries on this product's page)
    page_url = _product_public_url(db, product)
    if page_url:
        sc_rows, sc_err = sc_svc.get_queries_for_page(db, page_url, days=90)
        if sc_err is None and sc_rows is not None:
            sources_used.append("search_console")
            for row in sc_rows:
                kw = (row.get("query") or "").lower().strip()
                if not kw:
                    continue
                existing_c = candidates.get(kw)
                if existing_c is not None:
                    existing_c["sc_clicks"] = row["clicks"]
                    existing_c["sc_impressions"] = row["impressions"]
                    existing_c["sc_ctr"] = row["ctr"]
                    existing_c["sc_position"] = row["position"]
                else:
                    candidates[kw] = {
                        "keyword": kw,
                        "intent": "generic",
                        "funnel": "consideration",
                        "match_type": "phrase",
                        "relevance_score": 65,  # organic = proven traffic
                        "rationale": "Already drives organic traffic to this page",
                        "bucket": "unsorted",
                        "source": "search_console",
                        "sc_clicks": row["clicks"],
                        "sc_impressions": row["impressions"],
                        "sc_ctr": row["ctr"],
                        "sc_position": row["position"],
                    }
        else:
            source_errors["search_console"] = sc_err or "no rows returned"
            logger.warning("Search Console skipped: %s", sc_err)
    else:
        source_errors["search_console"] = "Search Console not configured (no site URL)"

    # 4) Persist
    added = 0
    for c in candidates.values():
        existing_row = db.scalar(
            select(ProductKeyword).where(
                ProductKeyword.product_id == product_id,
                ProductKeyword.keyword == c["keyword"],
            )
        )
        if existing_row is None:
            db.add(
                ProductKeyword(
                    product_id=product_id,
                    keyword=c["keyword"],
                    intent=c.get("intent"),
                    funnel=c.get("funnel"),
                    match_type=c.get("match_type"),
                    relevance_score=c.get("relevance_score"),
                    rationale=c.get("rationale"),
                    source=c.get("source", "ai"),
                    avg_monthly_searches=c.get("avg_monthly_searches"),
                    competition=c.get("competition"),
                    low_bid_micros=c.get("low_bid_micros"),
                    high_bid_micros=c.get("high_bid_micros"),
                    sc_clicks=c.get("sc_clicks"),
                    sc_impressions=c.get("sc_impressions"),
                    sc_ctr=c.get("sc_ctr"),
                    sc_position=c.get("sc_position"),
                    bucket=c.get("bucket", "unsorted"),
                )
            )
            added += 1
        else:
            # Update enrichment + score; do NOT clobber user-assigned bucket
            existing_row.relevance_score = c.get("relevance_score", existing_row.relevance_score)
            existing_row.rationale = c.get("rationale", existing_row.rationale)
            for key in (
                "avg_monthly_searches",
                "competition",
                "low_bid_micros",
                "high_bid_micros",
                "sc_clicks",
                "sc_impressions",
                "sc_ctr",
                "sc_position",
            ):
                v = c.get(key)
                if v is not None:
                    setattr(existing_row, key, v)
            existing_row.updated_at = datetime.now(timezone.utc)
    db.commit()

    total = db.execute(
        select(ProductKeyword).where(ProductKeyword.product_id == product_id)
    ).scalars().all()
    total_count = len(total)

    run.finished_at = datetime.now(timezone.utc)
    run.ok = bool(sources_used) and claude_error is None
    run.sources_used = ",".join(sources_used)
    run.keywords_added = added
    run.keywords_total = total_count
    run.source_errors = json.dumps(source_errors) if source_errors else None
    if not sources_used:
        run.detail = f"No sources available. See per-source errors above."
    else:
        detail = (
            f"{added} new, {total_count} total. Sources: {', '.join(sources_used)}."
        )
        if source_errors:
            detail += f" Errors on: {', '.join(source_errors.keys())}."
        run.detail = detail
    db.commit()

    return run.ok, run.detail or ""
