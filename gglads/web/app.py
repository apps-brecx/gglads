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
from gglads.models.shopify_product import (
    ShopifyCollection,
    ShopifyInventorySnapshot,
    ShopifyProduct,
    ShopifyProductCollection,
    ShopifyProductPublication,
    ShopifyPublication,
    ShopifyVariant,
)
from gglads.models.user import User
from gglads.services import integration_tests, integrations as integrations_svc
from gglads.services import shopify as shopify_svc

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

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"version": __version__, "user": user, "active": "dashboard"},
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
}


@app.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    integrations_state = {
        name: integrations_svc.summarize_for_form(db, name)
        for name in ("anthropic", "shopify", "google_ads")
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


@app.get("/products", response_class=HTMLResponse)
def products_collections(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    q = (request.query_params.get("q") or "").strip().lower()
    collections = _collections_summary(db)
    if q:
        collections = [c for c in collections if q in c["title"].lower()]

    total_products = db.scalar(select(func.count(ShopifyProduct.id))) or 0
    active_products = db.scalar(
        select(func.count(ShopifyProduct.id)).where(ShopifyProduct.status == "active")
    ) or 0

    return templates.TemplateResponse(
        request,
        "products.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "collections": collections,
            "total_count": total_products,
            "active_count": active_products,
            "last_synced": _last_sync_display(db),
            "query": q,
            "shopify_connected": _shopify_status(db),
            "flashes": _consume_flashes(request),
        },
    )


def _render_products_list(
    request: Request,
    db: Session,
    user: User,
    products: list[ShopifyProduct],
    *,
    collection: ShopifyCollection | None,
) -> Response:
    view, cols = _parse_view_params(request)
    pids = [p.id for p in products]
    titles_by_pid = _product_collection_titles(db, pids)
    channels_by_pid = _product_channel_names(db, pids)
    stock_by_pid = _stock_history(db, pids)
    items = [
        _product_to_dict(
            p,
            titles_by_pid.get(p.id, []),
            channels_by_pid.get(p.id, []),
            stock_by_pid.get(p.id, (0, 0, 0)),
        )
        for p in products
    ]
    total_count = db.scalar(select(func.count(ShopifyProduct.id))) or 0

    return templates.TemplateResponse(
        request,
        "products_list.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "heading": collection.title if collection else "All products",
            "collection": {"title": collection.title, "handle": collection.handle}
            if collection
            else None,
            "items": items,
            "total_count": total_count,
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
):
    if q:
        base_query = base_query.where(ShopifyProduct.title.ilike(f"%{q}%"))
    if status_filter:
        base_query = base_query.where(ShopifyProduct.status == status_filter)
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


@app.get("/products/all", response_class=HTMLResponse)
def products_all(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    q = (request.query_params.get("q") or "").strip()
    status_filter = request.query_params.get("status") or ""
    collection_filter = request.query_params.get("collection") or ""
    channel_filter = request.query_params.get("channel") or ""

    query = select(ShopifyProduct).order_by(ShopifyProduct.units_sold_90d.desc())
    query = _apply_filters(
        db, query, q, status_filter, collection_filter, channel_filter
    )
    products = db.execute(query.limit(500)).scalars().unique().all()

    return _render_products_list(request, db, user, products, collection=None)


@app.get("/products/collection/{handle}", response_class=HTMLResponse)
def products_collection(handle: str, request: Request, db: DbDep) -> Response:
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

    query = (
        select(ShopifyProduct)
        .join(
            ShopifyProductCollection,
            ShopifyProductCollection.product_id == ShopifyProduct.id,
        )
        .where(ShopifyProductCollection.collection_id == collection.id)
        .order_by(ShopifyProduct.units_sold_90d.desc())
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
    products = db.execute(query.limit(500)).scalars().unique().all()

    return _render_products_list(request, db, user, products, collection=collection)


@app.post("/products/sync")
def products_sync(request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    ok, detail, _stats = shopify_svc.sync_catalog(db)
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


def _mock_ads(product: dict) -> dict:
    title = product["title"]
    return {
        "primary_keywords": [title.lower(), f"buy {title.lower()}", f"{title.lower()} online"],
        "secondary_keywords": [
            "free shipping",
            "best price",
            "premium quality",
            "shop now",
            "limited stock",
        ],
        "negative_keywords": ["free", "cheap", "knockoff", "diy", "tutorial"],
        "headlines": [
            f"{title[:25]}",
            "Free Shipping on $50+",
            "Designed To Last",
            "Shop New Arrivals",
            "Limited-Time Offer",
            "Loved By 10K+ Buyers",
            "Hand-Built Quality",
            "30-Day Easy Returns",
        ],
        "descriptions": [
            f"Premium {title.lower()} crafted for everyday use. Free shipping on $50+.",
            f"Order today and ship free. Easy 30-day returns. Shop the {title.lower()}.",
            f"Built to last with sustainable materials. Trusted by 10,000+ customers.",
        ],
        "usps": [
            "Sustainable materials, ethically sourced",
            "Designed in-house, hand-finished",
            "Free shipping on orders over $50",
        ],
        "pain_points": [
            "Cheap products that break after a few weeks",
            "Long shipping times from overseas",
        ],
    }


def _mock_analytics(product: dict, range_days: int) -> dict:
    """Mocked chart data for the design preview. Real data when orders are stored."""
    today = datetime.now(timezone.utc).date()
    labels = [(today - timedelta(days=i)).strftime("%b %d") for i in range(range_days - 1, -1, -1)]
    base = max(1, product["units_sold_90d"] // 30)
    chart_units = [max(0, base + ((i * 7) % 5) - 2) for i in range(range_days)]
    stock_now = product["total_inventory"]
    chart_stock = [stock_now + (range_days - i) * 2 for i in range(range_days)]
    chart_spend = [base * 4 + (i % 4) * 3 for i in range(range_days)]
    chart_conv = [max(0, base // 2 + ((i * 3) % 4) - 1) for i in range(range_days)]

    return {
        "range_label": f"last {range_days} days",
        "units_sold": sum(chart_units),
        "revenue": f"{sum(chart_units) * 32:.2f}",
        "conversions": sum(chart_conv),
        "conv_rate": "2.1",
        "roas": "3.4x",
        "chart_labels": labels,
        "chart_units": chart_units,
        "chart_stock": chart_stock,
        "chart_spend": chart_spend,
        "chart_conv": chart_conv,
        "chart_kw_labels": [
            "linen bag", "crossbody bag", "summer tote", "lightweight bag", "linen tote",
        ],
        "chart_kw_clicks": [42, 31, 28, 19, 14],
    }


def _mock_history(product: dict) -> list[dict]:
    return []


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
        },
    )


@app.get("/products/{product_id}/seo", response_class=HTMLResponse)
def product_seo(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _p, product = _load_product_context(db, product_id)
    return templates.TemplateResponse(
        request,
        "product_seo.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "seo",
            "product": product,
            "seo": _mock_seo(product),
            "flashes": _consume_flashes(request),
        },
    )


@app.get("/products/{product_id}/ads", response_class=HTMLResponse)
def product_ads(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _p, product = _load_product_context(db, product_id)
    return templates.TemplateResponse(
        request,
        "product_ads.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "tab": "ads",
            "product": product,
            "ads": _mock_ads(product),
            "flashes": _consume_flashes(request),
        },
    )


@app.get("/products/{product_id}/analytics", response_class=HTMLResponse)
def product_analytics(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _p, product = _load_product_context(db, product_id)
    range_param = request.query_params.get("range") or "30"
    try:
        range_days = int(range_param) if range_param != "custom" else 30
    except ValueError:
        range_days = 30
    range_days = max(1, min(range_days, 365))
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
            "range_from": "",
            "range_to": "",
            "analytics": _mock_analytics(product, range_days),
            "flashes": _consume_flashes(request),
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
            "history": _mock_history(product),
            "flashes": _consume_flashes(request),
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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> PlainTextResponse:
    tb = traceback.format_exc()
    logger.error("Unhandled exception on %s: %s", request.url.path, tb)
    if settings.app_env == "production":
        return PlainTextResponse("Internal Server Error", status_code=500)
    return PlainTextResponse(tb, status_code=500)
