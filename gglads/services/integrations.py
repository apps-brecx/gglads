"""Integration config storage. Encrypted at rest, env vars as fallback."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.config import get_settings
from gglads.models.integration import Integration
from gglads.services.crypto import decrypt_json, encrypt_json


# Field definitions: (key, label, is_secret)
INTEGRATION_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    "anthropic": [
        ("api_key", "API key", True),
        ("model", "Model", False),
    ],
    "shopify": [
        ("store_domain", "Store domain", False),
        ("admin_api_token", "Admin API access token", True),
        ("api_version", "API version", False),
    ],
    "google_ads": [
        ("customer_id", "Customer ID", False),
        ("login_customer_id", "Login customer ID", False),
        ("developer_token", "Developer token", True),
        ("oauth_client_id", "OAuth client ID", True),
        ("oauth_client_secret", "OAuth client secret", True),
        ("refresh_token", "Refresh token", True),
    ],
    "google_search_console": [
        ("site_url", "Site URL", False),
        ("oauth_client_id", "OAuth client ID", True),
        ("oauth_client_secret", "OAuth client secret", True),
        ("refresh_token", "Refresh token", True),
    ],
    "smtp": [
        ("host", "SMTP host", False),
        ("port", "SMTP port", False),
        ("username", "SMTP username", False),
        ("password", "SMTP password / API key", True),
        ("from_email", "From address", False),
        ("from_name", "From name", False),
        ("use_tls", "Use TLS (yes/no)", False),
    ],
}


def _env_fallback(name: str) -> dict[str, Any]:
    s = get_settings()
    if name == "anthropic":
        return {"api_key": s.anthropic_api_key, "model": s.anthropic_model}
    if name == "shopify":
        return {
            "store_domain": s.shopify_store_domain,
            "admin_api_token": s.shopify_admin_api_token,
            "api_version": s.shopify_api_version,
        }
    if name == "google_ads":
        return {
            "customer_id": s.google_ads_customer_id,
            "login_customer_id": s.google_ads_login_customer_id,
            "developer_token": s.google_ads_developer_token,
            "oauth_client_id": s.google_ads_client_id,
            "oauth_client_secret": s.google_ads_client_secret,
            "refresh_token": s.google_ads_refresh_token,
        }
    if name == "google_search_console":
        return {
            "site_url": "",
            "oauth_client_id": "",
            "oauth_client_secret": "",
            "refresh_token": "",
        }
    if name == "smtp":
        return {
            "host": "",
            "port": "",
            "username": "",
            "password": "",
            "from_email": "",
            "from_name": "",
            "use_tls": "yes",
        }
    return {}


def get_row(db: Session, name: str) -> Integration | None:
    return db.get(Integration, name)


def get_config(db: Session, name: str) -> dict[str, Any]:
    """Return the decrypted DB config for an integration, falling back to env vars."""
    row = get_row(db, name)
    if row is not None:
        decrypted = decrypt_json(row.config_encrypted)
        if decrypted is not None:
            return decrypted
    return _env_fallback(name)


def save_config(
    db: Session,
    name: str,
    incoming: dict[str, Any],
    user_id: int | None,
) -> Integration:
    """Upsert integration config.

    Empty values for secret fields are dropped (keep existing). Empty values
    for non-secret fields overwrite to empty.
    """
    if name not in INTEGRATION_FIELDS:
        raise ValueError(f"unknown integration: {name}")

    existing = get_config(db, name)
    merged: dict[str, Any] = dict(existing)
    for key, _label, is_secret in INTEGRATION_FIELDS[name]:
        new_val = (incoming.get(key) or "").strip()
        if is_secret:
            if new_val:
                merged[key] = new_val
            # else keep existing
        else:
            merged[key] = new_val

    row = get_row(db, name)
    if row is None:
        row = Integration(
            name=name,
            config_encrypted=encrypt_json(merged),
            updated_by_user_id=user_id,
        )
        db.add(row)
    else:
        row.config_encrypted = encrypt_json(merged)
        row.updated_by_user_id = user_id
        row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


def record_test(
    db: Session,
    name: str,
    ok: bool,
    detail: str,
) -> None:
    row = get_row(db, name)
    if row is None:
        return
    row.last_tested_at = datetime.now(timezone.utc)
    row.last_test_ok = ok
    row.last_test_detail = detail[:1000]
    db.commit()


def delete_config(db: Session, name: str) -> None:
    row = get_row(db, name)
    if row is not None:
        db.delete(row)
        db.commit()


def is_configured(config: dict[str, Any], required_keys: list[str]) -> bool:
    return all((config.get(k) or "").strip() for k in required_keys)


def required_keys(name: str) -> list[str]:
    """The minimum set of fields that must be present to consider an integration usable."""
    if name == "anthropic":
        return ["api_key"]
    if name == "shopify":
        return ["store_domain", "admin_api_token"]
    if name == "google_ads":
        return [
            "developer_token",
            "oauth_client_id",
            "oauth_client_secret",
            "refresh_token",
            "customer_id",
        ]
    if name == "google_search_console":
        return ["site_url", "oauth_client_id", "oauth_client_secret", "refresh_token"]
    if name == "smtp":
        return ["host", "port", "from_email"]
    return []


def summarize_for_form(db: Session, name: str) -> dict[str, Any]:
    """Return a dict the connections template can render:

    - non-secret fields: actual current value
    - secret fields: empty string + a flag indicating whether one is stored
    - meta: last_tested_at, last_test_ok, last_test_detail, is_configured
    """
    row = get_row(db, name)
    config = get_config(db, name)
    fields: dict[str, dict[str, Any]] = {}
    for key, _label, is_secret in INTEGRATION_FIELDS[name]:
        val = config.get(key) or ""
        if is_secret:
            fields[key] = {"value": "", "is_secret": True, "has_stored": bool(val)}
        else:
            fields[key] = {"value": val, "is_secret": False, "has_stored": bool(val)}
    return {
        "fields": fields,
        "is_configured": is_configured(config, required_keys(name)),
        "last_tested_at": row.last_tested_at if row else None,
        "last_test_ok": row.last_test_ok if row else None,
        "last_test_detail": row.last_test_detail if row else None,
        "from_db": row is not None,
    }
