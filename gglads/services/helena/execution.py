"""Execution layer: the DB-backed task queue, approval gates, retries, and
the ExecutionRun audit log.

Every provider call that spends money or publishes/sends publicly goes through
here so it can be (1) gated behind explicit user approval, (2) retried on
failure, and (3) logged with its input spec, steps, result, and artifacts.

The cron worker (gglads/cron/helena_worker.py) calls run_due_tasks(); the chat
agent and routes call enqueue()/approve()/run_task().
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.email_campaign import EmailCampaign
from gglads.models.helena import (
    ExecutionRun,
    MetaAdCampaign,
    Post,
    ScheduledTask,
)
from gglads.services.helena import analytics as analytics_svc
from gglads.services.helena.email.factory import get_email_provider
from gglads.services.helena.meta.factory import get_meta_provider
from gglads.services.helena.specs import (
    CampaignSpec,
    DateRange,
    EmailCampaignSpec,
    InstagramPostSpec,
    ProviderResult,
)

logger = logging.getLogger("gglads.helena.execution")

# Actions that spend money or publish/send publicly, or write into Shopify —
# always need approval. Creating a Shopify Email draft is gated too (it writes
# a campaign into the Shopify account); it still never auto-sends.
APPROVAL_REQUIRED_KINDS = {
    "publish_post",
    "schedule_post",
    "create_ad_campaign",
    "update_budget",
    "resume_campaign",
    "create_email_draft",
    "send_email",
    "schedule_email",
}


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------

def enqueue(
    db: Session,
    *,
    title: str,
    kind: str,
    spec: dict[str, Any] | None = None,
    run_after: datetime | None = None,
    recurrence: str | None = None,
    user_id: int | None = None,
) -> ScheduledTask:
    requires = kind in APPROVAL_REQUIRED_KINDS
    task = ScheduledTask(
        title=title,
        kind=kind,
        spec_json=json.dumps(spec or {}),
        status="needs_review" if requires else "pending",
        requires_approval=requires,
        run_after=run_after,
        recurrence=recurrence,
        created_by_user_id=user_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def approve(db: Session, task_id: int, user_id: int | None) -> ScheduledTask | None:
    task = db.get(ScheduledTask, task_id)
    if task is None or task.status != "needs_review":
        return task
    task.status = "approved"
    task.approved_at = _now()
    task.approved_by_user_id = user_id
    task.updated_at = _now()
    db.commit()
    db.refresh(task)
    return task


def cancel(db: Session, task_id: int) -> None:
    task = db.get(ScheduledTask, task_id)
    if task is not None and task.status in ("pending", "needs_review", "approved", "failed"):
        task.status = "cancelled"
        task.updated_at = _now()
        db.commit()


def upcoming(db: Session, limit: int = 20) -> list[ScheduledTask]:
    return list(
        db.scalars(
            select(ScheduledTask)
            .where(ScheduledTask.status.in_(("pending", "needs_review", "approved", "running")))
            .order_by(ScheduledTask.run_after.is_(None), ScheduledTask.run_after)
            .limit(limit)
        ).all()
    )


def pending_approvals(db: Session) -> list[ScheduledTask]:
    return list(
        db.scalars(
            select(ScheduledTask)
            .where(ScheduledTask.status == "needs_review")
            .order_by(ScheduledTask.created_at)
        ).all()
    )


# ---------------------------------------------------------------------------
# ExecutionRun logging
# ---------------------------------------------------------------------------

def _log_run(
    db: Session,
    *,
    task_id: int | None,
    action: str,
    backend: str,
    spec: dict[str, Any],
    result: ProviderResult,
) -> ExecutionRun:
    run = ExecutionRun(
        task_id=task_id,
        backend=backend,
        action=action,
        input_spec_json=json.dumps(spec),
        steps_json=json.dumps(result.steps),
        result_json=result.model_dump_json(),
        artifacts_json=json.dumps(result.artifacts),
        status="succeeded" if result.success else "failed",
        finished_at=_now(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


# ---------------------------------------------------------------------------
# The worker: drain due tasks
# ---------------------------------------------------------------------------

def run_due_tasks(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Run every task that is ready: status pending/approved and run_after due.
    needs_review tasks are skipped until approved. Returns a count summary."""
    now = now or _now()
    due = db.scalars(
        select(ScheduledTask)
        .where(ScheduledTask.status.in_(("pending", "approved")))
        .where((ScheduledTask.run_after.is_(None)) | (ScheduledTask.run_after <= now))
        .order_by(ScheduledTask.run_after.is_(None), ScheduledTask.run_after)
        .limit(50)
    ).all()
    summary = {"ran": 0, "succeeded": 0, "failed": 0}
    for task in due:
        summary["ran"] += 1
        ok = run_task(db, task)
        summary["succeeded" if ok else "failed"] += 1
    return summary


