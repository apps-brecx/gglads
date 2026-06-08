"""MetaApiProvider — official Instagram Graph API + Marketing API backend.

Activated when META_EXECUTION_MODE=api and a Meta connection exists (set up via
the Facebook-Login OAuth flow in meta/oauth.py). Reads the stored long-lived
token + linked IG business account + ad account from the 'meta' integration.

Safety: ad campaigns are always created PAUSED — Helena never auto-spends; the
campaign goes live only when explicitly resumed (through the approval-gated
queue). Instagram posts are published only when the publish task is approved.
"""

from __future__ import annotations

import json
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
            conv = _pick_purchase(row.get("actions"))
            rev = _pick_purchase(row.get("action_values"))
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
            purch = _pick_purchase(row.get("actions"))
            rev = _pick_purchase(row.get("action_values"))
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
                "name": c.get("name"),
                "status": raw_status.replace("_", " ").title(),
                "daily_budget": budget,
            }
        return out

    def _ad_status_map(self, campaign_id) -> dict[str, str]:
        """ad_id → human delivery status. Read from the ad object (the insights
        endpoint can't return effective_status)."""
        body, err = self._get(f"{campaign_id}/ads",
                              {"fields": "id,effective_status", "limit": 500})
        out: dict[str, str] = {}
        if err or not body:
            return out
        for a in (body.get("data") or []):
            raw = a.get("effective_status") or ""
            out[a.get("id")] = raw.replace("_", " ").title() or None
        return out

    def _ad_meta_map(self, campaign_id) -> dict[str, dict]:
        """ad_id → {status, image} for a campaign's ads. One call gives both the
        delivery status (not available on /insights) and the creative thumbnail."""
        body, err = self._get(f"{campaign_id}/ads", {
            "fields": "id,effective_status,creative{thumbnail_url,image_url}", "limit": 500})
        out: dict[str, dict] = {}
        if err or not body:
            return out
        for a in (body.get("data") or []):
            cr = a.get("creative") or {}
            raw = a.get("effective_status") or ""
            out[a.get("id")] = {
                "status": raw.replace("_", " ").title() or None,
                "image": cr.get("image_url") or cr.get("thumbnail_url"),
            }
        return out

    def _account_currency(self) -> str:
        body, err = self._get(f"act_{self._ad_account_id}", {"fields": "currency"})
        if err or not body:
            return "USD"
        return body.get("currency") or "USD"

    def fetch_campaign_detail(self, campaign_id, since, until) -> dict:
        """Drill-down for ONE campaign: its ads (with adset ids) at full metric
        detail, account totals, and a DAILY time-series for charting. Powers the
        campaign detail page."""
        if not self._token or not self._ad_account_id:
            return {"ok": False, "error": "Meta isn't connected for ads."}
        tr = f'{{"since":"{since}","until":"{until}"}}'
        filt = (f'[{{"field":"campaign.id","operator":"IN","value":["{campaign_id}"]}}]')

        # Ads in this campaign (with their ad-set ids for bid/cost-cap edits).
        # NOTE: the /insights endpoint does NOT accept delivery-status fields
        # (effective_status); those live on the ad object, fetched separately.
        ad_body, err = self._get(f"act_{self._ad_account_id}/insights", {
            "level": "ad",
            "fields": ("ad_id,ad_name,adset_id,adset_name,"
                       "spend,impressions,clicks,reach,frequency,"
                       "actions,action_values,purchase_roas"),
            "time_range": tr, "filtering": filt, "limit": 500,
        })
        if err:
            return {"ok": False, "error": f"Ad insights failed: {err}"}
        ad_meta = self._ad_meta_map(campaign_id)
        ads = []
        for r in (ad_body.get("data") or []):
            row = _ad_row(r)
            m = ad_meta.get(r.get("ad_id")) or {}
            row.update({"id": r.get("ad_id"), "name": r.get("ad_name") or "(unnamed ad)",
                        "adset_id": r.get("adset_id"), "adset_name": r.get("adset_name"),
                        "status": m.get("status"), "image": m.get("image")})
            ads.append(row)
        ads.sort(key=lambda x: x["spend"], reverse=True)

        # Daily series for the chart (campaign-level, one row per day).
        series = []
        s_body, s_err = self._get(f"act_{self._ad_account_id}/insights", {
            "level": "campaign",
            "fields": "spend,impressions,clicks,actions,action_values",
            "time_range": tr, "filtering": filt, "time_increment": 1, "limit": 500,
        })
        if not s_err:
            for r in (s_body.get("data") or []):
                spend = float(r.get("spend") or 0)
                rev = _pick_purchase(r.get("action_values"))
                series.append({
                    "date": r.get("date_start") or r.get("date_stop"),
                    "spend": round(spend, 2),
                    "revenue": round(rev, 2),
                    "clicks": int(float(r.get("clicks") or 0)),
                    "purchases": round(_pick_purchase(r.get("actions")), 2),
                    "roas": _div(rev, spend),
                })
            series.sort(key=lambda p: p["date"] or "")

        meta = self._campaign_meta_map().get(str(campaign_id)) or {}
        name = meta.get("name")
        if not name:  # fall back to a direct lookup
            cb, _ = self._get(str(campaign_id), {"fields": "name,effective_status,daily_budget"})
            if cb:
                name = cb.get("name")
        return {"ok": True,
                "campaign": {"id": str(campaign_id), "name": name or f"Campaign {campaign_id}",
                             "status": meta.get("status"),
                             "daily_budget": meta.get("daily_budget")},
                "ads": ads, "totals": _totals(ads), "series": series,
                "currency": self._account_currency(), "range": f"{since} → {until}"}

    def fetch_ad_detail(self, ad_id, since, until) -> dict:
        """Everything Meta exposes for ONE ad: the ad object + creative, its ad
        set (budget/bid/optimization/targeting), and its insights — so the ad
        can be reviewed and managed here without opening Ads Manager."""
        if not self._token or not self._ad_account_id:
            return {"ok": False, "error": "Meta isn't connected for ads."}
        ad, err = self._get(str(ad_id), {"fields": (
            "id,name,status,effective_status,created_time,updated_time,adset_id,campaign_id,"
            "creative{id,name,title,body,thumbnail_url,image_url,call_to_action_type,link_url}")})
        if err or not ad:
            return {"ok": False, "error": f"Ad lookup failed: {err}"}
        creative = ad.get("creative") or {}
        adset = {}
        if ad.get("adset_id"):
            ab, _ = self._get(str(ad["adset_id"]), {"fields": (
                "id,name,status,effective_status,daily_budget,lifetime_budget,bid_amount,"
                "bid_strategy,billing_event,optimization_goal,start_time,end_time,"
                "destination_type,targeting")})
            adset = ab or {}

        def _money(v):
            try:
                return round(float(v) / 100, 2) if v else None
            except (TypeError, ValueError):
                return None

        tr = f'{{"since":"{since}","until":"{until}"}}'
        ib, _ = self._get(f"act_{self._ad_account_id}/insights", {
            "level": "ad",
            "fields": ("spend,impressions,clicks,reach,frequency,"
                       "actions,action_values,purchase_roas"),
            "time_range": tr,
            "filtering": f'[{{"field":"ad.id","operator":"IN","value":["{ad_id}"]}}]',
            "limit": 1,
        })
        rows = (ib or {}).get("data") or []
        metrics = _ad_row(rows[0]) if rows else {}
        targeting = adset.get("targeting")
        return {
            "ok": True,
            "ad": {
                "id": ad.get("id"), "name": ad.get("name"),
                "status": _titlecase(ad.get("effective_status") or ad.get("status")),
                "created": ad.get("created_time"), "updated": ad.get("updated_time"),
                "campaign_id": ad.get("campaign_id"), "adset_id": ad.get("adset_id"),
            },
            "creative": {
                "title": creative.get("title"), "body": creative.get("body"),
                "cta": _titlecase(creative.get("call_to_action_type")),
                "link": creative.get("link_url"),
            },
            "image": creative.get("image_url") or creative.get("thumbnail_url"),
            "adset": {
                "id": adset.get("id"), "name": adset.get("name"),
                "status": _titlecase(adset.get("effective_status") or adset.get("status")),
                "daily_budget": _money(adset.get("daily_budget")),
                "lifetime_budget": _money(adset.get("lifetime_budget")),
                "bid_amount": _money(adset.get("bid_amount")),
                "bid_strategy": _titlecase(adset.get("bid_strategy")),
                "billing_event": _titlecase(adset.get("billing_event")),
                "optimization_goal": _titlecase(adset.get("optimization_goal")),
                "start_time": adset.get("start_time"), "end_time": adset.get("end_time"),
            },
            "targeting_json": json.dumps(targeting, indent=2) if targeting else None,
            "metrics": metrics, "currency": self._account_currency(),
            "range": f"{since} → {until}",
        }

    def fetch_ads_with_links(self) -> dict:
        """Every ad in the account with its destination URL + delivery status —
        so the stock guard can match each ad to a Shopify product by handle.
        Returns {ok, ads:[{ad_id, ad_name, campaign_id, status, link}]}."""
        if not self._token or not self._ad_account_id:
            return {"ok": False, "error": "Meta isn't connected for ads.", "ads": []}
        body, err = self._get(f"act_{self._ad_account_id}/ads", {
            "fields": "id,name,campaign_id,effective_status,"
                      "creative{link_url,object_story_spec}",
            "limit": 500,
        })
        if err:
            return {"ok": False, "error": err, "ads": []}
        ads = []
        for a in (body.get("data") or []):
            cr = a.get("creative") or {}
            ads.append({
                "ad_id": a.get("id"), "ad_name": a.get("name"),
                "campaign_id": a.get("campaign_id"),
                "status": (a.get("effective_status") or "").upper(),
                "link": cr.get("link_url") or _story_link(cr.get("object_story_spec")),
            })
        return {"ok": True, "ads": ads}

    # ---- generic writes (campaign / ad-set / ad) ----------------------
    def set_status(self, entity_id: str, status: str) -> ProviderResult:
        """Pause/activate any object (campaign, ad set, or ad) by id."""
        if not self._token:
            return self._not_connected("ads")
        status = "ACTIVE" if str(status).upper() in ("ACTIVE", "RESUME", "ON") else "PAUSED"
        _body, err = self._post(str(entity_id), {"status": status})
        if err:
            return ProviderResult(success=False, message=f"Status change failed: {err}")
        return ProviderResult(success=True, external_id=str(entity_id),
                              message=f"Set status to {status}.")

    def set_fields(self, entity_id: str, fields: dict) -> ProviderResult:
        """Update arbitrary writable fields on an object (e.g. daily_budget,
        bid_amount + bid_strategy). Money fields must be in minor units (cents)."""
        if not self._token:
            return self._not_connected("ads")
        _body, err = self._post(str(entity_id), dict(fields))
        if err:
            return ProviderResult(success=False, message=f"Update failed: {err}")
        return ProviderResult(success=True, external_id=str(entity_id), message="Updated.")


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


