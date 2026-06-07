"""VeoVideoService — short marketing video generation via Veo on the same
Gemini (Generative Language) API key used for images.

Veo runs as a long-running operation: POST `<model>:predictLongRunning`, then
poll the returned operation until done, then download the rendered MP4 and
store it. The model id is discovered via ListModels (any `veo*` model the key
can use), so we don't hardcode a version that 404s.

Generation is slow (tens of seconds to minutes); we poll up to
GOOGLE_FLOW_VIDEO_TIMEOUT_SECONDS and, if it isn't ready, return the operation
name so the caller can report "still rendering".
"""

from __future__ import annotations

import base64
import logging
import time

import httpx

from gglads.config import get_settings
from gglads.services.helena import storage
from gglads.services.helena.images.google_flow import _bare, gl_base, gl_list_models

logger = logging.getLogger("gglads.helena.veo")

# gRPC INTERNAL — Google's transient server error. Worth retrying.
_CODE_INTERNAL = 13
_RETRYABLE_HTTP = {500, 502, 503, 504}
_FRIENDLY_TRANSIENT = (
    "Veo had a temporary server error (gRPC code 13, INTERNAL) and didn't "
    "return a video after {attempts} attempts. This is a transient Google-side "
    "issue, not a problem with your request — please try again in a moment."
)


def _safe_json(resp) -> dict | None:
    try:
        return resp.json()
    except ValueError:
        return None


def _error_obj(body: dict | None) -> dict | None:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"]
    return None


def _is_transient_error(err: dict | None) -> bool:
    """True for gRPC code 13 / INTERNAL (transient server-side failure)."""
    if not isinstance(err, dict):
        return False
    return err.get("code") == _CODE_INTERNAL or err.get("status") == "INTERNAL"


def discover_video_model(api_key: str, preferred: str = "") -> tuple[str | None, str | None]:
    models, err = gl_list_models(api_key)
    if err:
        logger.error("Veo ListModels failed: %s", err)
        return None, err
    veo = [m for m in models if "veo" in m.get("name", "").lower()]
    # Log exactly which veo* models this key can see, with their methods.
    logger.info(
        "Veo ListModels: %d veo* model(s): %s",
        len(veo),
        [
            {"name": m.get("name"),
             "methods": m.get("supportedGenerationMethods")}
            for m in veo
        ],
    )
    if preferred:
        for m in models:
            if _bare(m.get("name", "")) == _bare(preferred):
                logger.info("Veo using preferred model: %s", m.get("name"))
                return m["name"], None
    # Prefer models that advertise the long-running predict method.
    veo.sort(
        key=lambda m: (
            "predictLongRunning" in (m.get("supportedGenerationMethods") or []),
            m.get("name", ""),
        ),
        reverse=True,
    )
    if veo:
        logger.info("Veo selected model: %s (methods=%s)",
                    veo[0].get("name"), veo[0].get("supportedGenerationMethods"))
        return veo[0]["name"], None
    names = ", ".join(_bare(m.get("name", "")) for m in models[:30])
    return None, f"No Veo video model is available to this API key. Models seen: {names or '(none)'}"


