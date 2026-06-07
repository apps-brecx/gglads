"""Helena chat agent — session management + the LLM tool-calling loop.

The loop: build the conversation from stored Message rows, call Claude with
the skill TOOLS, execute any tool calls (which route through the providers /
approval queue), feed results back, and repeat until Claude returns a final
text answer. stream_turn() yields events so the UI can render progress live.

Reuses claude.get_client_and_model() for credentials (from the integrations
table). All actions remain backend-agnostic — skills call the provider
interfaces, never a concrete backend.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.helena import ChatSession, Message
from gglads.services import claude as claude_svc
from gglads.services.helena import brand as brand_svc
from gglads.services.helena import skills as skills_svc

logger = logging.getLogger("gglads.helena.agent")

MAX_TOOL_ROUNDS = 8

SYSTEM_TEMPLATE = """You are Helena, an AI marketing agent for a Shopify brand. \
You plan content through conversation, generate on-brand images, schedule and \
publish Instagram posts, create and manage Meta ad campaigns, manage ad spend \
with optimization recommendations, design and push email campaigns to Shopify \
Email, and report performance analytics.

Rules:
- Use the brand context and Shopify product data below. Never invent prices or \
product facts — call list_products to look them up.
- Anything that spends money or publishes/sends publicly (publish_post, \
schedule_post, create_ad_campaign, update_budget, resume_campaign, \
create_email_draft, schedule_email) is queued for the user's explicit approval; \
tell the user clearly that it's awaiting approval. Never claim something went \
live if it is only queued.
- Email campaigns are always created as drafts — never auto-send.
- Whenever the user states a durable fact, preference, or decision (how they \
like things done, audience, do's/don'ts, recurring choices), call the \
`remember` skill so you never have to be told again. If they share company / \
brand info, call `update_brand_knowledge`.
- When generating content for a specific flavor, call `find_product_image` to \
fetch the exact bottle image from the product library and use it.
- Generated images and videos are shown to the user inline automatically. Do \
NOT paste raw image/video URLs or markdown image links in your replies, and \
never present an image as a "View image" text link. If image generation fails, \
say so plainly and offer to retry — never post a link that might not load.
- Be concise and concrete. Confirm what you did and the resulting IDs.

Brand context:
{brand_context}

{memory}

