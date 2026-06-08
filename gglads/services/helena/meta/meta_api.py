"""MetaApiProvider — official Instagram Graph API + Marketing API backend.

Activated when META_EXECUTION_MODE=api and a Meta connection exists (set up via
the Facebook-Login OAuth flow in meta/oauth.py). Reads the stored long-lived
token + linked IG business account + ad account from the 'meta' integration.

Safety: ad campaigns are always created PAUSED — Helena never auto-spends; the
campaign goes live only when explicitly resumed (through the approval-gated
queue). Instagram posts are published only when the publish task is approved.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from gglads.services.helena.meta.oauth import get_meta_config, graph_base
from gglads.services.helena.meta.provider import MetaExecutionProvider
from gglads.services.helena.specs import (
    CampaignSpec,
    DateRange,
    InstagramPostSpec,
    ProviderResult,
)

logger = logging.getLogger("gglads.helena.meta.api")

_OBJECTIVE = {
    "traffic": "OUTCOME_TRAFFIC", "awareness": "OUTCOME_AWARENESS",
    "engagement": "OUTCOME_ENGAGEMENT", "leads": "OUTCOME_LEADS",
    "sales": "OUTCOME_SALES", "conversions": "OUTCOME_SALES",
    "app": "OUTCOME_APP_PROMOTION",
}


class MetaApiProvider(MetaExecutionProvider):
    backend = "api"

    def __init__(self, db: Session) -> None:
        self._db = db
        cfg = get_meta_config(db)
        self._token = cfg.get("access_token")
        self._ig_user_id = cfg.get("ig_user_id")
        self._ad_account_id = cfg.get("ad_account_id")

    def _not_connected(self, what: str) -> ProviderResult:
        return ProviderResult(
            success=False,
            message=(f"Meta API not connected for {what}. Connect Instagram/Meta on the "
                     "Integrations page (official API), then try again."),
        )

    def _post(self, path: str, data: dict) -> tuple[dict | None, str | None]:
        data = {**data, "access_token": self._token}
        try:
            r = httpx.post(f"{graph_base()}/{path}", data=data, timeout=60.0)
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:400]}"
        return r.json(), None

    def _get(self, path: str, params: dict) -> tuple[dict | None, str | None]:
        params = {**params, "access_token": self._token}
        try:
            r = httpx.get(f"{graph_base()}/{path}", params=params, timeout=60.0)
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:400]}"
        return r.json(), None

    # ---- Ad campaigns (Marketing API) ---------------------------------
    def create_campaign(self, spec: CampaignSpec) -> ProviderResult:
        if not self._token or not self._ad_account_id:
            return self._not_connected("ads")
        data = {
            "name": spec.name,
            "objective": _OBJECTIVE.get(spec.objective, "OUTCOME_TRAFFIC"),
            "status": "PAUSED",  # never auto-spend; resume to go live
            "special_ad_categories": "[]",
        }
        if spec.budget_type == "daily" and spec.budget_cents > 0:
            data["daily_budget"] = spec.budget_cents
            data["bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
        body, err = self._post(f"act_{self._ad_account_id}/campaigns", data)
        if err:
            return ProviderResult(success=False, message=f"Campaign create failed: {err}")
        return ProviderResult(
            success=True, external_id=body.get("id"),
            message="Campaign created (PAUSED). Resume it to start spending.",
        )

    def update_budget(self, campaign_id: str, amount_cents: int) -> ProviderResult:
        if not self._token:
            return self._not_connected("ads")
        _body, err = self._post(campaign_id, {"daily_budget": amount_cents})
        return (ProviderResult(success=False, message=f"Budget update failed: {err}")
                if err else ProviderResult(success=True, external_id=campaign_id,
                                           message="Budget updated."))

    def pause_campaign(self, campaign_id: str) -> ProviderResult:
        if not self._token:
            return self._not_connected("ads")
        _body, err = self._post(campaign_id, {"status": "PAUSED"})
        return (ProviderResult(success=False, message=f"Pause failed: {err}")
                if err else ProviderResult(success=True, external_id=campaign_id,
                                           message="Campaign paused."))

    def resume_campaign(self, campaign_id: str) -> ProviderResult:
        if not self._token:
            return self._not_connected("ads")
        _body, err = self._post(campaign_id, {"status": "ACTIVE"})
        return (ProviderResult(success=False, message=f"Resume failed: {err}")
                if err else ProviderResult(success=True, external_id=campaign_id,
                                           message="Campaign is now active."))

    # ---- Instagram posts (Graph API) ----------------------------------
    def publish_instagram_post(self, post: InstagramPostSpec) -> ProviderResult:
        if not self._token or not self._ig_user_id:
            return self._not_connected("Instagram")
        if not post.image_url:
            return ProviderResult(success=False, message="An image URL is required to publish.")
        caption = post.caption or ""
        if post.hashtags:
            caption = f"{caption}\n\n{post.hashtags}".strip()
        steps = []
        container, err = self._post(f"{self._ig_user_id}/media",
                                    {"image_url": post.image_url, "caption": caption})
        if err:
            return ProviderResult(success=False, message=f"Media container failed: {err}")
        creation_id = container.get("id")
        steps.append({"step": "create_container", "id": creation_id})
        published, err = self._post(f"{self._ig_user_id}/media_publish",
                                    {"creation_id": creation_id})
        if err:
            return ProviderResult(success=False, message=f"Publish failed: {err}", steps=steps)
        media_id = published.get("id")
        steps.append({"step": "publish", "id": media_id})
        permalink = None
        info, perr = self._get(media_id, {"fields": "permalink"})
        if not perr and info:
            permalink = info.get("permalink")
        return ProviderResult(success=True, external_id=media_id, permalink=permalink,
                              message="Published to Instagram.", steps=steps)

    def schedule_post(self, post: InstagramPostSpec, when: datetime) -> ProviderResult:
        # The Graph API has no native future-scheduling for content publish; our
        # task queue already holds the post until `when` and runs it then, so at
        # run time this is simply a publish.
        return self.publish_instagram_post(post)

    # ---- Read-back ----------------------------------------------------
    def fetch_campaign_metrics(self, date_range: DateRange) -> ProviderResult:
        if not self._token or not self._ad_account_id:
            return self._not_connected("ads")
        tr = f'{{"since":"{date_range.start.date()}","until":"{date_range.end.date()}"}}'
        body, err = self._get(f"act_{self._ad_account_id}/insights", {
            "level": "account", "fields": "spend,impressions,clicks,actions,action_values",
            "time_range": tr,
        })
        if err:
            return ProviderResult(success=False, message=f"Insights failed: {err}")
        metrics = []
        for row in (body.get("data") or []):
            for m in ("spend", "impressions", "clicks"):
                if row.get(m) is not None:
                    metrics.append(_metric("meta_ads", m, row[m], date_range.end))
            conv = _sum_actions(row.get("actions"), "purchase")
            rev = _sum_actions(row.get("action_values"), "purchase")
            if conv:
                metrics.append(_metric("meta_ads", "conversions", conv, date_range.end))
            if rev:
                metrics.append(_metric("meta_ads", "revenue", rev, date_range.end))
        return ProviderResult(success=True, metrics=metrics,
                              message=f"Pulled {len(metrics)} ad metrics.")

    def fetch_ad_performance(self, since, until) -> ProviderResult:
        """Live per-ad insights from the SELECTED ad account for an explicit
        date range (e.g. yesterday). Returns per-ad rows in .steps and account
        totals in .metrics."""
        if not self._token or not self._ad_account_id:
            return self._not_connected("ads")
        tr = f'{{"since":"{since}","until":"{until}"}}'
        body, err = self._get(f"act_{self._ad_account_id}/insights", {
            "level": "ad",
            "fields": "ad_id,ad_name,campaign_name,spend,impressions,clicks,"
                      "actions,action_values,purchase_roas",
            "time_range": tr, "limit": 200,
        })
        if err:
            return ProviderResult(success=False,
                                  message=f"Ad performance failed for act_{self._ad_account_id}: {err}")
        ads: list[dict] = []
        tot_spend = tot_impr = tot_clicks = tot_purch = tot_rev = 0.0
        for row in (body.get("data") or []):
            spend = float(row.get("spend") or 0)
            impr = float(row.get("impressions") or 0)
            clicks = float(row.get("clicks") or 0)
            purch = _sum_actions(row.get("actions"), "purchase")
            rev = _sum_actions(row.get("action_values"), "purchase")
            roas = (rev / spend) if spend else 0.0
            ads.append({
                "ad_id": row.get("ad_id"), "ad_name": row.get("ad_name"),
                "campaign": row.get("campaign_name"),
                "spend": round(spend, 2), "impressions": int(impr), "clicks": int(clicks),
                "purchases": round(purch, 2), "revenue": round(rev, 2), "roas": round(roas, 2),
            })
            tot_spend += spend; tot_impr += impr; tot_clicks += clicks
            tot_purch += purch; tot_rev += rev
        ads.sort(key=lambda a: a["spend"], reverse=True)
        end = datetime.fromisoformat(f"{until}T00:00:00+00:00")
        metrics = [
            {"platform": "meta_ads", "entity_type": "account", "entity_id": None,
             "metric": m, "value": v, "captured_for": end.isoformat()}
            for m, v in (("spend", tot_spend), ("impressions", tot_impr),
                         ("clicks", tot_clicks), ("conversions", tot_purch),
                         ("revenue", tot_rev))
        ]
        roas = round(tot_rev / tot_spend, 2) if tot_spend else 0.0
        return ProviderResult(
            success=True, steps=ads, metrics=metrics,
            message=(f"act_{self._ad_account_id} {since}→{until}: "
                     f"${tot_spend:,.2f} spend · {int(tot_purch)} purchases · "
                     f"${tot_rev:,.2f} revenue · {roas}x ROAS across {len(ads)} ad(s)."),
        )

    def fetch_ads_breakdown(self, since, until) -> dict:
        """Full Meta ad analytics for an explicit date range: per-campaign and
        per-ad rows with every derived metric (CTR, CPC, CPM, cost-per-purchase,
        ROAS, reach, frequency), plus account totals + currency. Powers the
        Meta Ads analytics page. Returns a plain dict (not a ProviderResult)
        because the page needs richer structure than the metric list."""
        if not self._token or not self._ad_account_id:
            return {"ok": False, "error": "Meta isn't connected for ads. Connect it on the "
                    "Integrations page and pick an ad account."}
        tr = f'{{"since":"{since}","until":"{until}"}}'
        base_fields = ("spend,impressions,clicks,reach,frequency,"
                       "actions,action_values,purchase_roas")

        def _level(level: str, id_fields: str) -> tuple[list[dict] | None, str | None]:
            body, err = self._get(f"act_{self._ad_account_id}/insights", {
                "level": level, "fields": f"{id_fields},{base_fields}",
                "time_range": tr, "limit": 500,
            })
            if err:
                return None, err
            return (body.get("data") or []), None

        camp_raw, err = _level("campaign", "campaign_id,campaign_name")
        if err:
            return {"ok": False, "error": f"Campaign insights failed for "
                    f"act_{self._ad_account_id}: {err}"}
        ad_raw, ad_err = _level("ad", "ad_id,ad_name,campaign_name")
        if ad_err:  # ad-level detail is supplementary — don't fail the whole page
            ad_raw = []

        campaigns = []
        for r in camp_raw:
            row = _ad_row(r)
            row.update({"id": r.get("campaign_id"),
                        "name": r.get("campaign_name") or "(unnamed campaign)"})
            campaigns.append(row)
        ads = []
        for r in ad_raw:
            row = _ad_row(r)
            row.update({"id": r.get("ad_id"), "name": r.get("ad_name") or "(unnamed ad)",
                        "campaign": r.get("campaign_name")})
            ads.append(row)

        # Best-effort: enrich campaigns with delivery status + configured budget.
        meta = self._campaign_meta_map()
        for c in campaigns:
            m = meta.get(c["id"]) or {}
            c["status"] = m.get("status")
            c["daily_budget"] = m.get("daily_budget")

        campaigns.sort(key=lambda x: x["spend"], reverse=True)
        ads.sort(key=lambda x: x["spend"], reverse=True)
        return {"ok": True, "campaigns": campaigns, "ads": ads,
                "totals": _totals(campaigns), "currency": self._account_currency(),
                "range": f"{since} → {until}"}

    def _campaign_meta_map(self) -> dict[str, dict]:
        """campaign_id → {status, daily_budget} for context (best-effort)."""
        body, err = self._get(f"act_{self._ad_account_id}/campaigns", {
            "fields": "id,name,effective_status,status,daily_budget,lifetime_budget",
            "limit": 500,
        })
        out: dict[str, dict] = {}
        if err or not body:
            return out
        for c in (body.get("data") or []):
            raw = c.get("daily_budget") or c.get("lifetime_budget")
            try:  # budgets ARE in minor units (cents) — unlike insights money fields
                budget = round(float(raw) / 100, 2) if raw else None
            except (TypeError, ValueError):
                budget = None
            raw_status = c.get("effective_status") or c.get("status") or ""
            out[c.get("id")] = {
                "status": raw_status.replace("_", " ").title(),
                "daily_budget": budget,
            }
        return out

    def _account_currency(self) -> str:
        body, err = self._get(f"act_{self._ad_account_id}", {"fields": "currency"})
        if err or not body:
            return "USD"
        return body.get("currency") or "USD"

    def fetch_instagram_insights(self, date_range: DateRange) -> ProviderResult:
        if not self._token or not self._ig_user_id:
            return self._not_connected("Instagram")
        body, err = self._get(f"{self._ig_user_id}/insights", {
            "metric": "reach,impressions", "period": "day",
            "since": int(date_range.start.timestamp()),
            "until": int(date_range.end.timestamp()),
        })
        if err:
            return ProviderResult(success=False, message=f"IG insights failed: {err}")
        metrics = []
        for series in (body.get("data") or []):
            name = series.get("name")
            for v in (series.get("values") or []):
                if v.get("value") is not None:
                    metrics.append(_metric("instagram", name, v["value"], date_range.end))
        return ProviderResult(success=True, metrics=metrics,
                              message=f"Pulled {len(metrics)} Instagram metrics.")

    def fetch_instagram_media(self, limit: int = 10) -> ProviderResult:
        """Recent organic posts with per-post insights (reach, impressions,
        likes, comments). Returns per-post detail in .steps and metric rows in
        .metrics (for the dashboard)."""
        if not self._token or not self._ig_user_id:
            return self._not_connected("Instagram")
        limit = max(1, min(int(limit or 10), 25))
        body, err = self._get(f"{self._ig_user_id}/media", {
            "fields": "id,caption,permalink,timestamp,media_type,like_count,comments_count",
            "limit": limit,
        })
        if err:
            return ProviderResult(success=False, message=f"Couldn't list Instagram posts: {err}")
        posts: list[dict] = []
        metrics: list[dict] = []
        from datetime import UTC
        from datetime import datetime as _dt
        now = _dt.now(UTC)
        for m in (body.get("data") or []):
            mid = m.get("id")
            likes = m.get("like_count") or 0
            comments = m.get("comments_count") or 0
            reach = impressions = None
            ins, ierr = self._get(f"{mid}/insights", {"metric": "reach,impressions"})
            if not ierr and ins:
                for s in (ins.get("data") or []):
                    vals = s.get("values") or [{}]
                    v = vals[0].get("value")
                    if s.get("name") == "reach":
                        reach = v
                    elif s.get("name") == "impressions":
                        impressions = v
            posts.append({
                "id": mid,
                "caption": (m.get("caption") or "")[:120],
                "permalink": m.get("permalink"),
                "timestamp": m.get("timestamp"),
                "media_type": m.get("media_type"),
                "likes": likes, "comments": comments,
                "reach": reach, "impressions": impressions,
            })
            # Per-post metric rows for the dashboard / ingest.
            for name, val in (("reach", reach), ("impressions", impressions),
                              ("likes", likes), ("comments", comments)):
                if val is not None:
                    metrics.append(_metric("instagram", name, val, now))
        return ProviderResult(
            success=True, metrics=metrics, steps=posts,
            message=f"Read insights for {len(posts)} recent Instagram post(s).",
        )


def _metric(platform: str, metric: str, value, when: datetime) -> dict:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    # NOTE: Graph *insights* money fields (spend, action_values) are already in
    # the account currency as decimals — do NOT divide by 100. (Only Marketing
    # API *budgets* are in minor units, handled separately at campaign create.)
    return {"platform": platform, "entity_type": "account", "entity_id": None,
            "metric": metric, "value": value, "captured_for": when.isoformat()}


def _sum_actions(actions, contains: str) -> float:
    total = 0.0
    for a in (actions or []):
        if contains in (a.get("action_type") or ""):
            try:
                total += float(a.get("value", 0))
            except (TypeError, ValueError):
                pass
    return total


def _div(num: float, den: float, scale: float = 1.0) -> float:
    return round(num / den * scale, 2) if den else 0.0


def _ad_row(row: dict) -> dict:
    """Normalize one Graph insights row (campaign- or ad-level) into a full
    metric set with derived rates. Money fields are already in account currency
    (not cents) for insights."""
    spend = float(row.get("spend") or 0)
    impr = float(row.get("impressions") or 0)
    clicks = float(row.get("clicks") or 0)
    reach = float(row.get("reach") or 0)
    freq = float(row.get("frequency") or 0)
    purch = _sum_actions(row.get("actions"), "purchase")
    rev = _sum_actions(row.get("action_values"), "purchase")
    return {
        "spend": round(spend, 2), "impressions": int(impr), "clicks": int(clicks),
        "reach": int(reach), "frequency": round(freq, 2),
        "purchases": round(purch, 2), "revenue": round(rev, 2),
        "roas": _div(rev, spend), "ctr": _div(clicks, impr, 100.0),
        "cpc": _div(spend, clicks), "cpm": _div(spend, impr, 1000.0),
        "cost_per_purchase": _div(spend, purch),
    }


def _totals(rows: list[dict]) -> dict:
    """Aggregate raw counts across rows, then recompute derived rates from the
    sums (rates can't simply be summed)."""
    spend = sum(r["spend"] for r in rows)
    impr = sum(r["impressions"] for r in rows)
    clicks = sum(r["clicks"] for r in rows)
    reach = sum(r["reach"] for r in rows)
    purch = sum(r["purchases"] for r in rows)
    rev = sum(r["revenue"] for r in rows)
    return {
        "spend": round(spend, 2), "impressions": impr, "clicks": clicks,
        "reach": reach, "purchases": round(purch, 2), "revenue": round(rev, 2),
        "roas": _div(rev, spend), "ctr": _div(clicks, impr, 100.0),
        "cpc": _div(spend, clicks), "cpm": _div(spend, impr, 1000.0),
        "cost_per_purchase": _div(spend, purch),
        "frequency": _div(impr, reach), "campaigns": len(rows),
    }
