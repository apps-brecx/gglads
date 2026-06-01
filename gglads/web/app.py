import json
import logging
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import EmailStr, ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from gglads import __version__
from gglads.auth.password import hash_password, verify_password
from gglads.config import get_settings
from gglads.db.session import get_db
from gglads.db.session import ping as db_ping
from gglads.models.campaign import AdCampaign, AdCampaignKeyword, AdGroup
from gglads.models.product_keywords import KeywordResearchRun, ProductKeyword
from gglads.models.shopify_product import (
    ProductSeoDraft,
    ShopifyCollection,
    ShopifyInventorySnapshot,
    ShopifyProduct,
    ShopifyProductCollection,
    ShopifyProductImage,
    ShopifyProductPublication,
    ShopifyPublication,
    ShopifyVariant,
)
from gglads.models.user import User
from gglads.services import integration_tests, integrations as integrations_svc
from gglads.services import keyword_research as kw_research_svc
from gglads.services import search_console as sc_svc
from gglads.services import ad_copy_generation as ad_copy_svc
from gglads.services import campaigns as campaigns_svc
from gglads.services import keyword_placement as kw_place_svc
from gglads.services import seo_chat as seo_chat_svc
from gglads.services import seo_generation as seo_svc
from gglads.services import shopify as shopify_svc
from gglads.services import user_prefs as prefs_svc
from gglads.web import listing as listing_util

logger = logging.getLogger("gglads.web")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

OPEN_PATHS = {"/login", "/setup", "/healthz", "/readyz", "/favicon.ico"}

settings = get_settings()

app = FastAPI(title="gglads", version=__version__)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


DbDep = Annotated[Session, Depends(get_db)]


def _user_count(db: Session) -> int:
    return db.scalar(select(func.count(User.id))) or 0


def _current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))


class AuthGateMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/") or path in OPEN_PATHS:
            return await call_next(request)
        if request.session.get("user_id"):
            return await call_next(request)
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


# Order matters: the LAST add_middleware call becomes the OUTERMOST wrapper
# (runs first on a request). SessionMiddleware must run before AuthGateMiddleware
# so that request.session is populated when AuthGate reads it.
app.add_middleware(AuthGateMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret,
    https_only=settings.app_env == "production",
    same_site="lax",
    session_cookie="gglads_session",
)


# ---------------------------------------------------------------------------
# Health endpoints (open)
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


@app.get("/readyz")
def readyz() -> JSONResponse:
    ok, detail = db_ping()
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "database": "ok" if ok else detail},
        status_code=200 if ok else 503,
    )


# ---------------------------------------------------------------------------
# First-time setup (only when no users exist)
# ---------------------------------------------------------------------------

@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: DbDep) -> Response:
    if _user_count(db) > 0:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "setup.html", {"version": __version__, "error": None}
    )


@app.post("/setup", response_class=HTMLResponse)
def setup_submit(
    request: Request,
    db: DbDep,
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    if _user_count(db) > 0:
        raise HTTPException(status_code=404)

    error = None
    try:
        from pydantic import TypeAdapter

        email_norm = TypeAdapter(EmailStr).validate_python(email).lower().strip()
    except ValidationError:
        error = "That email address doesn't look right."
        email_norm = email

    if not error and len(password) < 8:
        error = "Password must be at least 8 characters."

    if not error and not name.strip():
        error = "Name is required."

    if error:
        return templates.TemplateResponse(
            request, "setup.html", {"version": __version__, "error": error}
        )

    user = User(
        email=email_norm,
        name=name.strip(),
        password_hash=hash_password(password),
        role="admin",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: DbDep) -> Response:
    if _user_count(db) == 0:
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "login.html", {"version": __version__, "error": None}
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: DbDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    if _user_count(db) == 0:
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)

    email_norm = email.lower().strip()
    user = db.scalar(select(User).where(User.email == email_norm, User.is_active.is_(True)))
    if not user or not user.password_hash or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"version": __version__, "error": "Invalid email or password."},
        )

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Authenticated pages
# ---------------------------------------------------------------------------

_DASHBOARD_PERIODS = [(7, "7 days"), (30, "30 days"), (90, "90 days")]
# Stable colors per channel — also referenced by the donut + chart legend.
_CHANNEL_COLORS = {
    "web":  "#7c9cff",
    "shop": "#f4a261",
}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    from gglads.services import analytics as analytics_svc
    from gglads.services import chart_svg as chart_svc

    try:
        days = int(request.query_params.get("days") or 30)
    except ValueError:
        days = 30
    if days not in (p[0] for p in _DASHBOARD_PERIODS):
        days = 30
    metric = request.query_params.get("metric") or "revenue"
    if metric not in ("revenue", "orders", "units", "customers"):
        metric = "revenue"
    selected_channels = request.query_params.getlist("channel")
    if not selected_channels:
        selected_channels = list(analytics_svc.CHANNEL_LABELS.keys())
    selected_channels = [
        c for c in selected_channels if c in analytics_svc.CHANNEL_LABELS
    ] or list(analytics_svc.CHANNEL_LABELS.keys())

    growth = analytics_svc.growth_summary(db, days, channels=selected_channels)
    daily = analytics_svc.daily_totals(db, days, channels=selected_channels)
    split = analytics_svc.channel_split(db, days)
    movers = analytics_svc.top_movers(db, days, limit=8)

    # Pull a per-channel daily breakdown so the chart can show stacked lines.
    chart_series: dict[str, list[float]] = {}
    if metric in ("revenue", "orders", "units", "customers"):
        # Total line (selected channels combined) always shown.
        chart_series["All selected"] = [float(d[metric]) for d in daily]
        # Per-channel split for any channel actually selected.
        if len(selected_channels) > 1:
            for ch in selected_channels:
                ch_series = analytics_svc.daily_totals(db, days, channels=[ch])
                chart_series[analytics_svc.CHANNEL_LABELS.get(ch, ch)] = [
                    float(d[metric]) for d in ch_series
                ]
    chart = chart_svc.line_chart(
        [d["day"] for d in daily], chart_series, width=860, height=240
    )

    donut = chart_svc.donut(
        [
            {
                "label": s["label"],
                "value": float(s["revenue"]),
                "color": _CHANNEL_COLORS.get(s["channel"], "#7c9cff"),
            }
            for s in split
        ],
        size=160,
        stroke=24,
    )

    # Top movers each get a sparkline of daily revenue over the same window.
    mover_views = []
    for m in movers:
        spark_vals = analytics_svc.product_sparkline(db, m["product_id"], days)
        mover_views.append({
            **m,
            "sparkline": chart_svc.sparkline(spark_vals, width=100, height=24),
        })

    latest_sync = analytics_svc.latest_sync_date(db)

    # Organic growth alerts — last 7 days vs prior comparable snapshot.
    from gglads.services import keyword_history as kh_svc
    alerts = kh_svc.growth_alerts(db, days_back=7, limit=12)
    alert_counts = kh_svc.alert_counts_by_type(alerts)
    on_the_rise = kh_svc.keywords_gained_per_product(db, days_back=7)[:6]

    # Audit panel: which channels did the most recent sales sync see, and
    # which did it drop? So the user can verify the filter is doing its job.
    last_sales_run = shopify_svc.last_sync_runs_by_kind(db).get("sales") \
        or shopify_svc.last_sync_runs_by_kind(db).get("full")
    last_sales_detail = (last_sales_run.detail if last_sales_run else None) or ""
    tracked_channels_display = sorted(shopify_svc.TRACKED_CHANNELS)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "version": __version__,
            "user": user,
            "active": "dashboard",
            "days": days,
            "metric": metric,
            "periods": _DASHBOARD_PERIODS,
            "metrics": [
                ("revenue", "Revenue"),
                ("orders", "Orders"),
                ("units", "Units sold"),
                ("customers", "Customers"),
            ],
            "growth": growth,
            "chart": chart,
            "donut": donut,
            "channel_split": split,
            "channel_colors": _CHANNEL_COLORS,
            "channel_labels": analytics_svc.CHANNEL_LABELS,
            "selected_channels": selected_channels,
            "movers": mover_views,
            "latest_sync": latest_sync,
            "alerts": alerts,
            "alert_counts": alert_counts,
            "on_the_rise": on_the_rise,
            "tracked_channels": tracked_channels_display,
            "last_sales_run_detail": last_sales_detail,
        },
    )


def _require_admin(request: Request, db: Session) -> tuple[User | None, Response | None]:
    user = _current_user(request, db)
    if user is None:
        return None, RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role != "admin":
        return None, PlainTextResponse("Forbidden", status_code=403)
    return user, None


def _flash(request: Request, message: str, level: str = "info") -> None:
    flashes = request.session.get("flashes", [])
    flashes.append({"message": message, "level": level})
    request.session["flashes"] = flashes


def _consume_flashes(request: Request) -> list[dict]:
    flashes = request.session.pop("flashes", [])
    return flashes


_INTEGRATION_ROUTE_TO_NAME = {
    "anthropic": "anthropic",
    "shopify": "shopify",
    "google-ads": "google_ads",
    "google-search-console": "google_search_console",
}


@app.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    integrations_state = {
        name: integrations_svc.summarize_for_form(db, name)
        for name in ("anthropic", "shopify", "google_ads", "google_search_console")
    }
    return templates.TemplateResponse(
        request,
        "connections.html",
        {
            "version": __version__,
            "user": user,
            "active": "connections",
            "integrations": integrations_state,
            "flashes": _consume_flashes(request),
        },
    )


