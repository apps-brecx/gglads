"""Chat between the user and Claude about a specific product.

Each user message becomes context for the next SEO generation. Claude's reply
is conversational — short acknowledgements + brief suggestions — not a full
SEO rewrite (that happens when the user clicks Generate).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.product_chat import ProductChatMessage
from gglads.models.shopify_product import ShopifyProduct
from gglads.services import claude as claude_svc

logger = logging.getLogger("gglads.seo_chat")


CHAT_SYSTEM = """You are an SEO + product copywriting assistant chatting with the \
brand owner about ONE specific product. The user gives you context they want you \
to remember (price changes, new features, seasonal angles, things to avoid, etc.).

Keep replies short (1-3 sentences). Acknowledge what you heard, and if it would \
change your SEO recommendation, say briefly how. Don't rewrite the full SEO copy — \
that happens when the user clicks "Generate with AI" on the SEO tab.

Tone: direct, helpful, never sycophantic. No emoji.
"""


def list_messages(
    db: Session, product_id: int, topic: str = "seo"
) -> list[ProductChatMessage]:
    return list(
        db.execute(
            select(ProductChatMessage)
            .where(ProductChatMessage.product_id == product_id)
            .where(ProductChatMessage.topic == topic)
            .order_by(ProductChatMessage.created_at)
        ).scalars().all()
    )


def send_message(
    db: Session,
    product_id: int,
    user_id: int | None,
    user_message: str,
    topic: str = "seo",
) -> tuple[bool, str]:
    user_message = (user_message or "").strip()
    if not user_message:
        return False, "Empty message."
    if len(user_message) > 4000:
        return False, "Message too long (max 4000 chars)."

    product = db.get(ShopifyProduct, product_id)
    if product is None:
        return False, "Product not found."

    # Persist the user message first so it appears even if Claude fails
    db.add(
        ProductChatMessage(
            product_id=product_id,
            topic=topic,
            role="user",
            content=user_message,
            user_id=user_id,
        )
    )
    db.commit()

    # Build chat history (oldest first) for Claude — last 20 messages
    history = db.execute(
        select(ProductChatMessage)
        .where(ProductChatMessage.product_id == product_id)
        .where(ProductChatMessage.topic == topic)
        .order_by(ProductChatMessage.created_at.desc())
        .limit(20)
    ).scalars().all()
    history = list(reversed(history))

    convo_lines = [
        f"[{m.role}] {m.content}" for m in history[:-1]  # all but the just-added user msg
    ]
    convo = "\n".join(convo_lines) if convo_lines else "(no prior messages)"

    product_brief = (
        f"Product: {product.title}\n"
        f"Vendor: {product.vendor or '—'} | Type: {product.product_type or '—'} | Status: {product.status}\n"
        f"Price: ${product.price_min or '—'}\n"
        f"Current SEO title: {product.seo_title or '(empty)'}\n"
        f"Current meta description: {product.seo_meta_description or '(empty)'}\n"
    )
    full_user = (
        f"{product_brief}\n"
        f"Conversation so far:\n{convo}\n\n"
        f"New user message:\n{user_message}"
    )

    reply, err = claude_svc.chat(
        db, system=CHAT_SYSTEM, user_message=full_user, max_tokens=600
    )
    if err or not reply:
        # Save a placeholder so the user can see something went wrong
        db.add(
            ProductChatMessage(
                product_id=product_id,
                topic=topic,
                role="assistant",
                content=f"(error: {err or 'no response'})",
                user_id=None,
            )
        )
        db.commit()
        return False, err or "Claude returned no reply."

    db.add(
        ProductChatMessage(
            product_id=product_id,
            topic=topic,
            role="assistant",
            content=reply.strip(),
            user_id=None,
        )
    )
    db.commit()
    return True, "Sent."
