"""Push a gglads campaign to Google Ads.

v1 scope:
- Create or update the CampaignBudget (shared name = "gglads-cb-<campaign_id>").
- Create or update the Campaign itself (Search network, PAUSED on first create
  so the user flips it active in Google Ads).
- For each AdGroup row: create or update the AdGroup on Google Ads, then
  create or update its keywords (one Criterion per keyword) and its
  Responsive Search Ad (one RSA per ad group, PAUSED on first create).
- All created entities start PAUSED — the user toggles them inside the
  Google Ads UI when they're ready.

Resource IDs are persisted back onto our rows so subsequent pushes do an
update mutation instead of duplicate-create.

This module is import-safe even when the google-ads SDK is missing or the
integration isn't configured — it returns a friendly error from push().
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from gglads.models.campaign import AdCampaign, AdCampaignKeyword, AdGroup
from gglads.services import campaigns as campaigns_svc
from gglads.services import integrations as integrations_svc

logger = logging.getLogger("gglads.gads_push")


_MATCH_TYPE_ENUM = {
    "exact": "EXACT",
    "phrase": "PHRASE",
    "broad": "BROAD",
}


def _build_client(db: Session):
    cfg = integrations_svc.get_config(db, "google_ads")
    required = [
        "developer_token",
        "oauth_client_id",
        "oauth_client_secret",
        "refresh_token",
        "customer_id",
    ]
    missing = [k for k in required if not (cfg.get(k) or "").strip()]
    if missing:
        return None, None, f"Google Ads missing: {', '.join(missing)}"
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        return None, None, "google-ads SDK not installed"
    client_cfg: dict[str, Any] = {
        "developer_token": cfg["developer_token"].strip(),
        "client_id": cfg["oauth_client_id"].strip(),
        "client_secret": cfg["oauth_client_secret"].strip(),
        "refresh_token": cfg["refresh_token"].strip(),
        "use_proto_plus": True,
    }
    login_cid = (cfg.get("login_customer_id") or "").replace("-", "").strip()
    if login_cid:
        client_cfg["login_customer_id"] = login_cid
    try:
        client = GoogleAdsClient.load_from_dict(client_cfg)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {exc}"
    customer_id = cfg["customer_id"].replace("-", "").strip()
    return client, customer_id, None


def _gads_error_message(exc: Exception) -> str:
    """Best-effort extract of GoogleAdsException error text."""
    msgs: list[str] = []
    try:
        failure = getattr(exc, "failure", None)
        if failure is not None:
            for err in getattr(failure, "errors", []) or []:
                msgs.append(str(getattr(err, "message", err))[:300])
    except Exception:  # noqa: BLE001
        pass
    if not msgs:
        return f"{type(exc).__name__}: {exc}"[:600]
    return " | ".join(msgs)[:600]


def _resource_id_from_name(resource_name: str) -> int:
    """e.g. 'customers/123/campaignBudgets/456' → 456"""
    try:
        return int(resource_name.rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return 0


def _budget_micros(daily_budget_cents: int) -> int:
    # Google Ads expects budget in micros of account currency. We treat the
    # daily_budget_cents value as USD cents.
    return max(10_000, int(daily_budget_cents) * 10_000)


def _apply_bidding_strategy(client, campaign, bid_strategy: str, target_cpa_cents: int | None) -> None:
    """Set the right bidding-strategy oneof on a Campaign create mutation."""
    if bid_strategy == "manual_cpc":
        campaign.manual_cpc.enhanced_cpc_enabled = False
    elif bid_strategy == "maximize_clicks":
        # 'TargetSpend' is the standard portfolio for Maximize Clicks.
        campaign.target_spend._pb.SetInParent()
    elif bid_strategy == "target_cpa":
        if target_cpa_cents and target_cpa_cents > 0:
            campaign.target_cpa.target_cpa_micros = int(target_cpa_cents) * 10_000
        else:
            campaign.target_cpa._pb.SetInParent()
    else:
        # Default: MAXIMIZE_CONVERSIONS
        campaign.maximize_conversions._pb.SetInParent()


def _field_mask(client, paths: list[str]):
    """Build a google.protobuf.FieldMask for update mutations.

    FieldMask is a standard protobuf type, NOT a Google Ads message type —
    so client.get_type("FieldMask") fails with "Specified type 'FieldMask'
    does not exist in Google Ads API vXX". Import the stdlib type instead.
    """
    from google.protobuf.field_mask_pb2 import FieldMask
    return FieldMask(paths=paths)


def _push_budget(client, customer_id: str, db: Session, c: AdCampaign) -> str:
    """Create or update the CampaignBudget. Returns resource_name."""
    budget_service = client.get_service("CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    amount_micros = _budget_micros(c.daily_budget_cents)
    if c.google_ads_budget_id:
        budget = op.update
        budget.resource_name = (
            f"customers/{customer_id}/campaignBudgets/{c.google_ads_budget_id}"
        )
        budget.amount_micros = amount_micros
        budget.name = f"gglads-cb-{c.id}"
        client.copy_from(op.update_mask, _field_mask(client, ["amount_micros", "name"]))
    else:
        budget = op.create
        budget.name = f"gglads-cb-{c.id}"
        budget.amount_micros = amount_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False
    resp = budget_service.mutate_campaign_budgets(
        customer_id=customer_id, operations=[op]
    )
    rn = resp.results[0].resource_name
    if not c.google_ads_budget_id:
        c.google_ads_budget_id = _resource_id_from_name(rn)
    return rn


def _push_campaign(
    client, customer_id: str, db: Session, c: AdCampaign, budget_resource: str
) -> str:
    """Create or update the Campaign. Returns resource_name."""
    campaign_service = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    if c.google_ads_campaign_id:
        # v1: update name + budget only. Changing bidding strategy after the
        # fact is risky (often requires the user to confirm in the UI) so we
        # leave it untouched on re-push.
        campaign = op.update
        campaign.resource_name = (
            f"customers/{customer_id}/campaigns/{c.google_ads_campaign_id}"
        )
        campaign.name = c.name
        campaign.campaign_budget = budget_resource
        client.copy_from(
            op.update_mask, _field_mask(client, ["name", "campaign_budget"])
        )
    else:
        campaign = op.create
        campaign.name = c.name
        campaign.campaign_budget = budget_resource
        campaign.advertising_channel_type = (
            client.enums.AdvertisingChannelTypeEnum.SEARCH
        )
        # Always start PAUSED — user activates in the Google Ads UI.
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        # Network settings: search only (no display partners) for v1.
        campaign.network_settings.target_google_search = True
        campaign.network_settings.target_search_network = True
        campaign.network_settings.target_content_network = False
        campaign.network_settings.target_partner_search_network = False
        _apply_bidding_strategy(client, campaign, c.bid_strategy, c.target_cpa_cents)
    resp = campaign_service.mutate_campaigns(
        customer_id=customer_id, operations=[op]
    )
    rn = resp.results[0].resource_name
    if not c.google_ads_campaign_id:
        c.google_ads_campaign_id = _resource_id_from_name(rn)
    return rn


def _push_ad_group(
    client,
    customer_id: str,
    ag: AdGroup,
    campaign_resource: str,
) -> str:
    ad_group_service = client.get_service("AdGroupService")
    op = client.get_type("AdGroupOperation")
    if ag.google_ads_ad_group_id:
        ag_pb = op.update
        ag_pb.resource_name = (
            f"customers/{customer_id}/adGroups/{ag.google_ads_ad_group_id}"
        )
        ag_pb.name = ag.name
        client.copy_from(op.update_mask, _field_mask(client, ["name"]))
    else:
        ag_pb = op.create
        ag_pb.name = ag.name
        ag_pb.campaign = campaign_resource
        ag_pb.status = client.enums.AdGroupStatusEnum.PAUSED
        ag_pb.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    resp = ad_group_service.mutate_ad_groups(
        customer_id=customer_id, operations=[op]
    )
    rn = resp.results[0].resource_name
    if not ag.google_ads_ad_group_id:
        ag.google_ads_ad_group_id = _resource_id_from_name(rn)
    return rn


def _push_keywords(
    client,
    customer_id: str,
    ag: AdGroup,
    ad_group_resource: str,
    kws: list[AdCampaignKeyword],
) -> int:
    """Add any keywords that don't yet have a resource_name. Idempotent."""
    new_kws = [k for k in kws if not k.google_ads_resource_name]
    if not new_kws:
        return 0
    ad_group_criterion_service = client.get_service("AdGroupCriterionService")
    operations = []
    for k in new_kws:
        op = client.get_type("AdGroupCriterionOperation")
        crit = op.create
        crit.ad_group = ad_group_resource
        if k.is_negative:
            crit.negative = True
        else:
            crit.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            if k.cpc_bid_cents:
                crit.cpc_bid_micros = int(k.cpc_bid_cents) * 10_000
        crit.keyword.text = k.text
        crit.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum,
            _MATCH_TYPE_ENUM.get(k.match_type, "PHRASE"),
        )
        operations.append(op)
    resp = ad_group_criterion_service.mutate_ad_group_criteria(
        customer_id=customer_id, operations=operations
    )
    for k, r in zip(new_kws, resp.results):
        k.google_ads_resource_name = r.resource_name
    return len(new_kws)


