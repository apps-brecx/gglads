from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gglads import __version__
from gglads.config import get_settings
from gglads.db.session import ping as db_ping

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
def status_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    db_ok, db_detail = db_ping()
    context = {
        "request": request,
        "version": __version__,
        "app_env": settings.app_env,
        "dry_run": settings.dry_run,
        "autonomous_mode": settings.autonomous_mode,
        "db_ok": db_ok,
        "db_detail": db_detail if db_ok else db_detail,
        "checks": [
            ("Web service", True, "FastAPI is responding"),
            ("Database", db_ok, db_detail if db_ok else "Connection failed"),
            (
                "Anthropic key",
                bool(settings.anthropic_api_key),
                "Not configured yet" if not settings.anthropic_api_key else "Set",
            ),
            (
                "Shopify token",
                bool(settings.shopify_admin_api_token),
                "Not configured yet" if not settings.shopify_admin_api_token else "Set",
            ),
            (
                "Google Ads",
                bool(settings.google_ads_developer_token),
                "Not configured yet"
                if not settings.google_ads_developer_token
                else "Set",
            ),
            (
                "Google OAuth (login)",
                bool(settings.google_oauth_client_id),
                "Not configured yet"
                if not settings.google_oauth_client_id
                else "Set",
            ),
        ],
    }
    return templates.TemplateResponse("status.html", context)