def run_task(db: Session, task: ScheduledTask) -> bool:
    """Execute one task now, recording attempts + an ExecutionRun. Reschedules
    recurring tasks on success."""
    if task.requires_approval and task.status not in ("approved",):
        return False
    task.status = "running"
    task.attempts += 1
    task.updated_at = _now()
    db.commit()

    spec = json.loads(task.spec_json or "{}")
    try:
        result = _dispatch(db, task.kind, spec)
    except Exception as exc:
        logger.exception("task %s crashed", task.id)
        result = ProviderResult(success=False, message=f"{type(exc).__name__}: {exc}")

    backend = "browser"  # dispatch records the real backend on the run below
    _log_run(db, task_id=task.id, action=task.kind, backend=backend, spec=spec, result=result)

    if result.success:
        task.status = "succeeded"
        task.last_error = None
        _reschedule_if_recurring(task)
    else:
        if task.attempts >= task.max_attempts:
            task.status = "failed"
        else:
            # back off and retry on the next worker tick
            task.status = "approved" if task.requires_approval else "pending"
            task.run_after = _now() + timedelta(minutes=5 * task.attempts)
        task.last_error = result.message[:2000]
    task.updated_at = _now()
    db.commit()
    return result.success


def _reschedule_if_recurring(task: ScheduledTask) -> None:
    if not task.recurrence:
        return
    base = _now()
    if task.recurrence == "daily":
        nxt = base + timedelta(days=1)
    elif task.recurrence.startswith("weekly"):
        nxt = base + timedelta(days=7)
    elif task.recurrence == "hourly":
        nxt = base + timedelta(hours=1)
    else:
        return
    # Recurring tasks re-arm; approval-required ones drop back to needs_review.
    task.status = "needs_review" if task.requires_approval else "pending"
    task.run_after = nxt
    task.attempts = 0


# ---------------------------------------------------------------------------
# Dispatch — map task kind -> provider call + DB side effects
# ---------------------------------------------------------------------------