def _push_rsa(
    client,
    customer_id: str,
    ag: AdGroup,
    ad_group_resource: str,
    landing_url: str,
) -> int | None:
    """Create the Responsive Search Ad. Returns the new google_ads_ad_id (or
    None if there's no copy to push)."""
    headlines = campaigns_svc.parse_list(ag.headlines_json)
    descriptions = campaigns_svc.parse_list(ag.descriptions_json)
    if not headlines or not descriptions:
        return None
    # Note: Google Ads doesn't allow editing an existing Ad's creative — only
    # its status. New copy → new Ad. The caller (approve_pending_copy) has
    # already moved the previous ad id into google_ads_prev_ad_id for later
    # pause, so by the time we reach here for an updated copy we know the
    # ag.google_ads_ad_id slot is empty and a fresh create is correct.
    ad_group_ad_service = client.get_service("AdGroupAdService")
    op = client.get_type("AdGroupAdOperation")
    ad_group_ad = op.create
    ad_group_ad.ad_group = ad_group_resource
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED
    ad = ad_group_ad.ad
    ad.final_urls.append(landing_url)
    for h in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = h
        ad.responsive_search_ad.headlines.append(asset)
    for d in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = d
        ad.responsive_search_ad.descriptions.append(asset)
    if ag.path1:
        ad.responsive_search_ad.path1 = ag.path1
    if ag.path2:
        ad.responsive_search_ad.path2 = ag.path2
    resp = ad_group_ad_service.mutate_ad_group_ads(
        customer_id=customer_id, operations=[op]
    )
    rn = resp.results[0].resource_name
    return _resource_id_from_name(rn)


