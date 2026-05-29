import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import EmailStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from gglads import __version__
from gglads.auth.password import hash_password, verify_password
from gglads.config import get_settings
from gglads.db.session import get_db
from gglads.db.session import ping as db_ping
from gglads.models.user import User
from gglads.services import integration_tests, integrations as integrations_svc

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
# Products (Shopify mirror) — design preview, hard-coded sample data
# ---------------------------------------------------------------------------

_SAMPLE_COLLECTIONS = [
    {"handle": "bestsellers", "title": "Bestsellers", "product_count": 4,
     "image_url": None, "description": "Most popular this month"},
    {"handle": "new-arrivals", "title": "New arrivals", "product_count": 3,
     "image_url": None, "description": "Added in the last 30 days"},
    {"handle": "bags", "title": "Bags", "product_count": 2,
     "image_url": None, "description": ""},
    {"handle": "accessories", "title": "Accessories", "product_count": 3,
     "image_url": None, "description": ""},
    {"handle": "home", "title": "Home & Kitchen", "product_count": 2,
     "image_url": None, "description": ""},
    {"handle": "apparel", "title": "Apparel", "product_count": 2,
     "image_url": None, "description": "Soft basics and seasonal cuts"},
    {"handle": "basics", "title": "Basics", "product_count": 1,
     "image_url": None, "description": ""},
    {"handle": "gifts-under-50", "title": "Gifts under $50", "product_count": 2,
     "image_url": None, "description": ""},
]


_SAMPLE_PRODUCTS = [
    {
        "id": 1, "title": "Linen Crossbody Bag", "status": "active", "image_url": None,
        "price_range": "$48.00", "sku": "BAG-LIN-001", "inventory": 42,
        "vendor": "Atelier Brecx", "product_type": "Bag", "variant_count": 3,
        "collection_handles": ["bags", "new-arrivals"],
    },
    {
        "id": 2, "title": "Merino Wool Beanie", "status": "active", "image_url": None,
        "price_range": "$28.00 – $34.00", "sku": "ACC-MER-002", "inventory": 88,
        "vendor": "Atelier Brecx", "product_type": "Accessory", "variant_count": 4,
        "collection_handles": ["accessories"],
    },
    {
        "id": 3, "title": "Hand-poured Soy Candle (12 oz)", "status": "active", "image_url": None,
        "price_range": "$32.00", "sku": "HME-CDL-003", "inventory": 156,
        "vendor": "North Studio", "product_type": "Home", "variant_count": 6,
        "collection_handles": ["home", "gifts-under-50", "bestsellers"],
    },
    {
        "id": 4, "title": "Recycled Cotton T-Shirt", "status": "active", "image_url": None,
        "price_range": "$24.00", "sku": "APP-TEE-004", "inventory": 320,
        "vendor": "Atelier Brecx", "product_type": "Apparel", "variant_count": 8,
        "collection_handles": ["apparel", "basics", "bestsellers"],
    },
    {
        "id": 5, "title": "Ceramic Pour-Over Set", "status": "draft", "image_url": None,
        "price_range": "$78.00", "sku": "HME-POV-005", "inventory": 12,
        "vendor": "North Studio", "product_type": "Home", "variant_count": 1,
        "collection_handles": ["home", "new-arrivals"],
    },
    {
        "id": 6, "title": "Leather Card Holder", "status": "archived", "image_url": None,
        "price_range": "$36.00", "sku": "ACC-LCH-006", "inventory": 0,
        "vendor": "Atelier Brecx", "product_type": "Accessory", "variant_count": 2,
        "collection_handles": ["accessories"],
    },
    {
        "id": 7, "title": "Canvas Tote Bag", "status": "active", "image_url": None,
        "price_range": "$22.00", "sku": "BAG-CNV-007", "inventory": 64,
        "vendor": "Atelier Brecx", "product_type": "Bag", "variant_count": 2,
        "collection_handles": ["bags", "bestsellers", "gifts-under-50"],
    },
    {
        "id": 8, "title": "Silk Hair Scrunchie", "status": "active", "image_url": None,
        "price_range": "$14.00", "sku": "ACC-SHS-008", "inventory": 210,
        "vendor": "Atelier Brecx", "product_type": "Accessory", "variant_count": 5,
        "collection_handles": ["accessories", "new-arrivals", "bestsellers"],
    },
    {
        "id": 9, "title": "Heavyweight Hoodie", "status": "active", "image_url": None,
        "price_range": "$78.00", "sku": "APP-HOD-009", "inventory": 47,
        "vendor": "Atelier Brecx", "product_type": "Apparel", "variant_count": 6,
        "collection_handles": ["apparel"],
    },
]


