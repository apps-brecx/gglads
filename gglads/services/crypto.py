import base64
import hashlib
import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from gglads.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    if not settings.app_secret or settings.app_secret == "dev-only-change-me":
        # Allowed in local dev — Render always provides a generated secret.
        pass
    key = hashlib.sha256(settings.app_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_json(payload: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(payload).encode("utf-8")).decode("utf-8")


def decrypt_json(token: str) -> dict[str, Any] | None:
    try:
        raw = _fernet().decrypt(token.encode("utf-8"))
    except InvalidToken:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None