class VeoVideoService:
    def __init__(self) -> None:
        s = get_settings()
        self._api_key = (s.google_flow_api_key or "").strip()
        self._preferred = (s.google_flow_video_model or "").strip()
        self._timeout = int(s.google_flow_video_timeout_seconds or 180)
        self._retries = max(0, int(s.google_flow_video_retries or 0))

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def generate(self, prompt: str, aspect_ratio: str = "16:9") -> dict:
        """Return {ok, url?, status, model?, operation?, error?}.

        Retries the start+poll up to self._retries times with exponential
        backoff when Veo fails with a transient gRPC code 13 (INTERNAL).
        """
        if not self.is_configured():
            return {"ok": False, "status": "error",
                    "error": "Google Flow API key is not set (GOOGLE_FLOW_API_KEY)."}
        storage_err = storage.config_error()
        if storage_err:
            return {"ok": False, "status": "error", "error": storage_err}

        model, err = discover_video_model(self._api_key, self._preferred)
        if err:
            return {"ok": False, "status": "error", "error": err}

        attempts = self._retries + 1
        delay = 4  # seconds; doubles each retry (4, 8, 16, …)
        for attempt in range(1, attempts + 1):
            result = self._attempt(model, prompt, aspect_ratio)
            if result["ok"] or result.get("status") == "processing":
                return result
            if result.get("transient") and attempt < attempts:
                logger.warning(
                    "Veo transient error (code 13) on attempt %d/%d — retrying in %ds",
                    attempt, attempts, delay,
                )
                time.sleep(delay)
                delay *= 2
                continue
            # Permanent error, or out of retries.
            if result.get("transient"):
                return {"ok": False, "status": "error",
                        "error": _FRIENDLY_TRANSIENT.format(attempts=attempts),
                        "model": _bare(model)}
            return result
        return {"ok": False, "status": "error",
                "error": _FRIENDLY_TRANSIENT.format(attempts=attempts),
                "model": _bare(model)}

    def _attempt(self, model: str, prompt: str, aspect_ratio: str) -> dict:
        """One full start+poll. Returns a result dict; `transient` marks a
        retryable code-13/5xx failure."""
        op_name, err, transient = self._start(model, prompt, aspect_ratio)
        if err:
            return {"ok": False, "status": "error", "error": err, "transient": transient}
        raw, status, err, transient = self._poll(op_name)
        if status == "processing":
            return {"ok": True, "status": "processing", "operation": op_name,
                    "model": _bare(model),
                    "note": "Veo is still rendering — check back shortly."}
        if err:
            return {"ok": False, "status": "error", "error": err,
                    "operation": op_name, "transient": transient}
        url, serr = storage.put_bytes(raw, content_type="video/mp4",
                                      key_prefix="helena/veo", ext="mp4")
        if serr:
            return {"ok": False, "status": "error", "error": serr, "transient": False}
        return {"ok": True, "status": "done", "url": url, "model": _bare(model)}

    def _start(self, model: str, prompt: str, aspect_ratio: str) -> tuple[str | None, str | None, bool]:
        url = f"{gl_base()}/{model}:predictLongRunning"
        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {"aspectRatio": aspect_ratio, "sampleCount": 1},
        }
        logger.info("Veo start: POST %s:predictLongRunning aspectRatio=%s", model, aspect_ratio)
        try:
            r = httpx.post(url, params={"key": self._api_key}, json=payload, timeout=60.0)
        except httpx.HTTPError as exc:
            logger.exception("Veo start request error")
            return None, f"Veo start failed: {type(exc).__name__}: {exc}", True
        if r.status_code != 200:
            body = _safe_json(r)
            transient = r.status_code in _RETRYABLE_HTTP or _is_transient_error(_error_obj(body))
            # Log + surface the FULL response body so the exact reason is visible.
            logger.error("Veo start HTTP %s (transient=%s) for model %s. Body: %s",
                         r.status_code, transient, model, r.text)
            return None, f"Veo start HTTP {r.status_code} for {_bare(model)}: {r.text}", transient
        name = r.json().get("name")
        if not name:
            logger.error("Veo start returned no operation name. Body: %s", r.text)
            return None, f"Veo start returned no operation name. Body: {r.text}", False
        logger.info("Veo operation started: %s", name)
        return name, None, False

    def _poll(self, op_name: str) -> tuple[bytes, str, str | None, bool]:
        """Poll until done/timeout. Returns (bytes, status, error, transient)."""
        url = f"{gl_base()}/{op_name}"
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                r = httpx.get(url, params={"key": self._api_key}, timeout=30.0)
            except httpx.HTTPError as exc:
                return b"", "error", f"Veo poll failed: {type(exc).__name__}: {exc}", True
            if r.status_code != 200:
                body = _safe_json(r)
                transient = r.status_code in _RETRYABLE_HTTP or _is_transient_error(_error_obj(body))
                logger.error("Veo poll HTTP %s (transient=%s). Body: %s",
                             r.status_code, transient, r.text)
                return b"", "error", f"Veo poll HTTP {r.status_code}: {r.text}", transient
            body = r.json()
            if body.get("done"):
                err_obj = body.get("error")
                if err_obj:
                    transient = _is_transient_error(err_obj)
                    logger.error("Veo operation error (transient=%s): %s", transient, err_obj)
                    if transient:
                        return b"", "error", (
                            "Veo temporary server error (gRPC code 13, INTERNAL)."
                        ), True
                    return b"", "error", f"Veo error: {err_obj}", False
                raw, err = self._download_result(body.get("response", {}))
                return raw, ("done" if not err else "error"), err, False
            time.sleep(6)
        return b"", "processing", None, False

    def _download_result(self, response: dict) -> tuple[bytes, str | None]:
        # Veo returns generated samples with either an inline base64 video or a
        # short-lived download URI. Handle both, plus a couple of shape variants.
        samples = (
            response.get("generateVideoResponse", {}).get("generatedSamples")
            or response.get("generatedSamples")
            or response.get("videos")
            or []
        )
        for s in samples:
            video = s.get("video", s) if isinstance(s, dict) else {}
            b64 = video.get("bytesBase64Encoded") or video.get("data")
            if b64:
                try:
                    return base64.b64decode(b64), None
                except (ValueError, TypeError):
                    pass
            uri = video.get("uri") or video.get("url")
            if uri:
                try:
                    r = httpx.get(uri, params={"key": self._api_key}, timeout=120.0,
                                  follow_redirects=True)
                except httpx.HTTPError as exc:
                    return b"", f"Veo download failed: {type(exc).__name__}: {exc}"
                if r.status_code == 200 and r.content:
                    return r.content, None
                return b"", f"Veo download HTTP {r.status_code}."
        return b"", "Veo finished but returned no downloadable video."
