"""Minimal Anthropic SDK wrapper that reads creds from the integrations table."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from gglads.services import integrations as integrations_svc


def get_client_and_model(db: Session) -> tuple[Any | None, str | None, str | None]:
    """Return (anthropic_client, model, error_message)."""
    cfg = integrations_svc.get_config(db, "anthropic")
    api_key = (cfg.get("api_key") or "").strip()
    if not api_key:
        return None, None, "Anthropic API key is not configured."
    try:
        import anthropic
    except ImportError:
        return None, None, "anthropic SDK is not installed."
    model = (cfg.get("model") or "claude-opus-4-7").strip() or "claude-opus-4-7"
    return anthropic.Anthropic(api_key=api_key), model, None


def chat(
    db: Session,
    system: str,
    user_message: str,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.7,
) -> tuple[str | None, str | None]:
    """Single-turn chat. Returns (text, error)."""
    client, model, err = get_client_and_model(db)
    if err:
        return None, err
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as exc:  # noqa: BLE001 — surface any SDK error
        return None, f"{type(exc).__name__}: {exc}"
    text = ""
    for block in resp.content:
        if getattr(block, "text", None):
            text += block.text
    return text, None
