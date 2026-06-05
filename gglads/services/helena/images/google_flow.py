"""GoogleFlowImageService — on-brand image generation via Google Flow
(Imagen/Veo).

Takes a structured prompt (brand context + product + creative concept),
generates one or more distinct concepts, and stores each to S3, returning
public URLs. Supports regeneration with tweaks (just call again with an
adjusted concept) and the caller can save a chosen image as a BrandAsset.

Credentials come from config (GOOGLE_FLOW_*). If Flow or storage isn't
configured the service returns a clear error instead of raising.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field

import httpx

from gglads.config import get_settings
from gglads.services.helena import storage

logger = logging.getLogger("gglads.helena.google_flow")


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
        self._api_key = s.google_flow_api_key
        self._project = s.google_flow_project_id
        self._base = (s.google_flow_base_url or "").rstrip("/")
        self._model = s.google_flow_image_model

    def is_configured(self) -> bool:
        return bool(self._api_key and self._base)

    def generate(self, prompt: ImagePrompt) -> tuple[list[GeneratedImage], str | None]:
        """Generate n concepts, store them, return ([images], error)."""
        if not self.is_configured():
            return [], "Google Flow is not configured (set GOOGLE_FLOW_* env vars)."
        if not storage.is_configured():
            return [], "S3 storage is not configured; cannot persist generated images."

        text = prompt.to_text()
        images: list[GeneratedImage] = []
        for _ in range(max(1, prompt.n)):
            raw, err = self._call_flow(text, prompt.aspect_ratio)
            if err:
                return images, err
            url, serr = storage.put_bytes(raw, content_type="image/png", key_prefix="helena/flow")
            if serr:
                return images, serr
            images.append(GeneratedImage(url=url, prompt=text))
        return images, None

    def _call_flow(self, text: str, aspect_ratio: str) -> tuple[bytes, str | None]:
        """Call the Imagen predict endpoint and return raw PNG bytes.

        Endpoint shape follows the Imagen `:predict` API. If the deployment
        uses a different Flow surface, adjust here only — callers are unaffected.
        """
        url = (
            f"{self._base}/v1/projects/{self._project}/locations/us-central1/"
            f"publishers/google/models/{self._model}:predict"
        )
        payload = {
            "instances": [{"prompt": text}],
            "parameters": {"sampleCount": 1, "aspectRatio": aspect_ratio},
        }
        try:
            resp = httpx.post(
                url,
                params={"key": self._api_key},
                json=payload,
                timeout=120.0,
            )
        except httpx.HTTPError as exc:
            return b"", f"Google Flow request failed: {type(exc).__name__}: {exc}"
        if resp.status_code != 200:
            return b"", f"Google Flow HTTP {resp.status_code}: {resp.text[:300]}"
        try:
            preds = resp.json().get("predictions", [])
            b64 = preds[0].get("bytesBase64Encoded")
        except (ValueError, IndexError, AttributeError):
            return b"", "Google Flow returned an unexpected response shape."
        if not b64:
            return b"", "Google Flow returned no image bytes."
        return base64.b64decode(b64), None