@app.post("/connections/{route}/save")
async def connections_save(route: str, request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    name = _INTEGRATION_ROUTE_TO_NAME.get(route)
    if name is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    incoming = {k: str(v) for k, v in form.items()}
    integrations_svc.save_config(db, name, incoming, user.id)
    _flash(request, f"Saved {name.replace('_', ' ').title()} credentials.", "ok")
    return RedirectResponse("/connections", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/connections/{route}/test")
def connections_test(route: str, request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    name = _INTEGRATION_ROUTE_TO_NAME.get(route)
    if name is None:
        raise HTTPException(status_code=404)
    config = integrations_svc.get_config(db, name)
    tester = integration_tests.TESTERS[name]
    ok, detail = tester(config)
    integrations_svc.record_test(db, name, ok, detail)
    _flash(
        request,
        f"{name.replace('_', ' ').title()}: {detail}",
        "ok" if ok else "error",
    )
    return RedirectResponse("/connections", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/connections/{route}/disconnect")
def connections_disconnect(route: str, request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    name = _INTEGRATION_ROUTE_TO_NAME.get(route)
    if name is None:
        raise HTTPException(status_code=404)
    integrations_svc.delete_config(db, name)
    _flash(request, f"Disconnected {name.replace('_', ' ').title()}.", "info")
    return RedirectResponse("/connections", status_code=status.HTTP_303_SEE_OTHER)


# Backwards-compat: earlier templates posted to /connections/{route} without
# the /save suffix. Keep this alias so any stale page in a user's browser
# still works.
@app.post("/connections/{route}")
async def connections_save_alias(route: str, request: Request, db: DbDep) -> Response:
    if route not in _INTEGRATION_ROUTE_TO_NAME:
        raise HTTPException(status_code=404)
    return await connections_save(route, request, db)


TRAINING_CATEGORIES = [
    {
        "slug": "voice",
        "name": "Brand voice & tone",
        "desc": "How ad copy should sound, which words to use, which to avoid.",
        "suggestions": [
            "How should ad copy sound? (friendly, expert, urgent, etc.)",
            "Which pronouns do we use — 'we', 'us', 'you', 'your'?",
            "Are there phrases we always use? Phrases we never use?",
        ],
    },
    {
        "slug": "catalog",
        "name": "Products & catalog",
        "desc": "What you sell, top categories, margins, hero products.",
        "suggestions": [
            "What are your top 3 product categories?",
            "Which products are your bestsellers vs. highest margin?",
            "Are there any products we should NOT advertise?",
        ],
    },
    {
        "slug": "customer",
        "name": "Target customer",
        "desc": "Who buys from you, what they care about, what objections they have.",
        "suggestions": [
            "Who is your ideal customer? (age, profession, lifestyle)",
            "What pain point does your product solve?",
            "What objections do customers have before buying?",
        ],
    },
    {
        "slug": "geo",
        "name": "Geography & language",
        "desc": "Where you sell, languages, regional constraints.",
        "suggestions": [
            "Which countries / regions do you sell to?",
            "What languages should ads run in?",
            "Are there shipping or fulfillment limits we should mention?",
        ],
    },
    {
        "slug": "guardrails",
        "name": "Do's & Don'ts",
        "desc": "Hard rules Claude must always or never follow.",
        "suggestions": [
            "Are there claims we cannot make (legal, FTC, etc.)?",
            "Should we ever discount? What's the minimum margin?",
            "Are there competitors we should never mention by name?",
        ],
    },
    {
        "slug": "banned",
        "name": "Banned terms",
        "desc": "Words / phrases / brand names that must never appear.",
        "suggestions": [
            "Are there words our brand never uses?",
            "Are there competitor names we cannot bid on?",
            "Any regulatory words to avoid (FDA, supplement claims, etc.)?",
        ],
    },
    {
        "slug": "required",
        "name": "Must-include",
        "desc": "Phrases or claims that should appear when relevant.",
        "suggestions": [
            "Do you have a tagline we should use?",
            "Are there claims you want in every ad? (e.g. 'free shipping')",
            "Are there disclosures we are legally required to include?",
        ],
    },
    {
        "slug": "promos",
        "name": "Promotions & seasonality",
        "desc": "Always-on offers, seasonal events, when to ramp.",
        "suggestions": [
            "Are there always-on promotions?",
            "What seasonal events matter? (Black Friday, summer sale, etc.)",
            "How early should we ramp spend before a seasonal event?",
        ],
    },
    {
        "slug": "competitors",
        "name": "Competitors",
        "desc": "Who they are, how to position against them.",
        "suggestions": [
            "Who are your main competitors?",
            "What do you do better than them?",
            "Should we bid on competitor brand names?",
        ],
    },
    {
        "slug": "other",
        "name": "Other context",
        "desc": "Anything else Claude should know.",
        "suggestions": [
            "Is there anything else Claude should know about the business?",
        ],
    },
]


def _categories_with_entries(_db: Session) -> list[dict]:
    # Backend is not built yet — all entries lists are empty. We still seed
    # categories so the design preview shows real structure + suggestions.
    return [{**c, "entries": []} for c in TRAINING_CATEGORIES]


@app.get("/training", response_class=HTMLResponse)
def training_page(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    return templates.TemplateResponse(
        request,
        "training.html",
        {
            "version": __version__,
            "user": user,
            "active": "training",
            "categories": _categories_with_entries(db),
            "flashes": _consume_flashes(request),
        },
    )


@app.get("/training/new", response_class=HTMLResponse)
def training_new(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    current_category = request.query_params.get("category", "voice")
    current_question = request.query_params.get("question", "")
    return templates.TemplateResponse(
        request,
        "training_form.html",
        {
            "version": __version__,
            "user": user,
            "active": "training",
            "categories": TRAINING_CATEGORIES,
            "current_category": current_category,
            "current_question": current_question,
            "current_answer": "",
            "entry": None,
            "form_action": "/training/new",
            "error": None,
        },
    )


@app.post("/training/new")
def training_new_submit(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    _flash(request, "Backend not yet built — design preview only.", "info")
    return RedirectResponse("/training", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/training/{entry_id}/edit", response_class=HTMLResponse)
def training_edit(entry_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    # Backend not built — placeholder entry so the form renders.
    return templates.TemplateResponse(
        request,
        "training_form.html",
        {
            "version": __version__,
            "user": user,
            "active": "training",
            "categories": TRAINING_CATEGORIES,
            "current_category": "voice",
            "current_question": "(example) How should ad copy sound?",
            "current_answer": "(example) Friendly, expert, never pushy.",
            "entry": {"id": entry_id},
            "form_action": f"/training/{entry_id}/edit",
            "error": None,
        },
    )


@app.post("/training/{entry_id}/edit")
def training_edit_submit(entry_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    _flash(request, "Backend not yet built — design preview only.", "info")
    return RedirectResponse("/training", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/training/{entry_id}/delete")
def training_delete(entry_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    _flash(request, "Backend not yet built — design preview only.", "info")
    return RedirectResponse("/training", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Products (Shopify mirror)
# ---------------------------------------------------------------------------

PRODUCT_COLUMNS = [
    ("image", "Image"),
    ("price", "Price"),
    ("sold", "Units sold (90d)"),
    ("customers", "Customers (90d)"),
    ("sku", "SKU"),
    ("inventory", "Stock now"),
    ("stock_history", "Stock days (30d)"),
    ("vendor", "Vendor"),
    ("type", "Product type"),
    ("variants", "Variants"),
    ("collections", "Collections"),
    ("channels", "Channels"),
    ("status", "Status"),
]

DEFAULT_COLUMNS = {
    "image",
    "price",
    "sold",
    "customers",
    "inventory",
    "stock_history",
    "collections",
    "status",
}

# These slugs are merged into a single virtual "online" channel filter.
ONLINE_SLUGS = {"online_store", "shop"}


def _shopify_status(db: Session) -> bool:
    cfg = integrations_svc.get_config(db, "shopify")
    return integrations_svc.is_configured(cfg, integrations_svc.required_keys("shopify"))


def _parse_view_params(request: Request) -> tuple[str, set[str]]:
    view = request.query_params.get("view") or "grid"
    if view not in ("grid", "list"):
        view = "grid"
    cols_raw = request.query_params.getlist("cols")
    if cols_raw:
        cols = {c for c in cols_raw if c in dict(PRODUCT_COLUMNS)}
    else:
        cols = set(DEFAULT_COLUMNS)
    return view, cols


def _format_price(p: ShopifyProduct) -> str:
    if p.price_min is None and p.price_max is None:
        return "—"
    currency_symbol = "$" if (p.currency in (None, "USD")) else ""
    if p.price_min is not None and p.price_max is not None and p.price_min != p.price_max:
        return f"{currency_symbol}{p.price_min:.2f} – {currency_symbol}{p.price_max:.2f}"
    val = p.price_min if p.price_min is not None else p.price_max
    return f"{currency_symbol}{val:.2f}"


def _product_to_dict(
    p: ShopifyProduct,
    collection_titles: list[str],
    channel_names: list[str],
    stock_history: tuple[int, int, int],
) -> dict:
    in_days, out_days, total_days = stock_history
    return {
        "id": p.id,
        "title": p.title,
        "status": p.status,
        "image_url": p.image_url,
        "price_range": _format_price(p),
        "sku": p.first_sku or "—",
        "inventory": p.total_inventory,
        "vendor": p.vendor or "—",
        "product_type": p.product_type or "—",
        "variant_count": p.variant_count,
        "units_sold_90d": p.units_sold_90d,
        "unique_customers_90d": p.unique_customers_90d,
        "stock_in_days": in_days,
        "stock_out_days": out_days,
        "stock_total_days": total_days,
        "collection_titles": collection_titles,
        "channel_names": channel_names,
    }


def _stock_history(
    db: Session, product_ids: list[int]
) -> dict[int, tuple[int, int, int]]:
    """Return {product_id: (in_stock_days, out_of_stock_days, total_days)} for last 30 days."""
    if not product_ids:
        return {}
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=30)
    rows = db.execute(
        select(
            ShopifyInventorySnapshot.product_id,
            func.sum(
                case((ShopifyInventorySnapshot.is_in_stock, 1), else_=0)
            ).label("in_days"),
            func.count().label("total"),
        )
        .where(ShopifyInventorySnapshot.product_id.in_(product_ids))
        .where(ShopifyInventorySnapshot.snapshot_date >= cutoff)
        .group_by(ShopifyInventorySnapshot.product_id)
    ).all()
    out: dict[int, tuple[int, int, int]] = {pid: (0, 0, 0) for pid in product_ids}
    for pid, in_days, total in rows:
        in_days = int(in_days or 0)
        total = int(total or 0)
        out[pid] = (in_days, total - in_days, total)
    return out


def _collections_summary(db: Session) -> list[dict]:
    rows = db.execute(
        select(ShopifyCollection).order_by(ShopifyCollection.title)
    ).scalars().all()
    return [
        {
            "handle": c.handle,
            "title": c.title,
            "description": c.description or "",
            "image_url": c.image_url,
            "product_count": c.product_count,
        }
        for c in rows
    ]


def _last_sync_display(db: Session) -> str | None:
    run = shopify_svc.last_sync_run(db)
    if run is None or run.finished_at is None:
        return None
    return run.finished_at.strftime("%Y-%m-%d %H:%M UTC")


def _sync_kind_summary(db: Session) -> list[dict]:
    """Per-kind sync card data for /products."""
    runs = shopify_svc.last_sync_runs_by_kind(db)
    kinds = [
        ("full", "Full"),
        ("catalog", "Catalog"),
        ("sales", "Sales"),
        ("inventory", "Inventory"),
    ]
    out: list[dict] = []
    for slug, label in kinds:
        r = runs.get(slug)
        out.append({
            "slug": slug,
            "label": label,
            "last_at": r.finished_at.strftime("%Y-%m-%d %H:%M UTC")
                if r and r.finished_at else None,
            "ok": r.ok if r else None,
            "detail": (r.detail or "")[:200] if r else None,
        })
    return out


def _product_collection_titles(db: Session, product_ids: list[int]) -> dict[int, list[str]]:
    if not product_ids:
        return {}
    rows = db.execute(
        select(
            ShopifyProductCollection.product_id,
            ShopifyCollection.title,
        )
        .join(
            ShopifyCollection,
            ShopifyCollection.id == ShopifyProductCollection.collection_id,
        )
        .where(ShopifyProductCollection.product_id.in_(product_ids))
        .order_by(ShopifyCollection.title)
    ).all()
    by_pid: dict[int, list[str]] = {pid: [] for pid in product_ids}
    for pid, title in rows:
        by_pid[pid].append(title)
    return by_pid


def _product_channel_names(db: Session, product_ids: list[int]) -> dict[int, list[str]]:
    """Return channel display names per product, with online_store + shop collapsed."""
    if not product_ids:
        return {}
    rows = db.execute(
        select(
            ShopifyProductPublication.product_id,
            ShopifyPublication.name,
            ShopifyPublication.slug,
        )
        .join(
            ShopifyPublication,
            ShopifyPublication.id == ShopifyProductPublication.publication_id,
        )
        .where(ShopifyProductPublication.product_id.in_(product_ids))
        .order_by(ShopifyPublication.name)
    ).all()
    by_pid: dict[int, list[str]] = {pid: [] for pid in product_ids}
    seen_online: dict[int, bool] = {}
    for pid, name, slug in rows:
        if slug in ONLINE_SLUGS:
            if not seen_online.get(pid):
                by_pid[pid].append("Online")
                seen_online[pid] = True
        else:
            by_pid[pid].append(name)
    return by_pid


def _channel_options(db: Session) -> list[dict]:
    """Build the channel filter dropdown options."""
    pubs = db.execute(
        select(ShopifyPublication).order_by(ShopifyPublication.name)
    ).scalars().all()
    options: list[dict] = []
    has_online = any(p.slug in ONLINE_SLUGS for p in pubs)
    if has_online:
        options.append({"slug": "online", "label": "Online (Store + Shop app)"})
    for p in pubs:
        if p.slug in ONLINE_SLUGS:
            continue
        options.append({"slug": p.slug, "label": p.name})
    return options


@app.get("/collections", response_class=HTMLResponse)
def collections_index(request: Request, db: DbDep) -> Response:
    """Grid of collections — formerly /products. Each card links to its detail
    page where SEO/keywords/organic queries are managed."""
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    from gglads.services import collections as collections_svc
    from gglads.services import collection_suggestions as cs_svc

    q = (request.query_params.get("q") or "").strip().lower()
    cols = collections_svc.list_collections(db)
    if q:
        cols = [c for c in cols if q in c["title"].lower()]

    total_products = db.scalar(select(func.count(ShopifyProduct.id))) or 0
    active_products = db.scalar(
        select(func.count(ShopifyProduct.id)).where(ShopifyProduct.status == "active")
    ) or 0

    pending_suggestions = cs_svc.list_pending(db, limit=8)
    suggestion_views = [
        {
            "id": s.id,
            "title": s.title,
            "handle": s.handle,
            "seo_title": s.seo_title,
            "seo_meta_description": s.seo_meta_description,
            "rationale": s.rationale,
            "opportunity_score": s.opportunity_score,
            "theme_keywords": cs_svc.parse_keywords(s),
            "generated_at": s.generated_at,
        }
        for s in pending_suggestions
    ]

    return templates.TemplateResponse(
        request,
        "collections.html",
        {
            "version": __version__,
            "user": user,
            "active": "collections",
            "collections": cols,
            "total_count": total_products,
            "active_count": active_products,
            "last_synced": _last_sync_display(db),
            "sync_kinds": _sync_kind_summary(db),
            "query": q,
            "shopify_connected": _shopify_status(db),
            "suggestions": suggestion_views,
            "flashes": _consume_flashes(request),
        },
    )


@app.post("/collections/suggestions/generate")
def collection_suggestions_generate(request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import collection_suggestions as cs_svc
    ok, detail, _ = cs_svc.generate_suggestions(db, days=90, max_suggestions=8)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse("/collections", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/collections/suggestions/{suggestion_id}/dismiss")
def collection_suggestion_dismiss(
    suggestion_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import collection_suggestions as cs_svc
    ok, detail = cs_svc.dismiss(db, suggestion_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse("/collections", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/collections/suggestions/{suggestion_id}/mark-created")
def collection_suggestion_mark_created(
    suggestion_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import collection_suggestions as cs_svc
    ok, detail = cs_svc.mark_created(db, suggestion_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse("/collections", status_code=status.HTTP_303_SEE_OTHER)


# Back-compat: the old grid lived at /products. Now /products is the list of
# products and the grid moved to /collections.
@app.get("/products/grid")
def products_grid_legacy() -> Response:
    return RedirectResponse("/collections", status_code=status.HTTP_308_PERMANENT_REDIRECT)


def _render_products_list(
    request: Request,
    db: Session,
    user: User,
    products: list[ShopifyProduct],
    *,
    collection: ShopifyCollection | None,
    listing: dict | None = None,
) -> Response:
    view, cols = _parse_view_params(request)
    pids = [p.id for p in products]
    titles_by_pid = _product_collection_titles(db, pids)
    channels_by_pid = _product_channel_names(db, pids)
    stock_by_pid = _stock_history(db, pids)
    items = []
    for p in products:
        d = _product_to_dict(
            p,
            titles_by_pid.get(p.id, []),
            channels_by_pid.get(p.id, []),
            stock_by_pid.get(p.id, (0, 0, 0)),
        )
        # Surface the new is_ignored flag to the template.
        d["is_ignored"] = bool(getattr(p, "is_ignored", False))
        items.append(d)
    total_count = db.scalar(select(func.count(ShopifyProduct.id))) or 0
    drafts_hidden = db.scalar(
        select(func.count(ShopifyProduct.id)).where(
            ShopifyProduct.status == "draft"
        )
    ) or 0
    ignored_count = db.scalar(
        select(func.count(ShopifyProduct.id)).where(
            ShopifyProduct.is_ignored.is_(True)
        )
    ) or 0

    return templates.TemplateResponse(
        request,
        "products_list.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "heading": collection.title if collection else "All products",
            "collection": {
                "id": collection.id,
                "title": collection.title,
                "handle": collection.handle,
            } if collection else None,
            "items": items,
            "total_count": total_count,
            "drafts_hidden": drafts_hidden,
            "ignored_count": ignored_count,
            "include_drafts": request.query_params.get("include_drafts") in ("1", "true", "on"),
            "include_ignored": request.query_params.get("include_ignored") in ("1", "true", "on"),
            "collections": _collections_summary(db),
            "channels": _channel_options(db),
            "query": (request.query_params.get("q") or "").strip(),
            "status_filter": request.query_params.get("status") or "",
            "collection_filter": request.query_params.get("collection") or "",
            "channel_filter": request.query_params.get("channel") or "",
            "view": view,
            "cols": cols,
            "available_columns": PRODUCT_COLUMNS,
            "passthrough_qs": [
                ("view", view),
                *[("cols", c) for c in cols],
            ],
            "listing": listing or {},
            "flashes": _consume_flashes(request),
        },
    )


def _apply_filters(
    db: Session,
    base_query,
    q: str,
    status_filter: str,
    collection_handle: str | None,
    channel_filter: str | None,
    *,
    include_drafts: bool = True,
    include_ignored: bool = True,
):
    if q:
        base_query = base_query.where(ShopifyProduct.title.ilike(f"%{q}%"))
    if status_filter:
        # User picked a specific status — honor it (overrides include_drafts).
        base_query = base_query.where(ShopifyProduct.status == status_filter)
    elif not include_drafts:
        # Default behavior: hide drafts unless the user asks to see them.
        base_query = base_query.where(ShopifyProduct.status != "draft")
    if not include_ignored:
        base_query = base_query.where(ShopifyProduct.is_ignored.is_(False))
    if collection_handle:
        coll = db.scalar(
            select(ShopifyCollection).where(ShopifyCollection.handle == collection_handle)
        )
        if coll is not None:
            base_query = base_query.join(
                ShopifyProductCollection,
                ShopifyProductCollection.product_id == ShopifyProduct.id,
            ).where(ShopifyProductCollection.collection_id == coll.id)
    if channel_filter:
        if channel_filter == "online":
            slugs = list(ONLINE_SLUGS)
        else:
            slugs = [channel_filter]
        pub_ids = db.execute(
            select(ShopifyPublication.id).where(ShopifyPublication.slug.in_(slugs))
        ).scalars().all()
        if pub_ids:
            base_query = base_query.join(
                ShopifyProductPublication,
                ShopifyProductPublication.product_id == ShopifyProduct.id,
            ).where(ShopifyProductPublication.publication_id.in_(pub_ids))
    return base_query


_PRODUCT_SORT_KEYS = {
    "title", "units_sold", "net_sales", "buyers", "stock", "stock_days_in",
}
_PRODUCT_SORT_COLUMNS = {
    "title": ShopifyProduct.title,
    "units_sold": ShopifyProduct.units_sold_90d,
    "net_sales": ShopifyProduct.net_sales_90d,
    "buyers": ShopifyProduct.unique_customers_90d,
    "stock": ShopifyProduct.total_inventory,
}


@app.get("/products/all")
def products_all_legacy() -> Response:
    return RedirectResponse("/products", status_code=status.HTTP_308_PERMANENT_REDIRECT)


@app.get("/products", response_class=HTMLResponse)
def products_index(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    q = (request.query_params.get("q") or "").strip()
    status_filter = request.query_params.get("status") or ""
    collection_filter = request.query_params.get("collection") or ""
    channel_filter = request.query_params.get("channel") or ""
    include_drafts = request.query_params.get("include_drafts") in ("1", "true", "on")
    include_ignored = request.query_params.get("include_ignored") in ("1", "true", "on")

    sort = listing_util.parse_sort(
        request.query_params.get("sort"), _PRODUCT_SORT_KEYS, "units_sold"
    )
    direction = listing_util.parse_direction(request.query_params.get("dir"), "desc")
    per_page = listing_util.parse_per_page(request.query_params.get("per_page"))
    page = listing_util.parse_page(request.query_params.get("page"))

    base = select(ShopifyProduct)
    base = _apply_filters(
        db, base, q, status_filter, collection_filter, channel_filter,
        include_drafts=include_drafts,
        include_ignored=include_ignored,
    )

    # Sort
    col = _PRODUCT_SORT_COLUMNS.get(sort, ShopifyProduct.units_sold_90d)
    base = base.order_by(col.desc() if direction == "desc" else col.asc())

    # Total count for pagination (compile a separate count query)
    total = db.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    products = db.execute(base.limit(per_page).offset(offset)).scalars().unique().all()

    return _render_products_list(
        request,
        db,
        user,
        products,
        collection=None,
        listing={
            "sort": sort,
            "direction": direction,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total": total,
            "qs_no_page": listing_util.query_string_for(request, drop=["page"]),
            "qs_no_sort": listing_util.query_string_for(request, drop=["sort", "dir", "page"]),
        },
    )


@app.get("/products/collection/{handle}")
def products_collection_legacy(handle: str) -> Response:
    return RedirectResponse(
        f"/collections/{handle}/products",
        status_code=status.HTTP_308_PERMANENT_REDIRECT,
    )


@app.get("/collections/{handle}/products", response_class=HTMLResponse)
def collection_products_list(handle: str, request: Request, db: DbDep) -> Response:
    """Filtered product list scoped to one collection (with the standard
    products_list.html table)."""
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    collection = db.scalar(
        select(ShopifyCollection).where(ShopifyCollection.handle == handle)
    )
    if collection is None:
        raise HTTPException(status_code=404)

    q = (request.query_params.get("q") or "").strip()
    status_filter = request.query_params.get("status") or ""
    channel_filter = request.query_params.get("channel") or ""

    sort = listing_util.parse_sort(
        request.query_params.get("sort"), _PRODUCT_SORT_KEYS, "units_sold"
    )
    direction = listing_util.parse_direction(request.query_params.get("dir"), "desc")
    per_page = listing_util.parse_per_page(request.query_params.get("per_page"))
    page = listing_util.parse_page(request.query_params.get("page"))

    query = (
        select(ShopifyProduct)
        .join(
            ShopifyProductCollection,
            ShopifyProductCollection.product_id == ShopifyProduct.id,
        )
        .where(ShopifyProductCollection.collection_id == collection.id)
    )
    if q:
        query = query.where(ShopifyProduct.title.ilike(f"%{q}%"))
    if status_filter:
        query = query.where(ShopifyProduct.status == status_filter)
    if channel_filter:
        if channel_filter == "online":
            slugs = list(ONLINE_SLUGS)
        else:
            slugs = [channel_filter]
        pub_ids = db.execute(
            select(ShopifyPublication.id).where(ShopifyPublication.slug.in_(slugs))
        ).scalars().all()
        if pub_ids:
            query = query.join(
                ShopifyProductPublication,
                ShopifyProductPublication.product_id == ShopifyProduct.id,
            ).where(ShopifyProductPublication.publication_id.in_(pub_ids))

    col = _PRODUCT_SORT_COLUMNS.get(sort, ShopifyProduct.units_sold_90d)
    query = query.order_by(col.desc() if direction == "desc" else col.asc())

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    products = db.execute(query.limit(per_page).offset(offset)).scalars().unique().all()

    return _render_products_list(
        request,
        db,
        user,
        products,
        collection=collection,
        listing={
            "sort": sort,
            "direction": direction,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total": total,
            "qs_no_page": listing_util.query_string_for(request, drop=["page"]),
            "qs_no_sort": listing_util.query_string_for(request, drop=["sort", "dir", "page"]),
        },
    )


@app.get("/collections/{handle}", response_class=HTMLResponse)
def collection_detail(handle: str, request: Request, db: DbDep) -> Response:
    """Collection SEO + linked products + organic queries landing on this URL."""
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    from gglads.services import collections as collections_svc

    c = collections_svc.get_collection(db, handle)
    if c is None:
        raise HTTPException(status_code=404)

    products = collections_svc.products_in_collection(db, c.id)
    pids = [p.id for p in products]

    # Count of product-level keywords per product, so we can flag products
    # with no keyword research yet (i.e. ones that need a one-time generate).
    kw_counts: dict[int, int] = {}
    if pids:
        rows = db.execute(
            select(
                ProductKeyword.product_id, func.count(ProductKeyword.id)
            )
            .where(ProductKeyword.product_id.in_(pids))
            .group_by(ProductKeyword.product_id)
        ).all()
        kw_counts = {pid: int(n) for pid, n in rows}

    product_rows = []
    for p in products:
        product_rows.append({
            "id": p.id,
            "title": p.title,
            "handle": p.handle,
            "image_url": p.image_url,
            "status": p.status,
            "units_sold_90d": p.units_sold_90d,
            "net_sales_90d": p.net_sales_90d,
            "keyword_count": kw_counts.get(p.id, 0),
        })

    # Organic queries landing on this collection's URL.
    organic_rows, organic_err = collections_svc.organic_queries(
        db, handle, days=90, row_limit=50
    )
    page_url = collections_svc.page_url_for_collection(db, handle)

    return templates.TemplateResponse(
        request,
        "collection_detail.html",
        {
            "version": __version__,
            "user": user,
            "active": "collections",
            "collection": c,
            "page_url": page_url,
            "product_rows": product_rows,
            "organic_rows": organic_rows or [],
            "organic_err": organic_err,
            "flashes": _consume_flashes(request),
        },
    )


@app.post("/collections/{handle}/seo")
async def collection_seo_save(handle: str, request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import collections as collections_svc
    c = collections_svc.get_collection(db, handle)
    if c is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    ok, detail = collections_svc.update_seo(
        db,
        c.id,
        seo_title=form.get("seo_title"),
        seo_meta_description=form.get("seo_meta_description"),
        description=form.get("description"),
    )
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(f"/collections/{handle}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/collections/{handle}/seo/generate")
def collection_seo_generate(handle: str, request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import collections as collections_svc
    c = collections_svc.get_collection(db, handle)
    if c is None:
        raise HTTPException(status_code=404)
    ok, detail, _ = collections_svc.generate_seo(db, c.id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(f"/collections/{handle}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/products/out-of-stock", response_class=HTMLResponse)
def products_oos(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    from gglads.services import oos as oos_svc

    include_ignored = request.query_params.get("include_ignored") in ("1", "true", "on")
    collection_handle = request.query_params.get("collection") or None
    q = (request.query_params.get("q") or "").strip() or None
    # "since" is a small enum: '', '7', '14', '30' (within N days),
    # or 'old' (over 30 days ago).
    since = (request.query_params.get("since") or "").strip()
    oos_within_days: int | None = None
    oos_older_than_days: int | None = None
    if since in ("7", "14", "30"):
        oos_within_days = int(since)
    elif since == "old":
        oos_older_than_days = 30
    rows = oos_svc.list_out_of_stock(
        db,
        include_ignored=include_ignored,
        collection_handle=collection_handle,
        q=q,
        oos_within_days=oos_within_days,
        oos_older_than_days=oos_older_than_days,
    )
    counts = oos_svc.oos_counts(db)
    return templates.TemplateResponse(
        request,
        "products_oos.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "rows": rows,
            "counts": counts,
            "include_ignored": include_ignored,
            "collection_handle": collection_handle,
            "q": q or "",
            "since": since,
            "collections": _collections_summary(db),
            "flashes": _consume_flashes(request),
        },
    )


@app.post("/products/out-of-stock/ignore-matching")
async def products_oos_ignore_matching(request: Request, db: DbDep) -> Response:
    """Ignore every OOS product matching the current filter — search + collection."""
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import oos as oos_svc
    form = await request.form()
    collection_handle = (form.get("collection") or "").strip() or None
    q = (form.get("q") or "").strip() or None
    since = (form.get("since") or "").strip()
    oos_within_days = int(since) if since in ("7", "14", "30") else None
    oos_older_than_days = 30 if since == "old" else None
    ok, detail, _ = oos_svc.ignore_all_matching(
        db,
        collection_handle=collection_handle,
        q=q,
        oos_within_days=oos_within_days,
        oos_older_than_days=oos_older_than_days,
    )
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        request.headers.get("referer", "/products/out-of-stock"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/products/{product_id}/oos/ignore")
def product_oos_ignore(product_id: int, request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import oos as oos_svc
    ok, detail = oos_svc.ignore_product(db, product_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        request.headers.get("referer", "/products/out-of-stock"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/products/{product_id}/oos/unignore")
def product_oos_unignore(product_id: int, request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import oos as oos_svc
    ok, detail = oos_svc.unignore_product(db, product_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        request.headers.get("referer", "/products/out-of-stock"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/products/out-of-stock/bulk")
async def products_oos_bulk(request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import oos as oos_svc
    form = await request.form()
    action = (form.get("action") or "ignore").strip()
    raw_ids = form.getlist("product_id")
    ids: list[int] = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    if action == "unignore":
        ok, detail, _ = oos_svc.bulk_unignore(db, ids)
    else:
        ok, detail, _ = oos_svc.bulk_ignore(db, ids)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        request.headers.get("referer", "/products/out-of-stock"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/products/bulk-ignore")
async def products_bulk_ignore(request: Request, db: DbDep) -> Response:
    """Bulk Ignore: hide selected products from default views + skip in bulk ops."""
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import product_ignore as pi_svc
    form = await request.form()
    action = (form.get("action") or "ignore").strip()
    raw_ids = form.getlist("product_id")
    ids: list[int] = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    if action == "unignore":
        ok, detail, _ = pi_svc.unignore_products(db, ids)
    else:
        ok, detail, _ = pi_svc.ignore_products(db, ids)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        request.headers.get("referer", "/products"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/products/ignore-matching")
async def products_ignore_matching(request: Request, db: DbDep) -> Response:
    """Ignore every product matching the current filter (search + collection +
    status). Respects the include_drafts toggle."""
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import product_ignore as pi_svc
    form = await request.form()
    q = (form.get("q") or "").strip() or None
    status_filter = (form.get("status") or "").strip() or None
    collection_handle = (form.get("collection") or "").strip() or None
    include_drafts = form.get("include_drafts") in ("1", "true", "on")
    ok, detail, _ = pi_svc.ignore_all_matching(
        db,
        q=q,
        status_filter=status_filter,
        collection_handle=collection_handle,
        include_drafts=include_drafts,
    )
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        request.headers.get("referer", "/products"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/products/ignored", response_class=HTMLResponse)
def products_ignored(request: Request, db: DbDep) -> Response:
    """List of ignored products — un-ignore in bulk to bring them back."""
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    q = (request.query_params.get("q") or "").strip()
    stmt = select(ShopifyProduct).where(ShopifyProduct.is_ignored.is_(True))
    if q:
        stmt = stmt.where(ShopifyProduct.title.ilike(f"%{q}%"))
    stmt = stmt.order_by(ShopifyProduct.title)
    products = db.execute(stmt).scalars().unique().all()
    items = [
        {
            "id": p.id,
            "title": p.title,
            "status": p.status,
            "image_url": p.image_url,
            "units_sold_90d": p.units_sold_90d,
            "net_sales_90d": p.net_sales_90d,
            "total_inventory": p.total_inventory,
        }
        for p in products
    ]
    return templates.TemplateResponse(
        request,
        "products_ignored.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "items": items,
            "q": q,
            "total": len(items),
            "flashes": _consume_flashes(request),
        },
    )


@app.post("/products/keywords/research-all")
def products_keywords_research_all(request: Request, db: DbDep) -> Response:
    """One-click keyword research for every product that doesn't have any yet."""
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    ok, detail, _ = kw_research_svc.research_all_products(
        db, started_by_user_id=user.id, only_missing=True
    )
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        request.headers.get("referer", "/products"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


_SYNC_FN_BY_KIND = {
    "full": shopify_svc.sync_full,
    "catalog": shopify_svc.sync_catalog_only,
    "sales": shopify_svc.sync_sales_only,
    "inventory": shopify_svc.sync_inventory_only,
}


@app.post("/products/sync")
@app.post("/products/sync/{kind}")
def products_sync(
    request: Request, db: DbDep, kind: str = "full"
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    fn = _SYNC_FN_BY_KIND.get(kind)
    if fn is None:
        _flash(request, f"Unknown sync kind: {kind}.", "error")
        return RedirectResponse("/products", status_code=status.HTTP_303_SEE_OTHER)
    ok, detail, _stats = fn(db)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse("/products", status_code=status.HTTP_303_SEE_OTHER)


def _load_product_context(
    db: Session, product_id: int
) -> tuple[ShopifyProduct, dict]:
    p = db.get(ShopifyProduct, product_id)
    if p is None:
        raise HTTPException(status_code=404)

    variants = db.execute(
        select(ShopifyVariant)
        .where(ShopifyVariant.product_id == p.id)
        .order_by(ShopifyVariant.id)
    ).scalars().all()

    channel_names = _product_channel_names(db, [p.id]).get(p.id, [])
    in_days, out_days, total_days = _stock_history(db, [p.id]).get(p.id, (0, 0, 0))

    product = {
        "id": p.id,
        "title": p.title,
        "status": p.status,
        "image_url": p.image_url,
        "price_range": _format_price(p),
        "vendor": p.vendor or "—",
        "product_type": p.product_type or "—",
        "variant_count": p.variant_count,
        "total_inventory": p.total_inventory,
        "units_sold_90d": p.units_sold_90d,
        "unique_customers_90d": p.unique_customers_90d,
        "net_sales_90d": f"{p.net_sales_90d:.2f}" if p.net_sales_90d is not None else "0.00",
        "currency": p.currency or "USD",
        "last_sale_at": p.last_sale_at.strftime("%Y-%m-%d") if p.last_sale_at else "—",
        "created_at": p.created_at.strftime("%Y-%m-%d") if p.created_at else "—",
        "updated_at": p.updated_at.strftime("%Y-%m-%d") if p.updated_at else "—",
        "description_html": p.description_html or "",
        "shopify_admin_url": p.shopify_admin_url or "#",
        "variants": [
            {
                "sku": v.sku or "—",
                "title": v.title or "—",
                "price": f"{v.price:.2f}" if v.price is not None else "—",
                "inventory_quantity": v.inventory_quantity,
                "options": [o for o in (v.option1, v.option2, v.option3) if o],
            }
            for v in variants
        ],
        "channels": channel_names,
        "stock_in_days": in_days,
        "stock_out_days": out_days,
        "stock_total_days": total_days,
    }
    return p, product


def _score_band(score: int) -> str:
    if score >= 80:
        return "good"
    if score >= 65:
        return "ok"
    if score >= 45:
        return "warn"
    return "bad"


def _mock_seo(product: dict) -> dict:
    """Placeholder content — replaced by real Claude output once AI is wired up."""
    title = product["title"]
    return {
        "score": 72,
        "score_band": _score_band(72),
        "title": {
            "current": title,
            "suggestion": f"{title} — Free Shipping & Easy Returns",
        },
        "meta": {
            "current": "",
            "suggestion": (
                f"Shop {title} crafted for everyday wear. Free shipping on orders "
                f"over $50. 30-day returns. Made to last."
            )[:160],
        },
        "description": {
            "current": product.get("description_html") or "",
            "suggestion": (
                f"<p><strong>{title}</strong> — built for daily use.</p>"
                "<ul><li>Durable materials</li><li>Designed in-house</li>"
                "<li>Free shipping on $50+</li></ul>"
            ),
        },
        "bullets": [
            "Lightweight, durable build",
            "Designed for everyday wear",
            "Free shipping on orders $50+",
            "30-day no-questions returns",
            "Made with sustainable materials",
        ],
        "images": [
            {
                "url": product["image_url"],
                "current_alt": "",
                "suggested_alt": f"{title} shown from the front on a neutral background",
            }
        ] if product.get("image_url") else [],
    }


def _ads_context(db: Session, product_id: int) -> dict:
    """Load real keyword research + last run from DB. No mock data."""
    keywords = db.execute(
        select(ProductKeyword)
        .where(ProductKeyword.product_id == product_id)
        .order_by(ProductKeyword.relevance_score.desc().nullslast(), ProductKeyword.keyword)
    ).scalars().all()

    by_bucket = {"primary": [], "secondary": [], "negative": [], "unsorted": [], "ignore": []}
    for k in keywords:
        item = {
            "id": k.id,
            "keyword": k.keyword,
            "intent": k.intent,
            "funnel": k.funnel,
            "match_type": k.match_type,
            "relevance_score": k.relevance_score,
            "rationale": k.rationale,
            "source": k.source,
            "bucket": k.bucket,
            "avg_monthly_searches": k.avg_monthly_searches,
            "competition": k.competition,
            "bid_range": _format_bid_range(k.low_bid_micros, k.high_bid_micros),
            "sc_clicks": k.sc_clicks,
            "sc_impressions": k.sc_impressions,
            "sc_position": f"{k.sc_position:.1f}" if k.sc_position else None,
        }
        by_bucket.setdefault(k.bucket, by_bucket["unsorted"]).append(item)

    last_run = db.scalar(
        select(KeywordResearchRun)
        .where(KeywordResearchRun.product_id == product_id)
        .order_by(KeywordResearchRun.started_at.desc())
        .limit(1)
    )

    return {
        "candidates": by_bucket["unsorted"],
        "primary": by_bucket["primary"],
        "secondary": by_bucket["secondary"],
        "negative": by_bucket["negative"],
        "ignored": by_bucket["ignore"],
        "total": len(keywords),
        "last_run": last_run,
    }


def _format_bid_range(low_micros: int | None, high_micros: int | None) -> str | None:
    if not low_micros and not high_micros:
        return None
    lo = (low_micros or 0) / 1_000_000
    hi = (high_micros or 0) / 1_000_000
    if lo and hi:
        return f"${lo:.2f}–${hi:.2f}"
    return f"${(lo or hi):.2f}"


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_overview(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _p, product = _load_product_context(db, product_id)
    return templates.TemplateResponse(
        request,
        "product_overview.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "overview",
            "product": product,
            "flashes": _consume_flashes(request),
            **_chat_ctx(request, db, product_id),
        },
    )


def _latest_pending_drafts(
    db: Session, product_id: int, fields: list[str]
) -> dict[str, ProductSeoDraft | None]:
    result: dict[str, ProductSeoDraft | None] = {f: None for f in fields}
    rows = db.execute(
        select(ProductSeoDraft)
        .where(ProductSeoDraft.product_id == product_id)
        .where(ProductSeoDraft.status == "pending")
        .where(ProductSeoDraft.field.in_(fields))
        .order_by(ProductSeoDraft.generated_at.desc())
    ).scalars().all()
    for r in rows:
        if result.get(r.field) is None:
            result[r.field] = r
    return result


@app.get("/products/{product_id}/seo", response_class=HTMLResponse)
def product_seo(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    p, product = _load_product_context(db, product_id)
    drafts = _latest_pending_drafts(
        db, product_id, ["seo_title", "meta_description", "description", "bullets"]
    )

    bullets_list: list[str] = []
    if drafts.get("bullets"):
        try:
            import json as _json
            parsed = _json.loads(drafts["bullets"].suggested_value)
            if isinstance(parsed, list):
                bullets_list = [str(x) for x in parsed][:10]
        except (ValueError, TypeError):
            pass

    # Show whether keyword research has any approved keywords
    has_approved = db.scalar(
        select(func.count(ProductKeyword.id))
        .where(ProductKeyword.product_id == product_id)
        .where(ProductKeyword.bucket.in_(("primary", "secondary")))
    ) or 0

    approved_not_pushed = db.scalar(
        select(func.count(ProductSeoDraft.id))
        .where(ProductSeoDraft.product_id == product_id)
        .where(ProductSeoDraft.field.in_(("seo_title", "meta_description", "description")))
        .where(ProductSeoDraft.status == "approved")
        .where(ProductSeoDraft.pushed_to_shopify_at.is_(None))
    ) or 0

    return templates.TemplateResponse(
        request,
        "product_seo.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "seo",
            "product": product,
            "seo": {
                "current_title": p.seo_title,
                "current_meta": p.seo_meta_description,
                "drafts": drafts,
                "bullets_list": bullets_list,
            },
            "current_description_full": p.description_html or "",
            "no_keywords_warning": has_approved == 0,
            "approved_not_pushed": approved_not_pushed,
            "flashes": _consume_flashes(request),
            **_chat_ctx(request, db, product_id),
        },
    )


@app.post("/products/{product_id}/seo/generate")
def product_seo_generate(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = seo_svc.generate_seo_drafts(db, product_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        f"/products/{product_id}/seo", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/products/{product_id}/seo/drafts/{draft_id}/approve")
async def product_seo_draft_approve(
    product_id: int, draft_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    edited = form.get("edited_value")
    ok, detail, _ = seo_svc.approve_draft(db, draft_id, user.id, edited_value=edited)
    _flash(request, detail, "ok" if ok else "error")
    referer = request.headers.get("referer", f"/products/{product_id}/seo")
    return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/products/{product_id}/seo/drafts/{draft_id}/approve-and-push")
async def product_seo_draft_approve_and_push(
    product_id: int, draft_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    edited = form.get("edited_value")
    ok, detail = seo_svc.approve_and_push_image(
        db, product_id, draft_id, user.id, edited_value=edited
    )
    _flash(request, detail, "ok" if ok else "error")
    referer = request.headers.get("referer", f"/products/{product_id}/images")
    return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/products/{product_id}/images/approve-and-push-all")
def product_images_approve_and_push_all(
    product_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = seo_svc.approve_and_push_all_pending_image_alts(db, product_id, user.id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        f"/products/{product_id}/images", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/products/{product_id}/images/push-approved-all")
def product_images_push_approved_all(
    product_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = seo_svc.push_all_approved_image_alts(db, product_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        f"/products/{product_id}/images", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/products/{product_id}/seo/push")
def product_seo_push(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = seo_svc.push_approved_seo_to_shopify(db, product_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        f"/products/{product_id}/seo", status_code=status.HTTP_303_SEE_OTHER
    )


async def _handle_chat(
    request: Request, db: Session, product_id: int | None
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    topic = (form.get("topic") or "general").strip() or "general"
    message = (form.get("message") or "").strip()
    redirect_to = (form.get("redirect_to") or "").strip() or (
        f"/products/{product_id}" if product_id else "/"
    )
    ok, detail = seo_chat_svc.send_message(db, product_id, user.id, message, topic=topic)
    if not ok:
        _flash(request, detail, "error")
    return RedirectResponse(redirect_to, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/products/{product_id}/chat")
async def product_chat(product_id: int, request: Request, db: DbDep) -> Response:
    return await _handle_chat(request, db, product_id)


@app.post("/chat")
async def global_chat(request: Request, db: DbDep) -> Response:
    return await _handle_chat(request, db, None)


def _chat_ctx(request: Request, db: Session, product_id: int | None) -> dict:
    """Common chat context for any product subpage."""
    scope = request.query_params.get("chat_scope") or "product"
    if scope not in ("product", "all"):
        scope = "product"
    return {
        "chat_scope": scope,
        "chat_messages_product": seo_chat_svc.list_messages(
            db, product_id, topic="general"
        )
        if product_id is not None
        else [],
        "chat_messages_global": seo_chat_svc.list_messages(
            db, None, topic="general"
        ),
    }


@app.post("/products/{product_id}/seo/drafts/{draft_id}/reject")
def product_seo_draft_reject(
    product_id: int, draft_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = seo_svc.reject_draft(db, draft_id)
    _flash(request, detail, "ok" if ok else "error")
    referer = request.headers.get("referer", f"/products/{product_id}/seo")
    return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)


# --------- Images tab ---------

@app.get("/products/{product_id}/images", response_class=HTMLResponse)
def product_images(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    p, product = _load_product_context(db, product_id)
    images = db.execute(
        select(ShopifyProductImage)
        .where(ShopifyProductImage.product_id == product_id)
        .order_by(ShopifyProductImage.position)
    ).scalars().all()

    # All image_alt drafts for this product (most recent first), grouped by image
    alt_rows = db.execute(
        select(ProductSeoDraft)
        .where(ProductSeoDraft.product_id == product_id)
        .where(ProductSeoDraft.field == "image_alt")
        .order_by(ProductSeoDraft.generated_at.desc())
    ).scalars().all()
    pending_by_image: dict[int, ProductSeoDraft] = {}
    approved_by_image: dict[int, ProductSeoDraft] = {}
    pushed_by_image: dict[int, ProductSeoDraft] = {}
    for r in alt_rows:
        if not r.image_id:
            continue
        if r.status == "pending" and r.image_id not in pending_by_image:
            pending_by_image[r.image_id] = r
        elif r.status == "approved" and r.pushed_to_shopify_at is None and r.image_id not in approved_by_image:
            approved_by_image[r.image_id] = r
        elif r.pushed_to_shopify_at is not None and r.image_id not in pushed_by_image:
            pushed_by_image[r.image_id] = r

    image_views = [
        {
            "id": img.id,
            "url": img.url,
            "alt_text": img.alt_text,
            "position": img.position,
            "width": img.width,
            "height": img.height,
            "draft": pending_by_image.get(img.id),
            "approved_draft": approved_by_image.get(img.id),
            "pushed_draft": pushed_by_image.get(img.id),
        }
        for img in images
    ]

    pending_count = sum(1 for v in image_views if v["draft"])
    approved_unpushed = sum(1 for v in image_views if v["approved_draft"])

    return templates.TemplateResponse(
        request,
        "product_images.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "images",
            "product": product,
            "images": image_views,
            "pending_count": pending_count,
            "approved_unpushed_count": approved_unpushed,
            "flashes": _consume_flashes(request),
            **_chat_ctx(request, db, product_id),
        },
    )


@app.post("/products/{product_id}/images/generate-all")
def product_images_generate_all(
    product_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = seo_svc.generate_image_alt(db, product_id, image_id=None)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        f"/products/{product_id}/images", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/products/{product_id}/images/{image_id}/generate")
@app.post("/products/{product_id}/images/{image_id}/regenerate")
def product_image_generate(
    product_id: int, image_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = seo_svc.generate_image_alt(db, product_id, image_id=image_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        f"/products/{product_id}/images", status_code=status.HTTP_303_SEE_OTHER
    )


# --------- Keyword rank tab ---------

_RANK_SORT_KEYS = {
    "keyword", "source", "volume", "competition", "position", "clicks",
    "impressions", "ctr", "score", "bucket",
}

_RANK_COLUMNS = [
    ("source", "Source"),
    ("rationale", "Rationale"),
    ("volume", "Vol/mo"),
    ("competition", "Comp"),
    ("org_pos", "Org pos"),
    ("org_clicks", "Org clicks"),
    ("org_impr", "Org impr"),
    ("cov_title", "Title"),
    ("cov_meta_title", "Meta T"),
    ("cov_meta_description", "Meta D"),
    ("cov_description", "Desc"),
    ("cov_image_alts", "Alts"),
    ("ads", "Ads"),
    ("score", "Score"),
]
_RANK_DEFAULT_COLS = {
    "source", "rationale", "volume", "competition", "org_pos", "org_clicks",
    "org_impr", "cov_title", "cov_meta_title", "cov_meta_description",
    "cov_description", "cov_image_alts", "ads", "score",
}


_KW_SOURCE_LABELS = {
    "ai": "Claude",
    "keyword_planner": "Keyword Planner",
    "search_console": "Search Console",
    "manual": "Manual",
}


@app.get("/products/{product_id}/keyword-rank", response_class=HTMLResponse)
def product_keyword_rank(product_id: int, request: Request, db: DbDep) -> Response:
    """Master keywords view — every keyword from every source, with coverage and actions."""
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    p, product = _load_product_context(db, product_id)

    # Filter / sort / pagination params
    q = (request.query_params.get("q") or "").strip().lower()
    source_filter = request.query_params.get("source") or ""
    in_ads_filter = request.query_params.get("in_ads") or ""
    missing_filter = request.query_params.get("missing") or ""  # title|meta_title|...
    # Per-user defaults (Settings → Keywords) when URL doesn't override
    kp_defaults = prefs_svc.keyword_page_defaults(user)

    sort = listing_util.parse_sort(
        request.query_params.get("sort"), _RANK_SORT_KEYS, kp_defaults["sort"]
    )
    direction = listing_util.parse_direction(
        request.query_params.get("dir"), kp_defaults["dir"]
    )
    per_page = listing_util.parse_per_page(request.query_params.get("per_page"))
    page = listing_util.parse_page(request.query_params.get("page"))

    cols_raw = request.query_params.getlist("col")
    if cols_raw:
        cols = {c for c in cols_raw if c in dict(_RANK_COLUMNS)}
    else:
        user_cols = [c for c in kp_defaults["cols"] if c in dict(_RANK_COLUMNS)]
        cols = set(user_cols) if user_cols else set(_RANK_DEFAULT_COLS)

    # Pull every stored keyword (AI/KP/SC/manual)
    kw_rows = db.execute(
        select(ProductKeyword).where(ProductKeyword.product_id == product_id)
    ).scalars().all()

    coverage = kw_place_svc.coverage_for_product(db, product_id)

    items: list[dict] = []
    for kw in kw_rows:
        cov = coverage.get(kw.keyword.lower(), {})
        items.append({
            "id": kw.id,
            "keyword": kw.keyword,
            "source": kw.source or "ai",
            "source_label": _KW_SOURCE_LABELS.get(kw.source or "ai", kw.source or "ai"),
            "intent": kw.intent,
            "funnel": kw.funnel,
            "match_type": kw.match_type,
            "score": kw.relevance_score,
            "rationale": kw.rationale,
            "volume": kw.avg_monthly_searches,
            "competition": kw.competition,
            "bid_range": _format_bid_range(kw.low_bid_micros, kw.high_bid_micros),
            "organic_position": (round(kw.sc_position, 1) if kw.sc_position else None),
            "organic_clicks": kw.sc_clicks,
            "organic_impressions": kw.sc_impressions,
            "organic_ctr": (round((kw.sc_ctr or 0) * 100, 1) if kw.sc_ctr else None),
            "bucket": kw.bucket,
            "in_title": cov.get("title", False),
            "in_meta_title": cov.get("meta_title", False),
            "in_meta_description": cov.get("meta_description", False),
            "in_description": cov.get("description", False),
            "in_image_alts": cov.get("image_alts", False),
            "seo_targets": kw_place_svc.parse_seo_targets(kw.seo_targets),
        })

    # Apply filters
    if q:
        items = [r for r in items if q in r["keyword"].lower()]
    if source_filter:
        items = [r for r in items if r["source"] == source_filter]
    if in_ads_filter == "yes":
        items = [r for r in items if r["bucket"] in ("primary", "secondary")]
    elif in_ads_filter == "no":
        items = [r for r in items if r["bucket"] not in ("primary", "secondary")]
    if missing_filter and missing_filter in kw_place_svc.SEO_FIELDS:
        items = [r for r in items if not r[f"in_{missing_filter}"]]

    # Sort
    def _sort_key(r: dict):
        if sort == "keyword":
            return r["keyword"].lower()
        if sort == "source":
            return r["source"]
        if sort == "volume":
            return r["volume"] or 0
        if sort == "competition":
            order = {"low": 0, "medium": 1, "high": 2}
            return order.get(r["competition"], 3)
        if sort == "position":
            return r["organic_position"] or 9999
        if sort == "clicks":
            return r["organic_clicks"] or 0
        if sort == "impressions":
            return r["organic_impressions"] or 0
        if sort == "ctr":
            return r["organic_ctr"] or 0
        if sort == "bucket":
            order = {"primary": 0, "secondary": 1, "unsorted": 2, "negative": 3, "ignore": 4}
            return order.get(r["bucket"], 5)
        return r["score"] or 0
    items.sort(key=_sort_key, reverse=(direction == "desc"))

    page_items, page, total_pages, total = listing_util.paginate(items, page, per_page)

    # Distinct sources for filter dropdown
    distinct_sources = sorted({r["source"] for r in items}, key=lambda s: s or "")

    # Count per source from ALL rows (not just the current page) so the
    # header shows the real totals: "5 from Search Console, 12 from Claude…"
    source_counts: dict[str, int] = {}
    for r in items:
        source_counts[r["source"]] = source_counts.get(r["source"], 0) + 1

    # Last keyword-research run for this product, so we can surface errors
    last_research = db.scalar(
        select(KeywordResearchRun)
        .where(KeywordResearchRun.product_id == product_id)
        .order_by(KeywordResearchRun.started_at.desc())
        .limit(1)
    )

    # Campaigns this product already has — for the "push to existing campaign"
    # dropdown next to each keyword and on the bulk bar.
    product_campaigns = [
        {"id": c.id, "name": c.name, "status": c.status}
        for c in campaigns_svc.campaigns_for_product(db, product_id)
    ]

    return templates.TemplateResponse(
        request,
        "product_keyword_rank.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "rank",
            "product": product,
            "items": page_items,
            "distinct_sources": distinct_sources,
            "source_counts": source_counts,
            "last_research": last_research,
            "last_research_errors": (
                json.loads(last_research.source_errors)
                if last_research and last_research.source_errors
                else {}
            ),
            "available_columns": _RANK_COLUMNS,
            "cols": cols,
            "source_labels": _KW_SOURCE_LABELS,
            "seo_fields": kw_place_svc.SEO_FIELDS,
            "product_campaigns": product_campaigns,
            "filters": {
                "q": request.query_params.get("q") or "",
                "source": source_filter,
                "in_ads": in_ads_filter,
                "missing": missing_filter,
            },
            "sort": sort,
            "direction": direction,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total": total,
            "qs_no_page": listing_util.query_string_for(request, drop=["page"]),
            "qs_no_sort": listing_util.query_string_for(request, drop=["sort", "dir", "page"]),
            "flashes": _consume_flashes(request),
            **_chat_ctx(request, db, product_id),
        },
    )


@app.post("/products/{product_id}/keywords/{keyword_id}/place")
async def product_keyword_place(
    product_id: int, keyword_id: int, request: Request, db: DbDep
) -> Response:
    """Multi-place push: set seo_targets (checkboxes) + optional bucket."""
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    seo_fields = form.getlist("seo_field")
    bucket = (form.get("bucket") or "").strip()

    messages: list[str] = []
    errors: list[str] = []

    if seo_fields:
        ok, detail = kw_place_svc.push_to_seo(db, product_id, keyword_id, list(seo_fields))
        (messages if ok else errors).append(detail)
    if bucket:
        ok, detail = kw_place_svc.set_bucket(db, product_id, keyword_id, bucket)
        (messages if ok else errors).append(detail)

    if not messages and not errors:
        _flash(request, "Nothing chosen.", "info")
    elif errors:
        _flash(request, " | ".join(errors), "error")
    else:
        _flash(request, " | ".join(messages), "ok")
    referer = request.headers.get("referer", f"/products/{product_id}/keyword-rank")
    return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/products/{product_id}/keywords/bulk-place")
async def product_keywords_bulk_place(
    product_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    raw_ids = form.getlist("kw_id")
    keyword_ids: list[int] = []
    for v in raw_ids:
        try:
            keyword_ids.append(int(v))
        except (TypeError, ValueError):
            continue
    seo_fields = form.getlist("seo_field")
    bucket = (form.get("bucket") or "").strip()

    # If the user picked "AI decides", route to Claude-driven categorization
    # for ONLY the selected keywords. SEO fields still apply (uncommon, but
    # consistent with the bulk form).
    if bucket == "ai":
        if not keyword_ids:
            _flash(request, "No keywords selected.", "error")
        else:
            ok, detail = kw_research_svc.ai_categorize_keywords(
                db, product_id, keyword_ids
            )
            _flash(request, detail, "ok" if ok else "error")
            if seo_fields:
                ok2, detail2 = kw_place_svc.bulk_place(
                    db, product_id, keyword_ids, list(seo_fields), None
                )
                _flash(request, detail2, "ok" if ok2 else "error")
        referer = request.headers.get("referer", f"/products/{product_id}/keyword-rank")
        return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)

    ok, detail = kw_place_svc.bulk_place(
        db, product_id, keyword_ids, list(seo_fields), bucket or None
    )
    _flash(request, detail, "ok" if ok else "error")
    referer = request.headers.get("referer", f"/products/{product_id}/keyword-rank")
    return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/products/{product_id}/keywords/{keyword_id}/clear-seo")
def product_keyword_clear_seo(
    product_id: int, keyword_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = kw_place_svc.clear_seo_targets(db, product_id, keyword_id)
    _flash(request, detail, "ok" if ok else "error")
    referer = request.headers.get("referer", f"/products/{product_id}/keyword-rank")
    return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/products/{product_id}/keywords/{keyword_id}/ai-suggest")
def product_keyword_ai_suggest(
    product_id: int, keyword_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail, data = kw_place_svc.ai_suggest_placement(db, product_id, keyword_id)
    if ok and data:
        pieces = []
        if data["seo_fields"]:
            pieces.append("SEO: " + ", ".join(data["seo_fields"]))
        if data["ads_bucket"]:
            pieces.append(f"Ads bucket: {data['ads_bucket']}")
        if pieces:
            _flash(
                request,
                f"AI suggests — {' · '.join(pieces)}. {data['rationale']}",
                "info",
            )
        else:
            _flash(request, f"AI says: leave as-is. {data['rationale']}", "info")
    else:
        _flash(request, detail, "error")
    referer = request.headers.get("referer", f"/products/{product_id}/keyword-rank")
    return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/products/{product_id}/keyword-rank/refresh")
def product_keyword_rank_refresh(
    product_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(
        f"/products/{product_id}/keyword-rank", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/products/{product_id}/ads", response_class=HTMLResponse)
def product_ads(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _p, product = _load_product_context(db, product_id)
    ctx = _ads_context(db, product_id)
    return templates.TemplateResponse(
        request,
        "product_ads.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "ads",
            "product": product,
            "kw": ctx,
            "flashes": _consume_flashes(request),
            **_chat_ctx(request, db, product_id),
        },
    )


@app.post("/products/{product_id}/keywords/research")
def product_keywords_research(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = kw_research_svc.research_keywords(db, product_id, user.id)
    _flash(request, detail or ("Research completed." if ok else "Research failed."),
           "ok" if ok else "error")
    return RedirectResponse(
        f"/products/{product_id}/ads", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/products/{product_id}/keywords/apply-chat")
def product_keywords_apply_chat(
    product_id: int, request: Request, db: DbDep
) -> Response:
    """Cheap refresh: Claude re-evaluates existing keywords against chat rules.
    No Keyword Planner, no Search Console — just Claude."""
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    ok, detail = kw_research_svc.apply_chat_to_keywords(db, product_id, user.id)
    _flash(request, detail, "ok" if ok else "error")
    referer = request.headers.get("referer") or f"/products/{product_id}/keyword-rank"
    return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/products/{product_id}/keywords/{keyword_id}/bucket")
async def product_keyword_bucket(
    product_id: int, keyword_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    new_bucket = (form.get("bucket") or "").strip()
    if new_bucket not in ("primary", "secondary", "negative", "ignore", "unsorted"):
        raise HTTPException(status_code=400)
    kw = db.get(ProductKeyword, keyword_id)
    if kw is None or kw.product_id != product_id:
        raise HTTPException(status_code=404)
    kw.bucket = new_bucket
    kw.updated_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(
        f"/products/{product_id}/ads", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/products/{product_id}/analytics", response_class=HTMLResponse)
def product_analytics(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    p, product = _load_product_context(db, product_id)
    range_param = request.query_params.get("range") or "30"
    try:
        range_days = int(range_param) if range_param != "custom" else 30
    except ValueError:
        range_days = 30
    range_days = max(1, min(range_days, 365))

    # Real inventory chart from snapshots
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=range_days)
    snap_rows = db.execute(
        select(ShopifyInventorySnapshot.snapshot_date, ShopifyInventorySnapshot.inventory)
        .where(ShopifyInventorySnapshot.product_id == product_id)
        .where(ShopifyInventorySnapshot.snapshot_date >= cutoff)
        .order_by(ShopifyInventorySnapshot.snapshot_date)
    ).all()
    stock_labels = [d.strftime("%b %d") for d, _ in snap_rows]
    stock_values = [int(v) for _, v in snap_rows]

    return templates.TemplateResponse(
        request,
        "product_analytics.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "analytics",
            "product": product,
            "range": range_param,
            "range_days": range_days,
            "range_label": f"last {range_days} days",
            "stock_labels": stock_labels,
            "stock_values": stock_values,
            "has_ads_data": False,  # set true when we wire Google Ads sync
            "has_sc_data": _shopify_status(db),  # placeholder; replace per-integration
            "flashes": _consume_flashes(request),
            **_chat_ctx(request, db, product_id),
        },
    )


@app.get("/products/{product_id}/history", response_class=HTMLResponse)
def product_history(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _p, product = _load_product_context(db, product_id)
    return templates.TemplateResponse(
        request,
        "product_history.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "history",
            "product": product,
            "history": [],
            "flashes": _consume_flashes(request),
            **_chat_ctx(request, db, product_id),
        },
    )


# Placeholder POSTs for SEO / Ads actions — wire backend after design approval
def _placeholder_redirect(
    request: Request, db: Session, product_id: int, tab: str
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _flash(request, "Backend not built yet — design preview only.", "info")
    return RedirectResponse(
        f"/products/{product_id}/{tab}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/products/{product_id}/seo/generate")
@app.post("/products/{product_id}/seo/push")
def _seo_action(product_id: int, request: Request, db: DbDep) -> Response:
    return _placeholder_redirect(request, db, product_id, "seo")


@app.post("/products/{product_id}/seo/approve/{field}")
@app.post("/products/{product_id}/seo/reject/{field}")
def _seo_field_action(
    product_id: int, field: str, request: Request, db: DbDep
) -> Response:
    return _placeholder_redirect(request, db, product_id, "seo")


@app.post("/products/{product_id}/ads/generate")
@app.post("/products/{product_id}/ads/create-campaign")
def _ads_action(product_id: int, request: Request, db: DbDep) -> Response:
    return _placeholder_redirect(request, db, product_id, "ads")


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    db_ok, db_detail = db_ping()
    checks = [
        ("Web service", True, "FastAPI is responding"),
        ("Database", db_ok, db_detail if db_ok else "Connection failed"),
        (
            "Anthropic key",
            bool(settings.anthropic_api_key),
            "Set" if settings.anthropic_api_key else "Not configured yet",
        ),
        (
            "Shopify token",
            bool(settings.shopify_admin_api_token),
            "Set" if settings.shopify_admin_api_token else "Not configured yet",
        ),
        (
            "Google Ads",
            bool(settings.google_ads_developer_token),
            "Set" if settings.google_ads_developer_token else "Not configured yet",
        ),
        (
            "Google OAuth (login)",
            bool(settings.google_oauth_client_id),
            "Set" if settings.google_oauth_client_id else "Not configured yet",
        ),
    ]
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "version": __version__,
            "app_env": settings.app_env,
            "dry_run": settings.dry_run,
            "autonomous_mode": settings.autonomous_mode,
            "checks": checks,
            "user": user,
            "active": "status",
        },
    )


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

_CAMPAIGN_SORT_KEYS = {"name", "status", "scope", "budget", "updated_at"}


def _dashboard_summary(db: Session) -> dict:
    """Aggregate metrics for the campaigns dashboard. Anything that needs
    Google Ads performance sync stays None until we wire it up."""
    counts_by_status: dict[str, int] = {}
    for s in ("draft", "active", "paused", "archived"):
        counts_by_status[s] = db.scalar(
            select(func.count(AdCampaign.id)).where(AdCampaign.status == s)
        ) or 0
    total_campaigns = sum(counts_by_status.values())

    ai_managed_count = db.scalar(
        select(func.count(AdCampaign.id)).where(AdCampaign.ai_managed.is_(True))
    ) or 0

    counts_by_scope: dict[str, int] = {}
    for sc in ("product", "collection"):
        counts_by_scope[sc] = db.scalar(
            select(func.count(AdCampaign.id)).where(AdCampaign.scope_type == sc)
        ) or 0

    # Sum daily budget across active campaigns
    daily_budget_active = db.scalar(
        select(func.coalesce(func.sum(AdCampaign.daily_budget_cents), 0))
        .where(AdCampaign.status == "active")
    ) or 0
    daily_budget_all = db.scalar(
        select(func.coalesce(func.sum(AdCampaign.daily_budget_cents), 0))
    ) or 0

    # Target CPA range across campaigns that have one
    target_cpas = db.execute(
        select(AdCampaign.target_cpa_cents)
        .where(AdCampaign.target_cpa_cents.is_not(None))
    ).scalars().all()
    target_cpa_min = min(target_cpas) if target_cpas else None
    target_cpa_max = max(target_cpas) if target_cpas else None
    target_cpa_count = len(target_cpas)

    # Products with at least one campaign vs total products synced
    products_total = db.scalar(select(func.count(ShopifyProduct.id))) or 0
    products_with_campaign = db.scalar(
        select(func.count(func.distinct(AdCampaign.product_id)))
        .where(AdCampaign.product_id.is_not(None))
    ) or 0

    # Attention items
    active_zero_budget = db.execute(
        select(AdCampaign)
        .where(AdCampaign.status == "active")
        .where(AdCampaign.daily_budget_cents == 0)
        .limit(5)
    ).scalars().all()
    ai_no_target_cpa = db.execute(
        select(AdCampaign)
        .where(AdCampaign.ai_managed.is_(True))
        .where(AdCampaign.ai_target_cpa_cents.is_(None))
        .limit(5)
    ).scalars().all()

    # Active campaigns with no keywords (likely empty / not pushed yet)
    active_no_kw = []
    actives = db.execute(
        select(AdCampaign).where(AdCampaign.status == "active").limit(50)
    ).scalars().all()
    for c in actives:
        kc = db.scalar(
            select(func.count(AdCampaignKeyword.id))
            .where(AdCampaignKeyword.campaign_id == c.id)
            .where(AdCampaignKeyword.is_negative.is_(False))
        ) or 0
        if kc == 0:
            active_no_kw.append(c)
        if len(active_no_kw) >= 5:
            break

    return {
        "total_campaigns": total_campaigns,
        "status_counts": counts_by_status,
        "ai_managed_count": ai_managed_count,
        "scope_counts": counts_by_scope,
        "daily_budget_active_cents": int(daily_budget_active),
        "daily_budget_all_cents": int(daily_budget_all),
        "target_cpa_min_cents": int(target_cpa_min) if target_cpa_min else None,
        "target_cpa_max_cents": int(target_cpa_max) if target_cpa_max else None,
        "target_cpa_count": target_cpa_count,
        "products_total": products_total,
        "products_with_campaign": products_with_campaign,
        "products_coverage_pct": (
            round(100 * products_with_campaign / products_total) if products_total else 0
        ),
        "attention_active_zero_budget": [
            {"id": c.id, "name": c.name} for c in active_zero_budget
        ],
        "attention_ai_no_target_cpa": [
            {"id": c.id, "name": c.name} for c in ai_no_target_cpa
        ],
        "attention_active_no_keywords": [
            {"id": c.id, "name": c.name} for c in active_no_kw
        ],
    }


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_list(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    q = (request.query_params.get("q") or "").strip().lower()
    status_filter = request.query_params.get("status_filter") or ""
    scope_filter = request.query_params.get("scope") or ""

    sort = listing_util.parse_sort(
        request.query_params.get("sort"), _CAMPAIGN_SORT_KEYS, "updated_at"
    )
    direction = listing_util.parse_direction(request.query_params.get("dir"), "desc")
    per_page = listing_util.parse_per_page(request.query_params.get("per_page"))
    page = listing_util.parse_page(request.query_params.get("page"))

    base = select(AdCampaign)
    if q:
        base = base.where(AdCampaign.name.ilike(f"%{q}%"))
    if status_filter:
        base = base.where(AdCampaign.status == status_filter)
    if scope_filter in ("product", "collection"):
        base = base.where(AdCampaign.scope_type == scope_filter)

    sort_col = {
        "name": AdCampaign.name,
        "status": AdCampaign.status,
        "scope": AdCampaign.scope_type,
        "budget": AdCampaign.daily_budget_cents,
        "updated_at": AdCampaign.updated_at,
    }[sort]
    base = base.order_by(sort_col.desc() if direction == "desc" else sort_col.asc())

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    rows = db.execute(base.limit(per_page).offset(offset)).scalars().all()

    items = []
    for c in rows:
        items.append({
            "id": c.id,
            "name": c.name,
            "status": c.status,
            "scope": campaigns_svc.scope_label(db, c),
            "budget": f"${c.daily_budget_cents / 100:.2f}/day",
            "bid_strategy": c.bid_strategy.replace("_", " ").title(),
            "ai_managed": c.ai_managed,
            "updated_at": c.updated_at.strftime("%Y-%m-%d %H:%M"),
        })

    summary = _dashboard_summary(db)

    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {
            "version": __version__,
            "user": user,
            "active": "campaigns",
            "summary": summary,
            "items": items,
            "filters": {
                "q": request.query_params.get("q") or "",
                "status_filter": status_filter,
                "scope": scope_filter,
            },
            "sort": sort,
            "direction": direction,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total": total,
            "qs_no_page": listing_util.query_string_for(request, drop=["page"]),
            "qs_no_sort": listing_util.query_string_for(request, drop=["sort", "dir", "page"]),
            "flashes": _consume_flashes(request),
        },
    )


@app.post("/campaigns/new")
async def campaigns_new(request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    form = await request.form()
    scope_type = (form.get("scope_type") or "").strip()
    try:
        scope_id = int(form.get("scope_id") or 0)
    except (TypeError, ValueError):
        scope_id = 0
    if not scope_type or not scope_id:
        _flash(request, "Need scope type and id.", "error")
        return RedirectResponse("/campaigns", status_code=status.HTTP_303_SEE_OTHER)
    match_types = form.getlist("match_type") or list(campaigns_svc.MATCH_TYPES)
    name = (form.get("name") or "").strip() or None
    ok, detail, campaign_id = campaigns_svc.create_draft(
        db, scope_type, scope_id, user.id, name=name, match_types=match_types
    )
    if not ok:
        _flash(request, detail, "error")
        # Bounce back to whatever invoked us, fall back to /campaigns
        return RedirectResponse(
            request.headers.get("referer") or "/campaigns",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    _flash(request, detail, "ok")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


# Product subpage: list this product's campaigns + a create wizard.
@app.get("/products/{product_id}/campaigns", response_class=HTMLResponse)
def product_campaigns(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _p, product = _load_product_context(db, product_id)
    rows = campaigns_svc.campaigns_for_product(db, product_id)
    items = [
        {
            "id": c.id,
            "name": c.name,
            "status": c.status,
            "budget": f"${c.daily_budget_cents / 100:.2f}/day",
            "bid_strategy": c.bid_strategy.replace("_", " ").title(),
            "ai_managed": c.ai_managed,
            "updated_at": c.updated_at.strftime("%Y-%m-%d %H:%M"),
            "ad_group_count": len(campaigns_svc.ad_groups_for_campaign(db, c.id)),
        }
        for c in rows
    ]
    # Count of pushed-to-ads keywords so we can flag if it's empty
    pushed_count = db.scalar(
        select(func.count(ProductKeyword.id))
        .where(ProductKeyword.product_id == product_id)
        .where(ProductKeyword.bucket.in_(("primary", "secondary")))
    ) or 0
    return templates.TemplateResponse(
        request,
        "product_campaigns.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "campaigns",
            "product": product,
            "items": items,
            "pushed_count": pushed_count,
            "match_types": campaigns_svc.MATCH_TYPES,
            "match_type_labels": campaigns_svc.MATCH_TYPE_LABELS,
            "default_name": f"{product['title']} — Search",
            "flashes": _consume_flashes(request),
            **_chat_ctx(request, db, product_id),
        },
    )


def _campaign_back_url(db: Session, campaign_id: int) -> str:
    """Detail URL preserving product-page context when applicable."""
    c = db.get(AdCampaign, campaign_id)
    if c and c.scope_type == "product" and c.product_id:
        return f"/products/{c.product_id}/campaigns/{campaign_id}"
    return f"/campaigns/{campaign_id}"


def _campaign_detail_context(
    db: Session, campaign_id: int
) -> tuple[AdCampaign, dict] | None:
    c = db.get(AdCampaign, campaign_id)
    if c is None:
        return None
    ad_groups = campaigns_svc.ad_groups_for_campaign(db, campaign_id)
    groups_view = []
    for ag in ad_groups:
        pos, neg = campaigns_svc.keywords_for_ad_group(db, ag.id)
        groups_view.append({
            "ag": ag,
            "positive_kw": pos,
            "negative_kw": neg,
            "headlines": campaigns_svc.parse_list(ag.headlines_json),
            "descriptions": campaigns_svc.parse_list(ag.descriptions_json),
            "stale": campaigns_svc.is_copy_stale(ag),
            "has_pending": campaigns_svc.has_pending_copy(ag),
            "pending_headlines": campaigns_svc.parse_list(
                ag.ad_copy_pending_headlines_json
            ),
            "pending_descriptions": campaigns_svc.parse_list(
                ag.ad_copy_pending_descriptions_json
            ),
        })
    return c, {
        "campaign": c,
        "scope": campaigns_svc.scope_label(db, c),
        "ad_groups": groups_view,
        "ai_actions_selected": campaigns_svc.parse_actions(c),
        "ai_actions": campaigns_svc.AI_ACTIONS,
        "bid_strategies": campaigns_svc.BID_STRATEGIES,
        "match_type_labels": campaigns_svc.MATCH_TYPE_LABELS,
        "prev_ad_pause_hours": campaigns_svc.PREV_AD_PAUSE_DELAY_HOURS,
    }


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(campaign_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    result = _campaign_detail_context(db, campaign_id)
    if result is None:
        raise HTTPException(status_code=404)
    c, ctx = result

    # If this campaign is product-scoped, redirect to the product subpage
    # so the user stays in product context.
    if c.scope_type == "product" and c.product_id:
        return RedirectResponse(
            f"/products/{c.product_id}/campaigns/{campaign_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return templates.TemplateResponse(
        request,
        "campaign_detail.html",
        {
            "version": __version__,
            "user": user,
            "active": "campaigns",
            "back_url": "/campaigns",
            "back_label": "Campaigns",
            **ctx,
            "flashes": _consume_flashes(request),
        },
    )


@app.get("/products/{product_id}/campaigns/{campaign_id}", response_class=HTMLResponse)
def product_campaign_detail(
    product_id: int, campaign_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _p, product = _load_product_context(db, product_id)
    result = _campaign_detail_context(db, campaign_id)
    if result is None:
        raise HTTPException(status_code=404)
    c, ctx = result
    # Guard: campaign must belong to this product
    if c.scope_type != "product" or c.product_id != product_id:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "product_campaign_detail.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "campaigns",
            "product": product,
            "back_url": f"/products/{product_id}/campaigns",
            "back_label": "Campaigns for this product",
            **ctx,
            "flashes": _consume_flashes(request),
            **_chat_ctx(request, db, product_id),
        },
    )


@app.post("/campaigns/{campaign_id}/basics")
async def campaign_save_basics(
    campaign_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    form = await request.form()
    try:
        daily_dollars = float(form.get("daily_budget") or 0)
        cpa_dollars = form.get("target_cpa")
        cpa_cents = int(float(cpa_dollars) * 100) if cpa_dollars else None
    except (TypeError, ValueError):
        daily_dollars = 0.0
        cpa_cents = None
    ok, detail = campaigns_svc.update_basics(
        db,
        campaign_id,
        name=form.get("name"),
        status=form.get("status"),
        daily_budget_cents=int(daily_dollars * 100),
        bid_strategy=form.get("bid_strategy"),
        target_cpa_cents=cpa_cents,
        landing_page_url=form.get("landing_page_url"),
    )
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/campaigns/{campaign_id}/ai-settings")
async def campaign_save_ai_settings(
    campaign_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    form = await request.form()
    def _to_cents(field: str) -> int | None:
        v = (form.get(field) or "").strip()
        try:
            return int(float(v) * 100) if v else None
        except (TypeError, ValueError):
            return None
    ok, detail = campaigns_svc.update_ai_settings(
        db,
        campaign_id,
        ai_managed=form.get("ai_managed") == "on",
        ai_target_cpa_cents=_to_cents("ai_target_cpa"),
        ai_max_daily_budget_cents=_to_cents("ai_max_daily_budget"),
        ai_min_daily_budget_cents=_to_cents("ai_min_daily_budget"),
        ai_min_data_clicks=int(form.get("ai_min_data_clicks") or 20),
        actions_allowed=form.getlist("action"),
    )
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/campaigns/{campaign_id}/ad-groups/{ad_group_id}/keywords/add")
async def campaign_kw_add(
    campaign_id: int, ad_group_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    form = await request.form()
    ok, detail = campaigns_svc.add_keyword(
        db,
        campaign_id,
        ad_group_id,
        text=form.get("text") or "",
        is_negative=form.get("is_negative") == "on",
    )
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/campaigns/{campaign_id}/keywords/{keyword_id}/remove")
def campaign_kw_remove(
    campaign_id: int, keyword_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    ok, detail = campaigns_svc.remove_keyword(db, campaign_id, keyword_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/campaigns/{campaign_id}/ad-groups/{ad_group_id}/ad-copy")
async def campaign_ad_copy(
    campaign_id: int, ad_group_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    form = await request.form()
    headlines = [v for v in form.getlist("headline") if v]
    descriptions = [v for v in form.getlist("description") if v]
    ok, detail = campaigns_svc.update_ad_copy(
        db,
        campaign_id,
        ad_group_id,
        headlines,
        descriptions,
        path1=form.get("path1") or "",
        path2=form.get("path2") or "",
    )
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/campaigns/{campaign_id}/ad-groups/{ad_group_id}/generate-copy")
def campaign_ad_copy_generate(
    campaign_id: int, ad_group_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    ok, detail, _data = ad_copy_svc.generate_for_ad_group(
        db, campaign_id, ad_group_id
    )
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/campaigns/{campaign_id}/ad-groups/{ad_group_id}/delete")
def campaign_ad_group_delete(
    campaign_id: int, ad_group_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    ok, detail = campaigns_svc.delete_ad_group(db, campaign_id, ad_group_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/campaigns/{campaign_id}/delete")
def campaign_delete(campaign_id: int, request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    ok, detail = campaigns_svc.delete_campaign(db, campaign_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse("/campaigns", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/campaigns/{campaign_id}/ad-groups/{ad_group_id}/approve-copy")
def campaign_approve_pending_copy(
    campaign_id: int, ad_group_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    ok, detail = campaigns_svc.approve_pending_copy(db, campaign_id, ad_group_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/campaigns/{campaign_id}/ad-groups/{ad_group_id}/reject-copy")
def campaign_reject_pending_copy(
    campaign_id: int, ad_group_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    ok, detail = campaigns_svc.reject_pending_copy(db, campaign_id, ad_group_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/campaigns/{campaign_id}/push-to-google-ads")
def campaign_push_to_google_ads(
    campaign_id: int, request: Request, db: DbDep
) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    from gglads.services import google_ads_push as gads_push_svc
    ok, detail = gads_push_svc.push_campaign(db, campaign_id)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        _campaign_back_url(db, campaign_id), status_code=status.HTTP_303_SEE_OTHER
    )


# Push a single keyword (from the Keywords page) into an existing campaign.
# Fans out to every ad group, using each group's match type.
@app.post("/products/{product_id}/keywords/{keyword_id}/push-to-campaign")
async def product_keyword_push_to_campaign(
    product_id: int, keyword_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    try:
        campaign_id = int(form.get("campaign_id") or 0)
    except (TypeError, ValueError):
        campaign_id = 0
    is_negative = form.get("is_negative") in ("1", "on", "true")
    if not campaign_id:
        _flash(request, "Pick a campaign first.", "error")
        return RedirectResponse(
            request.headers.get("referer", f"/products/{product_id}/keyword-rank"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    kw = db.get(ProductKeyword, keyword_id)
    if kw is None or kw.product_id != product_id:
        _flash(request, "Keyword not found.", "error")
        return RedirectResponse(
            request.headers.get("referer", f"/products/{product_id}/keyword-rank"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    # Guard: campaign must belong to this product.
    camp = db.get(AdCampaign, campaign_id)
    if camp is None or camp.product_id != product_id:
        _flash(request, "Campaign does not belong to this product.", "error")
        return RedirectResponse(
            request.headers.get("referer", f"/products/{product_id}/keyword-rank"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    ok, detail, _ = campaigns_svc.push_keyword_to_campaign(
        db, campaign_id, kw.keyword, is_negative=is_negative
    )
    # Also mark the master keyword's bucket so the UI shows it's "in ads".
    if ok:
        target_bucket = "negative" if is_negative else "primary"
        if kw.bucket not in ("primary", "secondary", "negative"):
            kw_place_svc.set_bucket(db, product_id, keyword_id, target_bucket)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        request.headers.get("referer", f"/products/{product_id}/keyword-rank"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/products/{product_id}/keywords/bulk-push-to-campaign")
async def product_keywords_bulk_push_to_campaign(
    product_id: int, request: Request, db: DbDep
) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if user.role not in ("admin", "operator"):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    try:
        campaign_id = int(form.get("campaign_id") or 0)
    except (TypeError, ValueError):
        campaign_id = 0
    is_negative = form.get("is_negative") in ("1", "on", "true")
    raw_ids = form.getlist("kw_id")
    keyword_ids: list[int] = []
    for v in raw_ids:
        try:
            keyword_ids.append(int(v))
        except (TypeError, ValueError):
            continue
    if not campaign_id or not keyword_ids:
        _flash(request, "Pick a campaign and at least one keyword.", "error")
        return RedirectResponse(
            request.headers.get("referer", f"/products/{product_id}/keyword-rank"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    camp = db.get(AdCampaign, campaign_id)
    if camp is None or camp.product_id != product_id:
        _flash(request, "Campaign does not belong to this product.", "error")
        return RedirectResponse(
            request.headers.get("referer", f"/products/{product_id}/keyword-rank"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    texts: list[str] = []
    for kid in keyword_ids:
        kw = db.get(ProductKeyword, kid)
        if kw is not None and kw.product_id == product_id:
            texts.append(kw.keyword)
    ok, detail, _ = campaigns_svc.push_keywords_to_campaign_bulk(
        db, campaign_id, texts, is_negative=is_negative
    )
    if ok:
        target_bucket = "negative" if is_negative else "primary"
        for kid in keyword_ids:
            kw = db.get(ProductKeyword, kid)
            if kw is None or kw.product_id != product_id:
                continue
            if kw.bucket not in ("primary", "secondary", "negative"):
                kw_place_svc.set_bucket(db, product_id, kid, target_bucket)
    _flash(request, detail, "ok" if ok else "error")
    return RedirectResponse(
        request.headers.get("referer", f"/products/{product_id}/keyword-rank"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Settings (per-user preferences)
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    kp = prefs_svc.keyword_page_defaults(user)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "version": __version__,
            "user": user,
            "active": "settings",
            "available_columns": _RANK_COLUMNS,
            "kp_cols": kp["cols"],
            "kp_sort": kp["sort"],
            "kp_dir": kp["dir"],
            "sort_options": [
                ("keyword", "Keyword (A-Z)"),
                ("source", "Source"),
                ("volume", "Search volume"),
                ("competition", "Competition"),
                ("position", "Organic position"),
                ("clicks", "Organic clicks"),
                ("impressions", "Organic impressions"),
                ("ctr", "Organic CTR"),
                ("score", "Relevance score"),
                ("bucket", "Ads bucket"),
            ],
            "flashes": _consume_flashes(request),
        },
    )


@app.post("/settings/keywords")
async def settings_keywords_save(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    form = await request.form()
    cols = [c for c in form.getlist("col") if c in dict(_RANK_COLUMNS)]
    sort = form.get("sort") or "score"
    direction = form.get("dir") or "desc"
    prefs_svc.set_keyword_page_prefs(
        db, user, cols=cols or None, sort=sort, direction=direction
    )
    _flash(request, "Saved Keyword-page defaults.", "ok")
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> PlainTextResponse:
    tb = traceback.format_exc()
    logger.error("Unhandled exception on %s: %s", request.url.path, tb)
    if settings.app_env == "production":
        return PlainTextResponse("Internal Server Error", status_code=500)
    return PlainTextResponse(tb, status_code=500)
