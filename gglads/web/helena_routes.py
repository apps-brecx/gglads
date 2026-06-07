"""Helena module routes — chat (with streaming), Integrations grid, analytics
dashboard, content calendar, approvals, brand KB, and email preview.

Registered onto the main FastAPI app from web/app.py via build_router(templates).
Self-contained auth helpers mirror the app's session-cookie scheme.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads import __version__
from gglads.db.session import get_db
from gglads.models.email_campaign import EmailCampaign
from gglads.models.integration import Integration, IntegrationAccount
from gglads.models.user import User
from gglads.services.helena import agent as agent_svc
from gglads.services.helena import analytics as analytics_svc
from gglads.services.helena import brand as brand_svc
from gglads.services.helena import calendar as calendar_svc
from gglads.services.helena import dashboard as dashboard_svc
from gglads.services.helena import execution as exec_svc
from gglads.services.helena import explore as explore_svc
from gglads.services.helena import files as files_svc
from gglads.services.helena import integrations_catalog as catalog
from gglads.services.helena import optimization as opt_svc


def _now() -> datetime:
    return datetime.now(UTC)


# Match app.py's house style: inject the DB session via an Annotated dep
# rather than a call in the default argument.
DbDep = Annotated[Session, Depends(get_db)]


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    # ---- auth helpers --------------------------------------------------
    def current_user(request: Request, db: Session) -> User | None:
        uid = request.session.get("user_id")
        if not uid:
            return None
        return db.scalar(select(User).where(User.id == uid, User.is_active.is_(True)))

    def require_user(request: Request, db: Session) -> tuple[User | None, Response | None]:
        user = current_user(request, db)
        if user is None:
            return None, RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return user, None

    def flash(request: Request, message: str, level: str = "info") -> None:
        fl = request.session.get("flashes", [])
        fl.append({"message": message, "level": level})
        request.session["flashes"] = fl

    def consume_flashes(request: Request) -> list[dict]:
        return request.session.pop("flashes", [])

    def ctx(request: Request, user: User, active: str, **extra) -> dict:
        base = {
            "version": __version__, "user": user, "active": active,
            "flashes": consume_flashes(request), "request": request,
        }
        base.update(extra)
        return base

    # ---- right-sidebar shared data ------------------------------------
    def sidebar_data(db: Session) -> dict:
        return {
            "topline": analytics_svc.topline(db, days=30),
            "upcoming": exec_svc.upcoming(db, limit=8),
            "brand": brand_svc.get_or_create_brand(db),
            "assets": brand_svc.list_assets(db)[:6],
            "approvals_count": len(exec_svc.pending_approvals(db)),
        }

    # ===================================================================
    # Chat
    # ===================================================================
    @router.get("/helena", response_class=HTMLResponse)
    def helena_home() -> Response:
        return RedirectResponse("/helena/chat", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/helena/chat", response_class=HTMLResponse)
    def chat_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        sessions = agent_svc.list_sessions(db)
        sid = request.query_params.get("session")
        active_session = None
        if sid:
            active_session = agent_svc.get_session(db, int(sid))
        elif sessions:
            active_session = sessions[0]
        messages = agent_svc.get_messages(db, active_session.id) if active_session else []
        return templates.TemplateResponse(
            request, "helena/chat.html",
            ctx(request, user, "helena_chat",
                sessions=sessions, active_session=active_session,
                messages=messages, prefill=request.query_params.get("prefill", ""),
                **sidebar_data(db)),
        )

    @router.post("/helena/chat/new")
    def chat_new(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        s = agent_svc.create_session(db, user_id=user.id)
        return RedirectResponse(f"/helena/chat?session={s.id}",
                                status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/chat/{session_id}/stream")
    async def chat_stream(session_id: int, request: Request,
                          db: DbDep) -> Response:
        user = current_user(request, db)
        if user is None:
            return PlainTextResponse("Unauthorized", status_code=401)
        form = await request.form()
        message = str(form.get("message", "")).strip()
        if not message:
            return PlainTextResponse("Empty message", status_code=400)
        if agent_svc.get_session(db, session_id) is None:
            return PlainTextResponse("Session not found", status_code=404)

        def event_stream():
            for event in agent_svc.stream_turn(db, session_id, message, user.id):
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # ===================================================================
    # Integrations page
    # ===================================================================
    @router.get("/helena/integrations", response_class=HTMLResponse)
    def integrations_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        rows = {r.name: r for r in db.scalars(select(Integration)).all()}
        accounts: dict[str, list[IntegrationAccount]] = {}
        for acc in db.scalars(select(IntegrationAccount)).all():
            accounts.setdefault(acc.integration_name, []).append(acc)
        # Treat env-configured Shopify as connected even without a row.
        return templates.TemplateResponse(
            request, "helena/integrations.html",
            ctx(request, user, "helena_integrations",
                sections=catalog.SECTIONS, rows=rows, accounts=accounts),
        )

    @router.post("/helena/integrations/{key}/connect")
    async def integ_connect(key: str, request: Request,
                            db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        card = catalog.get_card(key)
        if card is None:
            return PlainTextResponse("Unknown integration", status_code=404)
        form = await request.form()
        handle = str(form.get("handle", "")).strip()

        # Google Flow connects only if a real auth test against Vertex AI /
        # the Generative Language API succeeds — no cosmetic "Connected".
        if key == "google_flow":
            from gglads.services.helena.images.google_flow import GoogleFlowImageService
            ok, detail = GoogleFlowImageService().test_connection()
            row = db.get(Integration, key)
            if row is None:
                from gglads.services.crypto import encrypt_json
                row = Integration(name=key, config_encrypted=encrypt_json({}))
                db.add(row)
            row.auth_type = "oauth"
            row.updated_by_user_id = user.id
            row.updated_at = _now()
            if ok:
                row.status = "connected"
                row.access_mode = "read_write"
                row.last_test_ok = True
                row.last_test_detail = detail
                db.commit()
                flash(request, f"Google Flow connected — {detail}", "ok")
            else:
                row.status = "not_connected"
                row.last_test_ok = False
                row.last_test_detail = detail
                db.commit()
                flash(request, f"Google Flow not connected: {detail}", "error")
            return RedirectResponse("/helena/integrations", status_code=status.HTTP_303_SEE_OTHER)

        row = db.get(Integration, key)
        if row is None:
            from gglads.services.crypto import encrypt_json
            row = Integration(name=key, config_encrypted=encrypt_json({}))
            db.add(row)
        row.status = "connected"
        row.auth_type = card["auth"]
        row.access_mode = "read_only"
        row.updated_by_user_id = user.id
        row.updated_at = _now()
        db.commit()

        if card["auth"] == "browser_agent":
            # The human performs login/verification; we record the linked
            # account for BrowserAgentMetaProvider to operate.
            db.add(IntegrationAccount(
                integration_name=key,
                handle=handle or f"{card['name']} account",
                status="connected",
            ))
            db.commit()
            flash(request, f"{card['name']} connected via browser agent. "
                           "Sign in and clear verification in the agent's browser.")
        else:
            flash(request, f"{card['name']} connected.")
        return RedirectResponse("/helena/integrations", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/integrations/{key}/access-mode")
    async def integ_access_mode(key: str, request: Request,
                                db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        form = await request.form()
        mode = str(form.get("mode", "read_only"))
        row = db.get(Integration, key)
        if row is not None:
            row.access_mode = "read_write" if mode == "read_write" else "read_only"
            row.updated_at = _now()
            db.commit()
            flash(request, f"{key} set to {row.access_mode.replace('_', ' ')}.")
        return RedirectResponse("/helena/integrations", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/integrations/{key}/disconnect")
    def integ_disconnect(key: str, request: Request,
                         db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        row = db.get(Integration, key)
        if row is not None:
            row.status = "not_connected"
            for acc in db.scalars(
                select(IntegrationAccount).where(IntegrationAccount.integration_name == key)
            ).all():
                db.delete(acc)
            db.commit()
            flash(request, f"{key} disconnected.")
        return RedirectResponse("/helena/integrations", status_code=status.HTTP_303_SEE_OTHER)

    # ===================================================================
    # Analytics dashboard
    # ===================================================================
    @router.get("/helena/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        days = int(request.query_params.get("days", 30))
        return templates.TemplateResponse(
            request, "helena/analytics.html",
            ctx(request, user, "helena_analytics",
                days=days,
                cards=dashboard_svc.cards(db, user, days),
                selected=dashboard_svc.get_selected(user),
                catalog=dashboard_svc.catalog(),
                chart=dashboard_svc.chart_series(db, user, days),
                tables=dashboard_svc.all_tables(db, days),
                recommendations=opt_svc.recommendations(db, days=days)),
        )

    @router.post("/helena/analytics/metrics/toggle")
    async def analytics_toggle_metric(request: Request, db: DbDep) -> Response:
        """Add/remove a metric from the user's dashboard. Returns JSON for the
        modal's live check toggles, or redirects for the no-JS path."""
        user, deny = require_user(request, db)
        if deny:
            return deny
        form = await request.form()
        key = str(form.get("key", ""))
        selected = dashboard_svc.toggle_metric(db, user, key)
        if request.headers.get("accept", "").startswith("application/json"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"selected": selected})
        return RedirectResponse(f"/helena/analytics?days={form.get('days', 30)}",
                                status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/analytics/refresh")
    def analytics_refresh(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        for kind in ("fetch_campaign_metrics", "fetch_instagram_insights", "fetch_email_metrics"):
            exec_svc.enqueue(db, title=f"Refresh: {kind}", kind=kind,
                             spec={"days": 30}, user_id=user.id)
        flash(request, "Queued a metrics refresh across all channels.")
        return RedirectResponse("/helena/analytics", status_code=status.HTTP_303_SEE_OTHER)

    # ===================================================================
    # Content calendar
    # ===================================================================
    @router.get("/helena/calendar", response_class=HTMLResponse)
    def calendar_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        view = request.query_params.get("view", "month")
        ref = calendar_svc.parse_ref(request.query_params.get("date"))
        data = calendar_svc.view_data(db, view, ref)
        return templates.TemplateResponse(
            request, "helena/calendar.html",
            ctx(request, user, "helena_calendar", cal=data),
        )

    @router.post("/helena/calendar/add")
    async def calendar_add(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        form = await request.form()
        channel = str(form.get("channel", "instagram"))
        day = calendar_svc.parse_ref(str(form.get("date", "")))
        caption = str(form.get("caption", "")).strip()
        calendar_svc.add_slot_item(db, channel=channel, day=day,
                                   caption=caption, user_id=user.id)
        flash(request, f"Added a {channel} draft for {day.isoformat()}.")
        view = form.get("view", "month")
        return RedirectResponse(
            f"/helena/calendar?view={view}&date={day.isoformat()}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # ===================================================================
    # Approvals + tasks
    # ===================================================================
    @router.get("/helena/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        return templates.TemplateResponse(
            request, "helena/approvals.html",
            ctx(request, user, "helena_approvals",
                pending=exec_svc.pending_approvals(db),
                upcoming=exec_svc.upcoming(db, limit=25)),
        )

    @router.post("/helena/tasks/{task_id}/approve")
    def task_approve(task_id: int, request: Request,
                     db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        exec_svc.approve(db, task_id, user.id)
        flash(request, "Approved. It will run on the next worker tick.")
        return RedirectResponse("/helena/approvals", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/tasks/{task_id}/cancel")
    def task_cancel(task_id: int, request: Request,
                    db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        exec_svc.cancel(db, task_id)
        flash(request, "Cancelled.")
        return RedirectResponse("/helena/approvals", status_code=status.HTTP_303_SEE_OTHER)

    # ===================================================================
    # Brand knowledge base
    # ===================================================================
    @router.get("/helena/brand", response_class=HTMLResponse)
    def brand_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        brand = brand_svc.get_or_create_brand(db)
        palette = json.loads(brand.palette_json) if brand.palette_json else []
        return templates.TemplateResponse(
            request, "helena/brand.html",
            ctx(request, user, "helena_brand", brand=brand,
                palette=palette, assets=brand_svc.list_assets(db),
                documents=brand_svc.list_documents(db)),
        )

    @router.post("/helena/brand")
    async def brand_save(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        form = await request.form()
        brand_svc.update_brand(db, {k: str(v) for k, v in form.items()}, user.id)
        flash(request, "Brand saved.")
        return RedirectResponse("/helena/brand", status_code=status.HTTP_303_SEE_OTHER)

    # ===================================================================
    # Email preview / edit
    # ===================================================================
    @router.get("/helena/email/{campaign_id}/preview", response_class=HTMLResponse)
    def email_preview(campaign_id: int, request: Request,
                      db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        camp = db.get(EmailCampaign, campaign_id)
        if camp is None:
            return PlainTextResponse("Not found", status_code=404)
        return templates.TemplateResponse(
            request, "helena/email_preview.html",
            ctx(request, user, "helena_chat", camp=camp),
        )

    @router.get("/helena/email/{campaign_id}/raw", response_class=HTMLResponse)
    def email_raw(campaign_id: int, request: Request,
                  db: DbDep) -> Response:
        # Served into the preview iframe.
        if current_user(request, db) is None:
            return PlainTextResponse("Unauthorized", status_code=401)
        camp = db.get(EmailCampaign, campaign_id)
        if camp is None or not camp.html:
            return HTMLResponse(
                "<p style='font-family:sans-serif;padding:24px'>No HTML rendered yet.</p>"
            )
        return HTMLResponse(camp.html)

    @router.post("/helena/email/{campaign_id}/html")
    async def email_save_html(campaign_id: int, request: Request,
                              db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        camp = db.get(EmailCampaign, campaign_id)
        if camp is None:
            return PlainTextResponse("Not found", status_code=404)
        form = await request.form()
        camp.html = str(form.get("html", camp.html))
        camp.updated_at = _now()
        db.commit()
        flash(request, "Email HTML saved.")
        return RedirectResponse(f"/helena/email/{campaign_id}/preview",
                                status_code=status.HTTP_303_SEE_OTHER)

    # ===================================================================
    # Chat history management (rename / delete / search)
    # ===================================================================
    @router.get("/helena/history", response_class=HTMLResponse)
    def history_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        q = request.query_params.get("q", "")
        return templates.TemplateResponse(
            request, "helena/history.html",
            ctx(request, user, "helena_history",
                q=q, sessions=agent_svc.search_sessions(db, q),
                scheduled=exec_svc.list_tasks(db)),
        )

    @router.post("/helena/chat/{session_id}/rename")
    async def chat_rename(session_id: int, request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        form = await request.form()
        agent_svc.rename_session(db, session_id, str(form.get("title", "")))
        nxt = str(form.get("redirect", "/helena/history"))
        return RedirectResponse(nxt, status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/chat/{session_id}/delete")
    async def chat_delete(session_id: int, request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        agent_svc.delete_session(db, session_id)
        flash(request, "Conversation deleted.")
        return RedirectResponse("/helena/history", status_code=status.HTTP_303_SEE_OTHER)

    # ===================================================================
    # Tasks (recurring + one-off scheduled jobs)
    # ===================================================================
    @router.get("/helena/tasks", response_class=HTMLResponse)
    def tasks_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        return templates.TemplateResponse(
            request, "helena/tasks.html",
            ctx(request, user, "helena_tasks",
                tasks=exec_svc.list_tasks(db),
                pending=exec_svc.pending_approvals(db)),
        )

    @router.post("/helena/tasks/{task_id}/pause")
    def task_pause(task_id: int, request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        exec_svc.pause(db, task_id)
        flash(request, "Task paused.")
        return RedirectResponse("/helena/tasks", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/tasks/{task_id}/resume")
    def task_resume(task_id: int, request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        exec_svc.resume(db, task_id)
        flash(request, "Task resumed.")
        return RedirectResponse("/helena/tasks", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/tasks/{task_id}/delete")
    def task_delete(task_id: int, request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        exec_svc.delete(db, task_id)
        flash(request, "Task deleted.")
        return RedirectResponse("/helena/tasks", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/tasks/{task_id}/edit")
    async def task_edit(task_id: int, request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        form = await request.form()
        run_after = None
        when = str(form.get("run_after", "")).strip()
        if when:
            try:
                run_after = datetime.fromisoformat(when)
                if run_after.tzinfo is None:
                    run_after = run_after.replace(tzinfo=UTC)
            except ValueError:
                run_after = None
        exec_svc.update_task(
            db, task_id, title=str(form.get("title", "")) or None,
            recurrence=str(form.get("recurrence", "")), run_after=run_after,
        )
        flash(request, "Task updated.")
        return RedirectResponse("/helena/tasks", status_code=status.HTTP_303_SEE_OTHER)

    # ===================================================================
    # Explore / Content Inspo
    # ===================================================================
    @router.get("/helena/explore", response_class=HTMLResponse)
    def explore_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        return templates.TemplateResponse(
            request, "helena/explore.html",
            ctx(request, user, "helena_explore",
                workflows=explore_svc.all_workflows(), types=explore_svc.TYPES),
        )

    @router.post("/helena/explore/launch")
    async def explore_launch(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        form = await request.form()
        wf = explore_svc.get(str(form.get("workflow", "")))
        if wf is None:
            return PlainTextResponse("Unknown workflow", status_code=404)
        s = agent_svc.create_session(db, title=wf["title"], user_id=user.id)
        from urllib.parse import quote
        return RedirectResponse(
            f"/helena/chat?session={s.id}&prefill={quote(wf['prompt'])}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # ===================================================================
    # Workspace files
    # ===================================================================
    @router.get("/helena/files", response_class=HTMLResponse)
    def files_page(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        return templates.TemplateResponse(
            request, "helena/files.html",
            ctx(request, user, "helena_files", files=files_svc.list_files(db)),
        )

    @router.post("/helena/files/delete")
    async def files_delete(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        form = await request.form()
        ok, err = files_svc.delete_file(db, str(form.get("ref", "")))
        flash(request, "File deleted." if ok else f"Couldn't delete: {err}",
              "info" if ok else "error")
        return RedirectResponse("/helena/files", status_code=status.HTTP_303_SEE_OTHER)

    # ===================================================================
    # Brand knowledge documents
    # ===================================================================
    @router.post("/helena/brand/docs")
    async def brand_doc_add(request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        form = await request.form()
        title = str(form.get("title", "")).strip()
        if title:
            brand_svc.add_document(
                db, title=title, content=str(form.get("content", "")),
                url=str(form.get("url", "")), user_id=user.id,
            )
            flash(request, "Document added to the brand knowledge base.")
        return RedirectResponse("/helena/brand", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/helena/brand/docs/{doc_id}/delete")
    def brand_doc_delete(doc_id: int, request: Request, db: DbDep) -> Response:
        user, deny = require_user(request, db)
        if deny:
            return deny
        brand_svc.delete_document(db, doc_id)
        flash(request, "Document removed.")
        return RedirectResponse("/helena/brand", status_code=status.HTTP_303_SEE_OTHER)

    return router
