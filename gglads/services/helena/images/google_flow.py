"""GoogleFlowImageService — on-brand image generation via Google's Imagen/Gemini.

Two auth paths:
  1. Service account (Vertex AI) — GOOGLE_APPLICATION_CREDENTIALS_JSON +
     GOOGLE_FLOW_PROJECT_ID (+ GOOGLE_VERTEX_LOCATION). Vertex Imagen :predict.
  2. API key (Generative Language API) — GOOGLE_FLOW_API_KEY.

For the API-key path we DISCOVER a working model at runtime via the
Generative Language ListModels endpoint instead of hardcoding an id (model
ids/versions differ per account, which is what caused the
`imagen-3.0-generate-002 not found for v1beta :predict` 404). Discovery picks an
Imagen `:predict` model when available, otherwise a Gemini image
`:generateContent` model, and calls it with the matching request shape.

`test_connection()` runs the exact same discovery + generation path, so the
Integrations card only shows "Connected" when real generation works.
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

# Discovery cache: {(api_key, version, kind): (model_name, method)}
_MODEL_CACHE: dict[tuple, tuple[str, str]] = {}


def gl_base() -> str:
    s = get_settings()
    ver = (s.google_flow_api_version or "v1beta").strip()
    return f"https://generativelanguage.googleapis.com/{ver}"


def gl_list_models(api_key: str) -> tuple[list[dict], str | None]:
    """Call ListModels and return ([model dicts], error)."""
    models: list[dict] = []
    url = f"{gl_base()}/models"
    page_token = None
    try:
        for _ in range(5):  # paginate defensively
            params = {"key": api_key, "pageSize": 200}
            if page_token:
                params["pageToken"] = page_token
            r = httpx.get(url, params=params, timeout=20.0)
            if r.status_code != 200:
                return models, f"ListModels HTTP {r.status_code}: {r.text[:200]}"
            body = r.json()
            models.extend(body.get("models", []) or [])
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    except httpx.HTTPError as exc:
        return models, f"ListModels request failed: {type(exc).__name__}: {exc}"
    return models, None


def _bare(name: str) -> str:
    return name.split("/")[-1]


def choose_image_model(models: list[dict], preferred: str = "") -> tuple[str, str] | None:
    """Return (full_model_name, method) for image generation, or None.

    method is 'predict' (Imagen) or 'generateContent' (Gemini image).
    """
    def methods(m):
        return set(m.get("supportedGenerationMethods", []) or [])

    # Honor an explicit preference if it's actually available.
    if preferred:
        pref = _bare(preferred)
        for m in models:
            if _bare(m.get("name", "")) == pref:
                ms = methods(m)
                method = "predict" if "predict" in ms else (
                    "generateContent" if "generateContent" in ms else "")
                if method:
                    return m["name"], method

    imagen_predict = [
        m for m in models
        if "predict" in methods(m) and "imagen" in m.get("name", "").lower()
    ]
    if imagen_predict:
        imagen_predict.sort(key=lambda m: m["name"], reverse=True)  # newest-ish
        return imagen_predict[0]["name"], "predict"

    gemini_image = [
        m for m in models
        if "generateContent" in methods(m) and "image" in m.get("name", "").lower()
    ]
    if gemini_image:
        gemini_image.sort(key=lambda m: m["name"], reverse=True)
        return gemini_image[0]["name"], "generateContent"
    return None


def discover_image_model(api_key: str, preferred: str = "") -> tuple[str | None, str, str | None]:
    """Return (model_name, method, error). Cached per api key + version."""
    s = get_settings()
    cache_key = (api_key, s.google_flow_api_version, "image", preferred)
    if cache_key in _MODEL_CACHE:
        name, method = _MODEL_CACHE[cache_key]
        return name, method, None
    models, err = gl_list_models(api_key)
    if err:
        return None, "", err
    chosen = choose_image_model(models, preferred)
    if not chosen:
        names = ", ".join(_bare(m.get("name", "")) for m in models[:20])
        return None, "", (
            "No image-generation model is available to this API key. "
            f"Models seen: {names or '(none)'}"
        )
    _MODEL_CACHE[cache_key] = chosen
    return chosen[0], chosen[1], None


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
        self._preferred = (s.google_flow_image_model or "").strip()

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

    def _vertex_model(self) -> str:
        return self._preferred or "imagen-3.0-generate-002"

    # ---- connection test (used by the Integrations Connect flow) ------
    def test_connection(self) -> tuple[bool, str]:
        mode = self.auth_mode()
        if mode is None:
            return False, (
                "Not configured. Set GOOGLE_APPLICATION_CREDENTIALS_JSON + "
                "GOOGLE_FLOW_PROJECT_ID (service account) or GOOGLE_FLOW_API_KEY."
            )
        if mode == "apikey":
            name, method, err = discover_image_model(self._api_key, self._preferred)
            if err:
                return False, err
            label = f"{_bare(name)} via {method}"
        else:
            label = f"Vertex AI {self._vertex_model()}"
        raw, err = self._predict_bytes("A plain solid light-grey square. Connection test.", "1:1")
        if err:
            return False, err
        if not raw:
            return False, "No image returned by the model."
        return True, f"Image generation works — {label}."

    # ---- generation ---------------------------------------------------
    def generate(self, prompt: ImagePrompt) -> tuple[list[GeneratedImage], str | None]:
        """Generate n concepts, store them, return ([images], error)."""
        if not self.is_configured():
            return [], (
                "Google Flow is not configured. Set GOOGLE_APPLICATION_CREDENTIALS_JSON "
                "+ GOOGLE_FLOW_PROJECT_ID (service account) or GOOGLE_FLOW_API_KEY."
            )
        storage_err = storage.config_error()
        if storage_err:
            return [], storage_err

        text = prompt.to_text()
        images: list[GeneratedImage] = []
        for _ in range(max(1, prompt.n)):
            raw, err = self._predict_bytes(text, prompt.aspect_ratio)
            if err:
                return images, err
            url, serr = storage.put_bytes(raw, content_type="image/png", key_prefix="helena/flow")
            if serr:
                return images, serr
            images.append(GeneratedImage(url=url, prompt=text))
        return images, None

    # ---- low-level prediction ----------------------------------------
    def _predict_bytes(self, text: str, aspect_ratio: str) -> tuple[bytes, str | None]:
        mode = self.auth_mode()
        if mode == "vertex":
            return self._vertex_predict(text, aspect_ratio)
        return self._apikey_predict(text, aspect_ratio)

    def _vertex_predict(self, text: str, aspect_ratio: str) -> tuple[bytes, str | None]:
        token, err = self._vertex_token()
        if err:
            return b"", err
        url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._location}/publishers/google/"
            f"models/{self._vertex_model()}:predict"
        )
        payload = {
            "instances": [{"prompt": text}],
            "parameters": {"sampleCount": 1, "aspectRatio": aspect_ratio},
        }
        try:
            resp = httpx.post(url, headers={"Authorization": f"Bearer {token}"},
                              json=payload, timeout=120.0)
        except httpx.HTTPError as exc:
            return b"", f"Vertex Imagen request failed: {type(exc).__name__}: {exc}"
        if resp.status_code != 200:
            return b"", f"Vertex Imagen HTTP {resp.status_code}: {resp.text[:300]}"
        return _extract_predict_image(resp.json())

    def _apikey_predict(self, text: str, aspect_ratio: str) -> tuple[bytes, str | None]:
        name, method, err = discover_image_model(self._api_key, self._preferred)
        if err:
            return b"", err
        url = f"{gl_base()}/{name}:{method}"
        if method == "predict":
            payload = {
                "instances": [{"prompt": text}],
                "parameters": {"sampleCount": 1, "aspectRatio": aspect_ratio},
            }
        else:  # generateContent (Gemini image models)
            payload = {
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {"responseModalities": ["IMAGE"]},
            }
        try:
            resp = httpx.post(url, params={"key": self._api_key}, json=payload, timeout=120.0)
        except httpx.HTTPError as exc:
            return b"", f"Imagen request failed: {type(exc).__name__}: {exc}"
        if resp.status_code != 200:
            # On a stale cached model, drop the cache so the next call re-discovers.
            _MODEL_CACHE.clear()
            return b"", f"Image API HTTP {resp.status_code}: {resp.text[:300]}"
        body = resp.json()
        if method == "predict":
            return _extract_predict_image(body)
        return _extract_generatecontent_image(body)


def _extract_predict_image(body: dict) -> tuple[bytes, str | None]:
    try:
        preds = body.get("predictions", [])
        b64 = preds[0].get("bytesBase64Encoded")
    except (AttributeError, IndexError):
        return b"", "Imagen returned an unexpected response shape."
    if not b64:
        return b"", "Imagen returned no image bytes."
    return base64.b64decode(b64), None


def _extract_generatecontent_image(body: dict) -> tuple[bytes, str | None]:
    try:
        parts = body["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return b"", "Gemini image response had no candidates/parts."
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"]), None
    return b"", "Gemini image response contained no inline image data."