{library}
"""


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Sessions + messages
# ---------------------------------------------------------------------------

def list_sessions(db: Session, limit: int = 50) -> list[ChatSession]:
    return list(
        db.scalars(
            select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)
        ).all()
    )


def create_session(db: Session, *, title: str = "New chat", channel: str = "general",
                   user_id: int | None = None) -> ChatSession:
    s = ChatSession(title=title, channel=channel, created_by_user_id=user_id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def get_session(db: Session, session_id: int) -> ChatSession | None:
    return db.get(ChatSession, session_id)


def search_sessions(db: Session, query: str, limit: int = 100) -> list[ChatSession]:
    q = select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)
    term = (query or "").strip()
    if term:
        q = q.where(ChatSession.title.ilike(f"%{term}%"))
    return list(db.scalars(q).all())


def rename_session(db: Session, session_id: int, title: str) -> ChatSession | None:
    s = db.get(ChatSession, session_id)
    if s is not None:
        s.title = (title or "").strip()[:255] or s.title
        s.updated_at = _now()
        db.commit()
        db.refresh(s)
    return s


def delete_session(db: Session, session_id: int) -> None:
    s = db.get(ChatSession, session_id)
    if s is not None:
        db.delete(s)  # messages cascade via FK ondelete=CASCADE
        db.commit()


def run_prompt(
    db: Session, prompt: str, *, user_id: int | None = None,
    title: str = "Scheduled task", channel: str = "general",
) -> int:
    """Create a session and run one full agent turn (non-streaming) for a
    stored instruction. Used by scheduled 'agent_prompt' tasks. Returns the
    session id. Publish/spend still routes through the approval queue."""
    sess = create_session(db, title=title, channel=channel, user_id=user_id)
    for _ in stream_turn(db, sess.id, prompt, user_id):
        pass  # drain the generator; side effects (messages, tasks) persist
    return sess.id


def get_messages(db: Session, session_id: int) -> list[Message]:
    return list(
        db.scalars(
            select(Message).where(Message.session_id == session_id).order_by(Message.id)
        ).all()
    )


def append_user_message(db: Session, session_id: int, text: str, user_id: int | None) -> Message:
    return _append(db, session_id, "user", text, user_id=user_id)


def append_assistant_message(db: Session, session_id: int, text: str) -> Message:
    return _append(db, session_id, "assistant", text)


def _append(db, session_id, role, content, *, tool_name=None, tool_payload=None, user_id=None) -> Message:
    m = Message(
        session_id=session_id, role=role, content=content or "",
        tool_name=tool_name,
        tool_payload_json=json.dumps(tool_payload) if tool_payload is not None else None,
        user_id=user_id,
    )
    db.add(m)
    sess = db.get(ChatSession, session_id)
    if sess is not None:
        sess.updated_at = _now()
    db.commit()
    db.refresh(m)
    return m


# ---------------------------------------------------------------------------
# Tool-calling loop (streaming)
# ---------------------------------------------------------------------------

def _history_for_api(db: Session, session_id: int) -> list[dict[str, Any]]:
    """Convert stored Messages into Anthropic message dicts (text only — tool
    rounds within a turn are handled live in stream_turn)."""
    msgs = get_messages(db, session_id)
    out: list[dict[str, Any]] = []
    for m in msgs:
        if m.role in ("user", "assistant") and m.content:
            out.append({"role": m.role, "content": m.content})
    return out


def stream_turn(
    db: Session, session_id: int, user_text: str, user_id: int | None
) -> Iterator[dict[str, Any]]:
    """Run one user turn. Yields events:
      {type: 'tool', name, args, result}
      {type: 'text', text}
      {type: 'done'} | {type: 'error', error}
    Persists the user message and the final assistant message.
    """
    append_user_message(db, session_id, user_text, user_id)
    yield {"type": "start"}  # UI shows the "working" state

    client, model, err = claude_svc.get_client_and_model(db)
    if err:
        yield {"type": "error", "error": err}
        return

    from gglads.services.helena import memory as memory_svc
    from gglads.services.helena import product_library as library_svc
    system = SYSTEM_TEMPLATE.format(
        brand_context=brand_svc.brand_context_text(db) or "(none)",
        memory=memory_svc.memory_context_text(db) or "",
        library=library_svc.library_context_text(db) or "",
    )
    messages = _history_for_api(db, session_id)

    final_text = ""
    try:
        for _round in range(MAX_TOOL_ROUNDS):
            resp = client.messages.create(
                model=model,
                max_tokens=2048,
                system=system,
                tools=skills_svc.TOOLS,
                messages=messages,
            )
            assistant_content: list[dict[str, Any]] = []
            tool_uses = []
            text_chunk = ""
            for block in resp.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    text_chunk += block.text
                    assistant_content.append({"type": "text", "text": block.text})
                elif btype == "tool_use":
                    tool_uses.append(block)
                    assistant_content.append({
                        "type": "tool_use", "id": block.id,
                        "name": block.name, "input": block.input,
                    })
            if text_chunk:
                final_text += text_chunk
                yield {"type": "text", "text": text_chunk}

            messages.append({"role": "assistant", "content": assistant_content})

            if resp.stop_reason != "tool_use" or not tool_uses:
                break

            tool_results = []
            for tu in tool_uses:
                # Announce the step before running it, so the UI shows each
                # action live as it happens.
                yield {"type": "step", "name": tu.name, "args": tu.input}
                result = skills_svc.run_skill(
                    db, tu.name, tu.input or {}, user_id=user_id, session_id=session_id
                )
                _append(db, session_id, "tool", "", tool_name=tu.name,
                        tool_payload={"args": tu.input, "result": result})
                yield {"type": "tool", "name": tu.name, "args": tu.input, "result": result}
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": json.dumps(result),
                })
            messages.append({"role": "user", "content": tool_results})
    except Exception as exc:
        logger.exception("agent turn failed")
        yield {"type": "error", "error": f"{type(exc).__name__}: {exc}"}
        return

    if final_text.strip():
        append_assistant_message(db, session_id, final_text.strip())
        # Auto-title a fresh session from the first exchange.
        sess = db.get(ChatSession, session_id)
        if sess is not None and sess.title == "New chat":
            sess.title = user_text.strip()[:60] or "New chat"
            db.commit()
    yield {"type": "done"}
