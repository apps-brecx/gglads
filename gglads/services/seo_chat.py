"""Chat between the user and Claude.

Two scopes:
- Product-scoped: messages where product_id = <id>. Context for that one product.
- Global ("all products"): product_id IS NULL. Brand voice, store-wide rules.

SEO and other generations read BOTH product-scoped AND global messages so the
user can teach Claude once globally and have it apply everywhere.
"""

from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.product_chat import ProductChatMessage
from gglads.models.shopify_product import ShopifyProduct
from gglads.services import claude as claude_svc

logger = logging.getLogger("gglads.chat")


CHAT_SYSTEM = """You are an SEO + product-copywriting assistant chatting with the \
brand owner. The user gives you context they want you to remember (price changes, \
new features, seasonal angles, things to avoid, brand voice, etc.).

If the user's message is in PRODUCT scope, it's about ONE specific product they're \
viewing. If it's in ALL-PRODUCTS scope, it's a store-wide rule that should apply \
to every product Claude writes for.

Keep replies short (1-3 sentences). Acknowledge what you heard, and if it would \
change your recommendation, say briefly how. Don't rewrite copy yourself — that \
happens when the user clicks Generate on the relevant tab.

Tone: direct, helpful, never sycophantic. No emoji.
"""


def list_messages(
    db: Session, product_id: int | None, topic: str = "general"
) -> list[ProductChatMessage]:
    """Messages in a specific scope (product or global) for a topic."""
    q = (
        select(ProductChatMessage)
        .where(ProductChatMessage.topic == topic)
        .order_by(ProductChatMessage.created_at)
    )
    if product_id is None:
        q = q.where(ProductChatMessage.product_id.is_(None))
    else:
        q = q.where(ProductChatMessage.product_id == product_id)
    return list(db.execute(q).scalars().all())


def list_context_for_product(
    db: Session, product_id: int, topics: Iterable[str] = ("seo", "general", "keywords")
) -> list[ProductChatMessage]:
    """All messages relevant to generating for `product_id`:
    its own messages PLUS all global ("all products") messages, across listed topics.
    Used by SEO/ad/etc generation prompts."""
    topics_list = list(topics)
    rows = db.execute(
        select(ProductChatMessage)
        .where(ProductChatMessage.topic.in_(topics_list))
        .where(
            (ProductChatMessage.product_id == product_id)
            | (ProductChatMessage.product_id.is_(None))
        )
        .order_by(ProductChatMessage.created_at)
    ).scalars().all()
    return list(rows)


def send_message(
    db: Session,
    product_id: int | None,
    user_id: int | None,
    user_message: str,
    topic: str = "general",
) -> tuple[bool, str]:
    user_message = (user_message or "").strip()
    if not user_message:
        return False, "Empty message."
    if len(user_message) > 4000:
        return False, "Message too long (max 4000 chars)."

    product: ShopifyProduct | None = None
    if product_id is not None:
        product = db.get(ShopifyProduct, product_id)
        if product is None:
            return False, "Product not found."

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

    # Build context: history of THIS scope + topic, last 20 messages
    history = db.execute(
        select(ProductChatMessage)
        .where(ProductChatMessage.topic == topic)
        .where(
            ProductChatMessage.product_id == product_id
            if product_id is not None
            else ProductChatMessage.product_id.is_(None)
        )
        .order_by(ProductChatMessage.created_at.desc())
        .limit(20)
    ).scalars().all()
    history = list(reversed(history))
    convo_lines = [
        f"[{m.role}] {m.content}" for m in history[:-1]
    ]
    convo = "\n".join(convo_lines) if convo_lines else "(no prior messages)"

    if product is not None:
        scope_brief = (
            f"Scope: PRODUCT — {product.title}\n"
            f"Vendor: {product.vendor or '—'} | Type: {product.product_type or '—'} | Status: {product.status}\n"
            f"Price: ${product.price_min or '—'}\n"
            f"Current SEO title: {product.seo_title or '(empty)'}\n"
            f"Current meta description: {product.seo_meta_description or '(empty)'}\n"
        )
    else:
        scope_brief = (
            "Scope: ALL PRODUCTS (global brand-wide context — applies to every "
            "product Claude writes for from now on)\n"
        )

    full_user = (
        f"{scope_brief}\n"
        f"Topic: {topic}\n"
        f"Conversation so far:\n{convo}\n\n"
        f"New user message:\n{user_message}"
    )

    reply, err = claude_svc.chat(
        db, system=CHAT_SYSTEM, user_message=full_user, max_tokens=600
    )
    if err or not reply:
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
