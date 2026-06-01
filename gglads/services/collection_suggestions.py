"""AI-suggested new collections.

Pulls all organic queries the site already ranks for (Search Console site-wide),
removes anything already covered by an existing collection, and asks Claude to
group the remainder into would-be collection themes. Each suggestion comes back
with a title, handle, target keywords, full SEO copy, rationale, and an
opportunity score (1-100, roughly weighted by impressions).

Lives behind a manual button + a weekly cron. We never create collections on
Shopify automatically — the user marks a suggestion as Created once they've
made the matching collection in Shopify admin.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from gglads.models.product_keywords import CollectionSuggestion
from gglads.models.shopify_product import ShopifyCollection
from gglads.services import claude as claude_svc
from gglads.services import search_console as sc_svc

logger = logging.getLogger("gglads.coll_suggest")


_SYSTEM = """You are a senior SEO strategist for a Shopify store. You'll be \
given a list of organic search queries the site already ranks for, plus the \
list of collections that already exist. Your job: identify high-opportunity \
themes that don't have a collection yet and propose new collections we could \
create — each one becomes its own landing page on /collections/<handle>.

Rules:
- Only suggest a collection if there are at least 3 distinct queries that share a clear theme.
- Skip themes that are already covered by an existing collection (case-insensitive match against the existing collection titles + handles).
- Theme keywords must be a list of 4-12 actual queries from the input (verbatim, not invented).
- seo_title ≤ 60 chars; seo_meta_description 140-155 chars; description_html 120-220 words of HTML using <p>, <h2>, <ul>, <li>.
- opportunity_score (1-100) weights total impressions in the theme + a bonus for low avg position (room to grow).
- 3-8 suggestions, ranked best first.

