"""Email campaigns admin + reusable HTML starters.

The chat agent already plans, writes, renders, and (on approval) pushes email
campaigns to Shopify Email as drafts. This module backs the Email PAGE: list
every campaign, send a rendered design to the approval queue from the UI, and
keep a library of reusable full-HTML "starters" the user can remix into a new
campaign and tweak (swap flavor / text) — by hand or in chat.

Starters are stored as EmailTemplate rows with kind='starter' (the block
renderer only knows its built-in block kinds, so these never interfere).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.email_campaign import EmailCampaign, EmailTemplate
from gglads.services import claude as claude_svc
from gglads.services.helena import brand as brand_svc

STARTER_KIND = "starter"


def list_campaigns(db: Session, limit: int = 200) -> list[EmailCampaign]:
    return list(
        db.scalars(
            select(EmailCampaign).order_by(EmailCampaign.updated_at.desc()).limit(limit)
        ).all()
    )


def list_starters(db: Session) -> list[EmailTemplate]:
    return list(
        db.scalars(
            select(EmailTemplate)
            .where(EmailTemplate.kind == STARTER_KIND)
            .order_by(EmailTemplate.created_at.desc())
        ).all()
    )


def add_starter(db: Session, *, name: str, html: str) -> EmailTemplate | None:
    name = (name or "").strip()
    html = (html or "").strip()
    if not name or not html:
        return None
    row = EmailTemplate(kind=STARTER_KIND, name=name[:120],
                        html_fragment=html, is_builtin=False)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_starter(db: Session, starter_id: int) -> None:
    row = db.get(EmailTemplate, starter_id)
    if row is not None and row.kind == STARTER_KIND:
        db.delete(row)
        db.commit()


def remix_starter(
    db: Session, starter_id: int, *, user_id: int | None = None
) -> EmailCampaign | None:
    """Create a new draft campaign seeded from a starter's HTML."""
    starter = db.get(EmailTemplate, starter_id)
    if starter is None or starter.kind != STARTER_KIND:
        return None
    camp = EmailCampaign(
        name=f"Remix — {starter.name}", subject=starter.name,
        html=starter.html_fragment, status="draft", created_by_user_id=user_id,
    )
    db.add(camp)
    db.commit()
    db.refresh(camp)
    return camp


def edit_html(db: Session, html: str, instruction: str) -> tuple[str | None, str | None]:
    """Apply a plain-language change (swap flavor, change text/colour) to an
    existing email's HTML, returning the FULL updated document."""
    if not (html or "").strip():
        return None, "This campaign has no HTML to edit yet."
    brand_ctx = brand_svc.brand_context_text(db) or "(none)"
    system = (
        "You are an expert HTML email developer. You receive an existing HTML email "
        "and an instruction. Return the COMPLETE updated HTML document and NOTHING "
        "else — no explanation, no markdown fences. Preserve the structure, inline "
        "CSS, table layout, and email-client compatibility; change ONLY what the "
        "instruction asks (e.g. swap a flavor name, edit copy, change a colour). "
        "Never invent prices or product claims.")
    user = (f"Brand context:\n{brand_ctx}\n\nInstruction: {instruction}\n\n"
            f"Current HTML:\n{html}")
    text, err = claude_svc.chat(db, system=system, user_message=user, max_tokens=8000)
    if err:
        return None, err
    out = _strip_fences(text or "")
    if "<" not in out:
        return None, "The model didn't return usable HTML — try rephrasing the change."
    return out, None


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1]
        text = text.removeprefix("html").strip()
    return text.strip()