def _pause_ad(client, customer_id: str, ad_group_id: int, ad_id: int) -> None:
    ad_group_ad_service = client.get_service("AdGroupAdService")
    op = client.get_type("AdGroupAdOperation")
    ad_group_ad = op.update
    ad_group_ad.resource_name = (
        f"customers/{customer_id}/adGroupAds/{ad_group_id}~{ad_id}"
    )
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED
    client.copy_from(op.update_mask, _field_mask(client, ["status"]))
    ad_group_ad_service.mutate_ad_group_ads(
        customer_id=customer_id, operations=[op]
    )


def push_campaign(db: Session, campaign_id: int) -> tuple[bool, str]:
    """Idempotently push the campaign + its ad groups + keywords + RSAs to
    Google Ads. Everything is created PAUSED on first push."""
    c = db.get(AdCampaign, campaign_id)
    if c is None:
        return False, "Campaign not found."
    if not (c.landing_page_url or "").strip():
        return False, "Set a landing page URL on the campaign before pushing."
    ad_groups = campaigns_svc.ad_groups_for_campaign(db, campaign_id)
    if not ad_groups:
        return False, "Campaign has no ad groups."
    client, customer_id, err = _build_client(db)
    if err:
        c.last_push_error = err
        db.commit()
        return False, err

    try:
        budget_rn = _push_budget(client, customer_id, db, c)
        campaign_rn = _push_campaign(client, customer_id, db, c, budget_rn)
        kw_total = 0
        rsa_total = 0
        landing_url = c.landing_page_url.strip()
        for ag in ad_groups:
            ag_rn = _push_ad_group(client, customer_id, ag, campaign_rn)
            pos, neg = campaigns_svc.keywords_for_ad_group(db, ag.id)
            kw_total += _push_keywords(client, customer_id, ag, ag_rn, pos + neg)
            # Push a fresh RSA only if we don't already have one live, OR if
            # the user approved new pending copy (which clears google_ads_ad_id).
            if not ag.google_ads_ad_id:
                new_ad_id = _push_rsa(client, customer_id, ag, ag_rn, landing_url)
                if new_ad_id:
                    ag.google_ads_ad_id = new_ad_id
                    rsa_total += 1
        c.last_pushed_at = datetime.now(timezone.utc)
        c.last_push_error = None
        db.commit()
    except Exception as exc:  # noqa: BLE001
        msg = _gads_error_message(exc)
        logger.exception("Google Ads push failed: %s", msg)
        c.last_push_error = msg
        db.commit()
        return False, f"Google Ads push failed: {msg}"

    return True, (
        f"Pushed to Google Ads (PAUSED). "
        f"{len(ad_groups)} ad group(s), {kw_total} new keyword(s), "
        f"{rsa_total} new ad(s). Activate them in the Google Ads UI when ready."
    )


def pause_due_prev_ads(db: Session) -> tuple[int, list[str]]:
    """For each ad group with a `google_ads_prev_ad_pause_at <= now()`,
    pause the prev ad on Google Ads and clear the prev_* fields.
    Returns (count_paused, errors)."""
    due = campaigns_svc.ad_groups_with_due_prev_ad(db)
    if not due:
        return 0, []
    client, customer_id, err = _build_client(db)
    if err:
        return 0, [err]
    paused = 0
    errors: list[str] = []
    for ag in due:
        try:
            _pause_ad(client, customer_id, ag.google_ads_ad_group_id, ag.google_ads_prev_ad_id)
            ag.google_ads_prev_ad_id = None
            ag.google_ads_prev_ad_pause_at = None
            db.commit()
            paused += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ad_group_id={ag.id}: {_gads_error_message(exc)}")
    return paused, errors