Output JSON only, exactly:
{
  "suggestions": [
    {
      "title": "Sugar-Free Fruity Syrups",
      "handle": "sugar-free-fruity-syrups",
      "theme_keywords": ["sugar free strawberry syrup", "..."],
      "seo_title": "...",
      "seo_meta_description": "...",
      "description_html": "<p>…</p><h2>…</h2><ul><li>…</li></ul>",
      "opportunity_score": 78,
      "rationale": "1-2 sentences on why this theme is worth a dedicated page."
    }
  ]
}
"""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    if start == -1:
        return None
    depth = 0
    end = -1
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None
    try:
        return json.loads(candidate[start:end])
    except json.JSONDecodeError:
        return None


def _slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:255] or "collection"


def list_existing_collection_terms(db: Session) -> set[str]:
    out: set[str] = set()
    for c in db.execute(select(ShopifyCollection)).scalars().all():
        out.add((c.title or "").lower().strip())
        out.add((c.handle or "").lower().strip())
    return {x for x in out if x}


def list_existing_pending_terms(db: Session) -> set[str]:
    """Don't re-suggest something that's already a pending suggestion."""
    out: set[str] = set()
    for s in db.execute(
        select(CollectionSuggestion).where(
            CollectionSuggestion.status == "pending"
        )
    ).scalars().all():
        out.add((s.title or "").lower().strip())
        out.add((s.handle or "").lower().strip())
    return {x for x in out if x}


def generate_suggestions(
    db: Session, *, days: int = 90, max_suggestions: int = 8
) -> tuple[bool, str, list[CollectionSuggestion]]:
    """Pull SC queries, ask Claude, persist as pending suggestions."""
    queries, err = sc_svc.get_site_queries(db, days=days, row_limit=500)
    if err:
        return False, err, []
    if not queries:
        return False, "Search Console returned no queries.", []

    existing_terms = list_existing_collection_terms(db) | list_existing_pending_terms(db)

    # Trim payload so the prompt stays manageable. Sort by impressions desc.
    queries.sort(key=lambda r: -(r.get("impressions") or 0))
    seed_lines = "\n".join(
        f"  - {r['query']}  (impressions: {r.get('impressions') or 0}, "
        f"pos: {r.get('position') or 0:.1f})"
        for r in queries[:200]
    )
    existing_lines = ", ".join(sorted(existing_terms)) or "(none)"

    prompt = (
        f"EXISTING COLLECTIONS (titles + handles, do not duplicate):\n"
        f"  {existing_lines}\n\n"
        f"ORGANIC QUERIES on the site (last {days}d, top 200 by impressions):\n"
        f"{seed_lines}\n\n"
        f"Suggest up to {max_suggestions} new collections worth creating. "
        f"Skip anything already covered above."
    )

    text, err = claude_svc.chat(
        db, system=_SYSTEM, user_message=prompt, max_tokens=4000
    )
    if err or not text:
        return False, err or "Claude returned no text.", []
    data = _extract_json(text)
    if not data:
        return False, "Claude reply was not parseable JSON.", []
    raw = data.get("suggestions") or []
    if not isinstance(raw, list):
        return False, "Claude reply missing 'suggestions' list.", []

    now = datetime.now(timezone.utc)
    saved: list[CollectionSuggestion] = []
    skipped = 0
    for s in raw:
        title = str(s.get("title") or "").strip()[:255]
        if not title:
            continue
        handle = _slugify(str(s.get("handle") or "") or title)
        if (
            handle.lower() in existing_terms
            or title.lower() in existing_terms
        ):
            skipped += 1
            continue
        theme = s.get("theme_keywords") or []
        if not isinstance(theme, list):
            theme = []
        theme = [str(x).strip().lower()[:255] for x in theme if str(x).strip()]
        if len(theme) < 3:
            skipped += 1
            continue
        score = int(s.get("opportunity_score") or 50)
        score = max(1, min(100, score))
        seo_title_clean = str(s.get("seo_title") or "").strip()[:255] or None
        suggestion = CollectionSuggestion(
            title=title,
            handle=handle,
            theme_keywords_json=json.dumps(theme),
            seo_title=seo_title_clean,
            seo_meta_description=str(s.get("seo_meta_description") or "").strip() or None,
            description_html=str(s.get("description_html") or "").strip() or None,
            rationale=str(s.get("rationale") or "").strip() or None,
            opportunity_score=score,
            status="pending",
            generated_at=now,
            updated_at=now,
        )
        db.add(suggestion)
        # Track in existing_terms so we don't double-insert within the same batch.
        existing_terms.add(title.lower())
        existing_terms.add(handle.lower())
        saved.append(suggestion)
    db.commit()
    msg = f"Generated {len(saved)} new suggestion(s)"
    if skipped:
        msg += f" (skipped {skipped} duplicate/invalid)"
    return True, msg + ".", saved


def list_pending(db: Session, limit: int = 25) -> list[CollectionSuggestion]:
    return list(
        db.execute(
            select(CollectionSuggestion)
            .where(CollectionSuggestion.status == "pending")
            .order_by(CollectionSuggestion.opportunity_score.desc())
            .limit(limit)
        ).scalars().all()
    )


def list_archived(db: Session, limit: int = 50) -> list[CollectionSuggestion]:
    return list(
        db.execute(
            select(CollectionSuggestion)
            .where(CollectionSuggestion.status.in_(("dismissed", "created")))
            .order_by(CollectionSuggestion.updated_at.desc())
            .limit(limit)
        ).scalars().all()
    )


def dismiss(db: Session, suggestion_id: int) -> tuple[bool, str]:
    s = db.get(CollectionSuggestion, suggestion_id)
    if s is None:
        return False, "Suggestion not found."
    s.status = "dismissed"
    s.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, f'Dismissed "{s.title}".'


def mark_created(db: Session, suggestion_id: int) -> tuple[bool, str]:
    s = db.get(CollectionSuggestion, suggestion_id)
    if s is None:
        return False, "Suggestion not found."
    s.status = "created"
    s.updated_at = datetime.now(timezone.utc)
    # If a collection with this handle already exists (synced from Shopify),
    # link them so we can deep-link to it.
    coll = db.scalar(
        select(ShopifyCollection).where(ShopifyCollection.handle == s.handle)
    )
    if coll is not None:
        s.created_collection_id = coll.id
    db.commit()
    return True, f'Marked "{s.title}" as created. Run a Shopify sync once the collection exists.'


def parse_keywords(s: CollectionSuggestion) -> list[str]:
    if not s.theme_keywords_json:
        return []
    try:
        v = json.loads(s.theme_keywords_json)
        if isinstance(v, list):
            return [str(x) for x in v]
    except (ValueError, TypeError):
        pass
    return []