def _dispatch(db: Session, kind: str, spec: dict[str, Any]) -> ProviderResult:
    meta = get_meta_provider(db)

    if kind in ("publish_post", "schedule_post"):
        post = db.get(Post, spec.get("post_id"))
        if post is None:
            return ProviderResult(success=False, message="Post not found.")
        pspec = InstagramPostSpec(
            caption=post.caption,
            hashtags=post.hashtags,
            image_url=post.image_url,
            account_handle=post.account_handle,
        )
        post.status = "publishing"
        db.commit()
        if kind == "schedule_post" and spec.get("when"):
            when = datetime.fromisoformat(spec["when"])
            res = meta.schedule_post(pspec, when)
        else:
            res = meta.publish_instagram_post(pspec)
        if res.success:
            post.status = "scheduled" if kind == "schedule_post" else "published"
            post.published_at = None if kind == "schedule_post" else _now()
            post.external_id = res.external_id
            post.permalink = res.permalink
        else:
            post.status = "failed"
        db.commit()
        return res

    if kind == "create_ad_campaign":
        camp = db.get(MetaAdCampaign, spec.get("campaign_id"))
        if camp is None:
            return ProviderResult(success=False, message="Campaign not found.")
        cspec = CampaignSpec(
            name=camp.name,
            objective=camp.objective,
            budget_type=camp.budget_type,
            budget_cents=camp.budget_cents,
            audience=json.loads(camp.audience_json) if camp.audience_json else {},
            creative_image_url=camp.creative_image_url,
            creative_copy=camp.creative_copy,
        )
        res = meta.create_campaign(cspec)
        camp.status = "active" if res.success else "failed"
        camp.external_id = res.external_id or camp.external_id
        camp.updated_at = _now()
        db.commit()
        return res

    if kind == "update_budget":
        camp = db.get(MetaAdCampaign, spec.get("campaign_id"))
        if camp is None or not camp.external_id:
            return ProviderResult(success=False, message="Campaign not pushed yet.")
        amount = int(spec.get("amount_cents", camp.budget_cents))
        res = meta.update_budget(camp.external_id, amount)
        if res.success:
            camp.budget_cents = amount
            camp.updated_at = _now()
            db.commit()
        return res

    if kind in ("pause_campaign", "resume_campaign"):
        camp = db.get(MetaAdCampaign, spec.get("campaign_id"))
        if camp is None or not camp.external_id:
            return ProviderResult(success=False, message="Campaign not pushed yet.")
        res = (
            meta.pause_campaign(camp.external_id)
            if kind == "pause_campaign"
            else meta.resume_campaign(camp.external_id)
        )
        if res.success:
            camp.status = "paused" if kind == "pause_campaign" else "active"
            camp.updated_at = _now()
            db.commit()
        return res

    if kind == "fetch_campaign_metrics":
        days = int(spec.get("days", 30))
        res = meta.fetch_campaign_metrics(_range(days))
        analytics_svc.ingest_metrics(db, res.metrics)
        return res

    if kind == "fetch_instagram_insights":
        days = int(spec.get("days", 30))
        res = meta.fetch_instagram_insights(_range(days))
        analytics_svc.ingest_metrics(db, res.metrics)
        return res

    if kind in ("create_email_draft", "schedule_email", "send_email"):
        return _dispatch_email(db, kind, spec)

    if kind == "fetch_email_metrics":
        days = int(spec.get("days", 30))
        provider = get_email_provider(db)
        res = provider.fetch_email_metrics(_range(days))
        analytics_svc.ingest_metrics(db, res.metrics)
        return res

    if kind == "performance_digest":
        return _performance_digest(db, spec)

    return ProviderResult(success=False, message=f"Unknown task kind: {kind}")


def _dispatch_email(db: Session, kind: str, spec: dict[str, Any]) -> ProviderResult:
    camp = db.get(EmailCampaign, spec.get("campaign_id"))
    if camp is None:
        return ProviderResult(success=False, message="Email campaign not found.")
    if not camp.html:
        return ProviderResult(success=False, message="Email has no rendered HTML yet.")
    provider = get_email_provider(db)
    espec = EmailCampaignSpec(
        name=camp.name,
        subject=camp.subject or camp.name,
        preheader=camp.preheader,
        html=camp.html,
        plain_text=camp.plain_text,
        audience=camp.audience,
    )
    if kind == "schedule_email" and spec.get("when"):
        when = datetime.fromisoformat(spec["when"])
        res = provider.schedule_campaign(espec, when)
        if res.success:
            camp.status = "scheduled"
            camp.scheduled_at = when
    else:
        # create_email_draft and send_email both create the draft; sending is
        # always a separate, explicitly-approved Shopify-side action.
        res = provider.create_draft_campaign(espec)
        if res.success:
            camp.status = "draft"
    if res.success:
        camp.external_id = res.external_id or camp.external_id
        camp.updated_at = _now()
    db.commit()
    return res


def _performance_digest(db: Session, spec: dict[str, Any]) -> ProviderResult:
    """Post a recurring performance summary into a chat session."""
    from gglads.services.helena import agent as agent_svc

    days = int(spec.get("days", 7))
    summary = analytics_svc.digest_text(db, days=days)
    session_id = spec.get("session_id")
    if session_id:
        agent_svc.append_assistant_message(db, int(session_id), summary)
    return ProviderResult(success=True, message="Digest posted.", steps=[{"days": days}])


def _range(days: int) -> DateRange:
    end = _now()
    return DateRange(start=end - timedelta(days=days), end=end)