# Meta reports the SAME purchases under several overlapping action types
# (omni_purchase, purchase, offsite_conversion.fb_pixel_purchase, …). Summing
# them double-counts and inflates purchases/revenue/ROAS, so we take ONE
# canonical value by priority instead.
_PURCHASE_PRIORITY = (
    "omni_purchase",
    "purchase",
    "offsite_conversion.fb_pixel_purchase",
    "onsite_web_purchase",
    "web_in_store_purchase",
    "app_custom_event.fb_mobile_purchase",
)


def _pick_purchase(rows) -> float:
    """Return the single best purchase count/value from an actions or
    action_values list — NOT the sum (Meta's purchase action types overlap)."""
    by_type: dict[str, float] = {}
    for a in (rows or []):
        at = a.get("action_type") or ""
        try:
            by_type[at] = float(a.get("value", 0))
        except (TypeError, ValueError):
            continue
    for pref in _PURCHASE_PRIORITY:
        if pref in by_type:
            return by_type[pref]
    # Unknown labelling — take the largest single purchase-ish value, never the sum.
    cands = [v for at, v in by_type.items() if "purchase" in at]
    return max(cands) if cands else 0.0


def _div(num: float, den: float, scale: float = 1.0) -> float:
    return round(num / den * scale, 2) if den else 0.0


def _titlecase(v) -> str | None:
    """'OUTCOME_SALES' -> 'Outcome Sales'; empty -> None."""
    return str(v or "").replace("_", " ").title() or None


def _story_link(spec) -> str | None:
    """Pull the destination URL out of a creative's object_story_spec."""
    if not isinstance(spec, dict):
        return None
    ld = spec.get("link_data") or {}
    if ld.get("link"):
        return ld["link"]
    cta = (ld.get("call_to_action") or {}).get("value") or {}
    if cta.get("link"):
        return cta["link"]
    vd = spec.get("video_data") or {}
    cta2 = (vd.get("call_to_action") or {}).get("value") or {}
    return cta2.get("link")


def _ad_row(row: dict) -> dict:
    """Normalize one Graph insights row (campaign- or ad-level) into a full
    metric set with derived rates. Money fields are already in account currency
    (not cents) for insights."""
    spend = float(row.get("spend") or 0)
    impr = float(row.get("impressions") or 0)
    clicks = float(row.get("clicks") or 0)
    reach = float(row.get("reach") or 0)
    freq = float(row.get("frequency") or 0)
    purch = _pick_purchase(row.get("actions"))
    rev = _pick_purchase(row.get("action_values"))
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
