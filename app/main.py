from contextlib import asynccontextmanager
import logging
import subprocess
import traceback

from fastapi import FastAPI, Depends, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db, SessionLocal
from app.routers import profile as profile_router
from app.routers.docs import router as docs_router
from app.routers.jobs import router as jobs_router
from app.routers.apps import router as apps_router
from app.routers.settings import router as settings_router
from app.routers.outreach import router as outreach_router

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_templates = Jinja2Templates(directory="app/templates")

_HTTP_TITLES = {
    400: "Bad Request",
    401: "Unauthorised",
    403: "Forbidden",
    404: "Not Found",
    422: "Validation Error",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


def _seed_profile_if_empty() -> None:
    from app.models.profile import Profile
    from app.services.profile_service import apply_seed
    db = SessionLocal()
    try:
        profile = db.query(Profile).first()
        if not profile:
            logger.info("No profile found — skipping seed")
            return
        if profile.data and profile.data.get("experience"):
            logger.info("Profile already has experience — skipping seed")
            return
        apply_seed(db)
        logger.info("Profile seeded with experience, skills, education, and narrative")
    except Exception as exc:
        logger.error("Profile seed failed: %s", exc)
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error("alembic upgrade failed: %s", result.stderr or result.stdout)
        else:
            logger.info("alembic upgrade head: %s", result.stdout.strip() or "up to date")
    except Exception as exc:
        logger.error("alembic upgrade error: %s", exc)
    _seed_profile_if_empty()
    yield


app = FastAPI(title="JobApp", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(profile_router.router)
app.include_router(docs_router)
app.include_router(jobs_router)
app.include_router(apps_router)
app.include_router(settings_router)
app.include_router(outreach_router)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> HTMLResponse:
    status = exc.status_code
    detail = str(exc.detail) if exc.detail else _HTTP_TITLES.get(status, "Error")
    logger.warning("HTTP %s: %s — %s %s", status, detail, request.method, request.url.path)
    if _is_htmx(request):
        return _templates.TemplateResponse(
            "errors/htmx_error.html",
            {"request": request, "status_code": status, "detail": detail},
            status_code=status,
        )
    return _templates.TemplateResponse(
        "errors/error.html",
        {
            "request": request,
            "status_code": status,
            "title": _HTTP_TITLES.get(status, "Error"),
            "detail": detail,
            "traceback": None,
        },
        status_code=status,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> HTMLResponse:
    errors = "; ".join(f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}" for e in exc.errors())
    detail = f"Validation failed — {errors}"
    logger.warning("422 validation error: %s — %s %s", errors, request.method, request.url.path)
    if _is_htmx(request):
        return _templates.TemplateResponse(
            "errors/htmx_error.html",
            {"request": request, "status_code": 422, "detail": detail},
            status_code=422,
        )
    return _templates.TemplateResponse(
        "errors/error.html",
        {
            "request": request,
            "status_code": 422,
            "title": "Validation Error",
            "detail": detail,
            "traceback": None,
        },
        status_code=422,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> HTMLResponse:
    tb = traceback.format_exc()
    logger.error("Unhandled exception on %s %s:\n%s", request.method, request.url.path, tb)
    detail = f"{type(exc).__name__}: {exc}"
    if _is_htmx(request):
        return _templates.TemplateResponse(
            "errors/htmx_error.html",
            {"request": request, "status_code": 500, "detail": detail},
            status_code=500,
        )
    return _templates.TemplateResponse(
        "errors/error.html",
        {
            "request": request,
            "status_code": 500,
            "title": "Internal Server Error",
            "detail": detail,
            "traceback": tb,
        },
        status_code=500,
    )


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return {"status": "ok", "db": db_status}


@app.get("/")
def root():
    return RedirectResponse(url="/apps")
