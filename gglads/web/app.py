import logging
import traceback
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from gglads import __version__
from gglads.config import get_settings
from gglads.db.session import ping as db_ping

logger = logging.getLogger("gglads.web")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="gglads", version=__version__)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


@app.get("/", response_class=HTMLResponse)
def status_page(request: Request) -> Response:
    settings = get_settings()
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
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"version": __version__, "error": None},
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "version": __version__,
            "error": "Sign-in backend not built yet — this is a design preview.",
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> PlainTextResponse:
    tb = traceback.format_exc()
    logger.error("Unhandled exception on %s: %s", request.url.path, tb)
    settings = get_settings()
    if settings.app_env == "production":
        return PlainTextResponse("Internal Server Error", status_code=500)
    return PlainTextResponse(tb, status_code=500)