PRODUCT_COLUMNS = [
    ("image", "Image"),
    ("price", "Price"),
    ("sku", "SKU"),
    ("inventory", "Inventory"),
    ("vendor", "Vendor"),
    ("type", "Product type"),
    ("variants", "Variants"),
    ("collections", "Collections"),
    ("status", "Status"),
]

DEFAULT_COLUMNS = {"image", "price", "collections", "status"}


def _collection_title(handle: str) -> str:
    for c in _SAMPLE_COLLECTIONS:
        if c["handle"] == handle:
            return c["title"]
    return handle


def _enrich(p: dict) -> dict:
    return {**p, "collection_titles": [_collection_title(h) for h in p["collection_handles"]]}


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


@app.get("/products", response_class=HTMLResponse)
def products_collections(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    q = (request.query_params.get("q") or "").strip().lower()
    collections = [c for c in _SAMPLE_COLLECTIONS if not q or q in c["title"].lower()]

    return templates.TemplateResponse(
        request,
        "products.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "collections": collections,
            "total_count": len(_SAMPLE_PRODUCTS),
            "active_count": sum(1 for p in _SAMPLE_PRODUCTS if p["status"] == "active"),
            "last_synced": None,
            "query": q,
            "shopify_connected": _shopify_status(db),
            "flashes": _consume_flashes(request),
        },
    )


def _render_products_list(
    request: Request,
    user: User,
    items: list[dict],
    *,
    collection: dict | None,
) -> Response:
    view, cols = _parse_view_params(request)
    return templates.TemplateResponse(
        request,
        "products_list.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "heading": collection["title"] if collection else "All products",
            "collection": collection,
            "items": [_enrich(p) for p in items],
            "total_count": len(_SAMPLE_PRODUCTS),
            "collections": _SAMPLE_COLLECTIONS,
            "query": (request.query_params.get("q") or "").strip(),
            "status_filter": request.query_params.get("status") or "",
            "collection_filter": request.query_params.get("collection") or "",
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


@app.get("/products/all", response_class=HTMLResponse)
def products_all(request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    q = (request.query_params.get("q") or "").strip().lower()
    status_filter = request.query_params.get("status") or ""
    collection_filter = request.query_params.get("collection") or ""

    items = list(_SAMPLE_PRODUCTS)
    if q:
        items = [p for p in items if q in p["title"].lower()]
    if status_filter:
        items = [p for p in items if p["status"] == status_filter]
    if collection_filter:
        items = [p for p in items if collection_filter in p["collection_handles"]]

    return _render_products_list(request, user, items, collection=None)


@app.get("/products/collection/{handle}", response_class=HTMLResponse)
def products_collection(handle: str, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    collection = next((c for c in _SAMPLE_COLLECTIONS if c["handle"] == handle), None)
    if collection is None:
        raise HTTPException(status_code=404)

    q = (request.query_params.get("q") or "").strip().lower()
    status_filter = request.query_params.get("status") or ""

    items = [p for p in _SAMPLE_PRODUCTS if handle in p["collection_handles"]]
    if q:
        items = [p for p in items if q in p["title"].lower()]
    if status_filter:
        items = [p for p in items if p["status"] == status_filter]

    return _render_products_list(request, user, items, collection=collection)


@app.post("/products/sync")
def products_sync(request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    _flash(
        request,
        "Sync backend not built yet — this preview uses sample data.",
        "info",
    )
    return RedirectResponse("/products", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail_page(product_id: int, request: Request, db: DbDep) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    base = next((p for p in _SAMPLE_PRODUCTS if p["id"] == product_id), None)
    if base is None:
        raise HTTPException(status_code=404)

    product = {
        **_enrich(base),
        "total_inventory": base["inventory"],
        "created_at": "2025-11-14",
        "updated_at": "2026-04-22",
        "description_html": (
            "<p>This is a sample product description shown for the design preview. "
            "Once Shopify sync is wired up, the real product description (HTML) "
            "renders here verbatim.</p>"
        ),
        "shopify_admin_url": "#",
        "variants": [
            {
                "sku": f"{base['sku']}-V{i}",
                "title": f"Variant {i}",
                "price": "29.00",
                "inventory_quantity": 25 + i * 4,
                "options": [f"Color {i}", "Size M"],
            }
            for i in range(1, base["variant_count"] + 1)
        ],
        "ads_performance": None,
        "collections": [_collection_title(h) for h in base["collection_handles"]],
    }

    return templates.TemplateResponse(
        request,
        "product_detail.html",
        {
            "version": __version__,
            "user": user,
            "active": "products",
            "product": product,
        },
    )


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
