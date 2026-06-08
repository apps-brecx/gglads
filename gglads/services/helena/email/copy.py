"""EmailCopyService — LLM-driven email copy with brand + product context.

Produces a subject line, preview/preheader (with A/B variants), and body copy
in the brand tone. Pulls product context from the ShopifyProductProvider. Used
by the plan_email_campaign / generate_email_copy chat skills.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from gglads.services import claude as claude_svc
from gglads.services.helena import brand as brand_svc

logger = logging.getLogger("gglads.helena.email.copy")

_SYSTEM = """You are Viktoriia, an expert email marketer writing for a Shopify \
brand. Write on-brand, concrete, benefit-led copy. No emoji unless the brand \
voice clearly uses them. Never invent prices or claims — use only the product \
facts given. Return STRICT JSON only, no prose."""


class EmailCopyService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._products = brand_svc.ShopifyProductProvider(db)

    def generate(
        self,
        *,
        goal: str,
        audience: str | None = None,
        product_ids: list[int] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        brand_ctx = brand_svc.brand_context_text(self._db)
        product_ctx = ""
        if product_ids:
            blocks = [self._products.product_context_text(pid) for pid in product_ids]
            product_ctx = "\n\n".join(b for b in blocks if b)

        user = f"""Brand context:
{brand_ctx or '(none provided)'}

Campaign goal: {goal}
Audience/segment: {audience or '(general subscribers)'}

Products to feature:
{product_ctx or '(none specified)'}

Produce JSON with this exact shape:
{{
  "subject_variants": ["...", "...", "..."],
  "preheader_variants": ["...", "..."],
  "headline": "...",
  "subhead": "...",
  "body": "2-4 short paragraphs of body copy",
  "cta": "short call to action label"
}}"""

        text, err = claude_svc.chat(self._db, system=_SYSTEM, user_message=user, max_tokens=1500)
        if err:
            return None, err
        data = _parse_json(text or "")
        if data is None:
            return None, "Could not parse copy JSON from the model."
        return data, None


def _parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        # strip markdown fences
        text = text.split("```", 2)[1] if "```" in text else text
        text = text.removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
