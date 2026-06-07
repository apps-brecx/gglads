"""GoogleFlowImageService — on-brand image generation via Google's Imagen.

Two real auth paths (pick whichever you configure on Render):

  1. Service account (Vertex AI) — set GOOGLE_APPLICATION_CREDENTIALS_JSON to
     the SA key JSON and GOOGLE_FLOW_PROJECT_ID (+ optional
     GOOGLE_VERTEX_LOCATION). Calls the Vertex AI Imagen `:predict` endpoint
     with a short-lived OAuth token minted from the SA.

  2. API key (Generative Language API) — set GOOGLE_FLOW_API_KEY. Calls the
     generativelanguage.googleapis.com Imagen `:predict` endpoint.

`test_connection()` performs a real, cheap auth check (token mint + model GET,
or model GET with the key) so the Integrations page only shows "Connected"
when credentials actually work. `generate()` produces images and stores them
to S3.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field

import httpx

from gglads.config import get_settings
from gglads.services.helena import storage

logger = logging.getLogger("gglads.helena.google_flow")

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_GL_BASE = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class ImagePrompt:
    concept: str
    brand_context: str = ""
    product_context: str = ""
    aspect_ratio: str = "1:1"  # 1:1 feed, 9:16 story, 16:9 hero
    n: int = 1  # number of distinct concepts
    extra: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        parts = [self.concept]
        if self.product_context:
            parts.append(f"Featured product:\n{self.product_context}")
        if self.brand_context:
            parts.append(f"Brand guidelines:\n{self.brand_context}")
        parts.extend(self.extra)
        parts.append(
            "High-quality marketing photograph, on-brand, clean composition, "
            "social-media ready."
        )
        return "\n\n".join(parts)


@dataclass
class GeneratedImage:
    url: str
    prompt: str


class GoogleFlowImageService:
    def __init__(self) -> None:
        s = get_settings()
        self._api_key = (s.google_flow_api_key or "").strip()
        self._project = (s.google_flow_project_id or "").strip()
        self._location = (s.google_vertex_location or "us-central1").strip()
        self._sa_json = (s.google_application_credentials_json or "").strip()
        self._model = (s.google_flow_image_model or "imagen-3.0-generate-002").strip()

    # ---- auth mode ----------------------------------------------------
    def auth_mode(self) -> str | None:
        """'vertex' (service account), 'apikey', or None if unconfigured."""
        if self._sa_json and self._project:
            return "vertex"
        if self._api_key:
            return "apikey"
        return None

    def is_configured(self) -> bool:
        return self.auth_mode() is not None

    def _vertex_token(self) -> tuple[str | None, str | None]:
        """Mint a short-lived OAuth token from the service account JSON."""
        try:
            import google.auth.transport.requests  # type: ignore
            from google.oauth2 import service_account  # type: ignore
        except ImportError:
            return None, "google-auth is not installed; cannot use the service account."
        try:
            info = json.loads(self._sa_json)
        except json.JSONDecodeError:
            return None, "GOOGLE_APPLICATION_CREDENTIALS_JSON is not valid JSON."
        try:
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[_SCOPE]
            )
            creds.refresh(google.auth.transport.requests.Request())
        except Exception as exc:
            return None, f"Service-account auth failed: {type(exc).__name__}: {exc}"
        return creds.token, None

    def _vertex_predict_url(self) -> str:
        return (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._location}/publishers/google/"
            f"models/{self._model}:predict"
        )

    # ---- connection test (used by the Integrations Connect flow) ------
    def test_connection(self) -> tuple[bool, str]:
        mode = self.auth_mode()
        if mode is None:
            return False, (
                "Not configured. Set GOOGLE_APPLICATION_CREDENTIALS_JSON + "
                "GOOGLE_FLOW_PROJECT_ID (service account) or GOOGLE_FLOW_API_KEY."
            )
        if mode == "vertex":
            token, err = self._vertex_token()
            if err:
                return False, err
            url = (
                f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
                f"{self._project}/locations/{self._location}/publishers/google/"
                f"models/{self._model}"
            )
            try:
                r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15.0)
            except httpx.HTTPError as exc:
                return False, f"Vertex AI request failed: {type(exc).__name__}: {exc}"
            if r.status_code == 200:
                return True, f"Vertex AI reachable (project {self._project}, model {self._model})."
            return False, f"Vertex AI HTTP {r.status_code}: {r.text[:200]}"
        # apikey
        try:
            r = httpx.get(f"{_GL_BASE}/models/{self._model}",
                          params={"key": self._api_key}, timeout=15.0)
        except httpx.HTTPError as exc:
            return False, f"Generative Language API request failed: {type(exc).__name__}: {exc}"
        if r.status_code == 200:
            return True, f"Google API key valid (model {self._model})."
        return False, f"Generative Language API HTTP {r.status_code}: {r.text[:200]}"

    # ---- generation ---------------------------------------------------
    def generate(self, prompt: ImagePrompt) -> tuple[list[GeneratedImage], str | None]:
        """Generate n concepts, store them, return ([images], error)."""
        if not self.is_configured():
            return [], (
                "Google Flow is not configured. Set GOOGLE_APPLICATION_CREDENTIALS_JSON "
                "+ GOOGLE_FLOW_PROJECT_ID (service account) or GOOGLE_FLOW_API_KEY."
            )
        if not storage.is_configured():
            return [], "S3 storage is not configured; cannot persist generated images."

        text = prompt.to_text()
        images: list[GeneratedImage] = []
        for _ in range(max(1, prompt.n)):
            raw, err = self._predict(text, prompt.aspect_ratio)
            if err:
                return images, err
            url, serr = storage.put_bytes(raw, content_type="image/png", key_prefix="helena/flow")
            if serr:
                return images, serr
            images.append(GeneratedImage(url=url, prompt=text))
        return images, None

    def _predict(self, text: str, aspect_ratio: str) -> tuple[bytes, str | None]:
        payload = {
            "instances": [{"prompt": text}],
            "parameters": {"sampleCount": 1, "aspectRatio": aspect_ratio},
        }
        mode = self.auth_mode()
        try:
            if mode == "vertex":
                token, err = self._vertex_token()
                if err:
                    return b"", err
                resp = httpx.post(
                    self._vertex_predict_url(),
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload, timeout=120.0,
                )
            else:
                resp = httpx.post(
                    f"{_GL_BASE}/models/{self._model}:predict",
                    params={"key": self._api_key},
                    json=payload, timeout=120.0,
                )
        except httpx.HTTPError as exc:
            return b"", f"Imagen request failed: {type(exc).__name__}: {exc}"
        if resp.status_code != 200:
            return b"", f"Imagen HTTP {resp.status_code}: {resp.text[:300]}"
        try:
            preds = resp.json().get("predictions", [])
            b64 = preds[0].get("bytesBase64Encoded")
        except (ValueError, IndexError, AttributeError):
            return b"", "Imagen returned an unexpected response shape."
        if not b64:
            return b"", "Imagen returned no image bytes."
        return base64.b64decode(b64), None
