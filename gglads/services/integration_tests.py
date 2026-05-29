"""Live test-connection calls. Each returns (ok: bool, detail: str)."""

from typing import Any

import httpx


def test_anthropic(config: dict[str, Any]) -> tuple[bool, str]:
    api_key = (config.get("api_key") or "").strip()
    model = (config.get("model") or "claude-opus-4-7").strip()
    if not api_key:
        return False, "Missing API key."
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=5,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
        text = ""
        for block in resp.content:
            if getattr(block, "text", None):
                text = block.text
                break
        return True, f"Reached Anthropic ({model}). Reply: {text[:30]!r}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def test_shopify(config: dict[str, Any]) -> tuple[bool, str]:
    domain = (config.get("store_domain") or "").strip().rstrip("/")
    token = (config.get("admin_api_token") or "").strip()
    version = (config.get("api_version") or "2025-01").strip()
    if not domain or not token:
        return False, "Missing store domain or admin API token."
    if not domain.endswith(".myshopify.com"):
        domain = f"{domain}.myshopify.com" if "." not in domain else domain
    url = f"https://{domain}/admin/api/{version}/shop.json"
    try:
        r = httpx.get(
            url,
            headers={"X-Shopify-Access-Token": token, "Accept": "application/json"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        return False, f"Request failed: {type(exc).__name__}: {exc}"
    if r.status_code != 200:
        body = r.text[:200].replace("\n", " ")
        return False, f"HTTP {r.status_code}: {body}"
    try:
        shop = r.json().get("shop", {})
    except ValueError:
        return False, "Unexpected (non-JSON) response from Shopify."
    return True, f"Connected to {shop.get('name', '?')} ({shop.get('domain', domain)})"


def test_google_ads(config: dict[str, Any]) -> tuple[bool, str]:
    required = ["developer_token", "oauth_client_id", "oauth_client_secret", "refresh_token"]
    missing = [k for k in required if not (config.get(k) or "").strip()]
    if missing:
        return False, f"Missing fields: {', '.join(missing)}."
    try:
        from google.ads.googleads.client import GoogleAdsClient

        cfg: dict[str, Any] = {
            "developer_token": config["developer_token"].strip(),
            "client_id": config["oauth_client_id"].strip(),
            "client_secret": config["oauth_client_secret"].strip(),
            "refresh_token": config["refresh_token"].strip(),
            "use_proto_plus": True,
        }
        login_cid = (config.get("login_customer_id") or "").replace("-", "").strip()
        if login_cid:
            cfg["login_customer_id"] = login_cid
        client = GoogleAdsClient.load_from_dict(cfg)
        customer_service = client.get_service("CustomerService")
        resp = customer_service.list_accessible_customers()
        count = len(resp.resource_names)
        cust_id = (config.get("customer_id") or "").replace("-", "").strip()
        warn = ""
        if cust_id and not any(rn.endswith(f"/{cust_id}") for rn in resp.resource_names):
            warn = f" (warning: customer_id {cust_id} not in accessible list)"
        return True, f"Reached Google Ads. {count} accessible accounts.{warn}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


TESTERS = {
    "anthropic": test_anthropic,
    "shopify": test_shopify,
    "google_ads": test_google_ads,
}
