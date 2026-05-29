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


@app.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    return templates.TemplateResponse(
        request,
        "connections.html",
        {"version": __version__, "user": user, "active": "connections"},
    )


@app.post("/connections/anthropic")
@app.post("/connections/shopify")
@app.post("/connections/google-ads")
def connections_save_placeholder(request: Request, db: DbDep) -> Response:
    user, deny = _require_admin(request, db)
    if deny is not None:
        return deny
    return RedirectResponse("/connections", status_code=status.HTTP_303_SEE_OTHER)


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
