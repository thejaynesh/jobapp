from contextlib import asynccontextmanager
import logging
import subprocess
import traceback
from urllib.parse import quote

from fastapi import FastAPI, Depends, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.config import settings
from app.database import get_db, SessionLocal
from app.services import auth
from app.routers.auth import router as auth_router
from app.routers import profile as profile_router
from app.routers.docs import router as docs_router
from app.routers.jobs import router as jobs_router
from app.routers.apps import router as apps_router
from app.routers.settings import router as settings_router
from app.routers.outreach import router as outreach_router  # /outreach pages + /api/apps trigger
from app.routers.runs import router as runs_router
from app.routers.llm import router as llm_router
from app.routers.funnel import router as funnel_router
from app.routers.agent import router as agent_router

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
        data = profile.data or {}
        if data.get("experience") and data.get("projects"):
            logger.info("Profile already has experience and projects — skipping seed")
            return
        apply_seed(db)
        logger.info("Profile seeded with experience, projects, skills, education, and narrative")
    except Exception as exc:
        logger.error("Profile seed failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# Why the schema could not be brought up to date, or None. Set by the lifespan,
# read by the middleware, which refuses to serve while it is populated.
#
# The alternative — log it and carry on — means new code runs against an old
# schema, and the first symptom is an UndefinedTable traceback from whichever
# feature happens to touch the missing column first. That reads as a bug in the
# feature rather than an unapplied migration, which is a long way from the
# actual problem. Refusing is the same reasoning as the auth 503: a deployment
# that cannot reach a working state should say so rather than half-serve.
_migration_failure: str | None = None


def migration_failure() -> str | None:
    return _migration_failure


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _migration_failure
    _migration_failure = None
    try:
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            # The tail rather than the head: alembic puts the actual cause last,
            # under the banner lines that are the same for every failure.
            detail = (result.stderr or result.stdout or "").strip()[-800:]
            _migration_failure = detail or "alembic exited non-zero with no output."
            logger.error(
                "SCHEMA MIGRATION FAILED — serving 503 until fixed: %s", _migration_failure
            )
        else:
            logger.info("alembic upgrade head: %s", result.stdout.strip() or "up to date")
    except Exception as exc:
        _migration_failure = str(exc)
        logger.error("SCHEMA MIGRATION FAILED — serving 503 until fixed: %s", exc)
    _seed_profile_if_empty()
    problem = auth.misconfiguration()
    if problem:
        logger.error("AUTHENTICATION NOT ENFORCED — serving 503 until fixed: %s", problem)
    elif not auth.auth_enabled():
        logger.warning(
            "AUTH_ENABLED=false — every route is open. Only do this when the app "
            "is not reachable from the internet."
        )
    insecure_cookie = auth.insecure_cookie_warning()
    if insecure_cookie:
        logger.warning(insecure_cookie)
    yield


app = FastAPI(title="JobApp", lifespan=lifespan)

_cors_origins = [
    origin.strip()
    for origin in (settings.CORS_ALLOW_ORIGINS or "").split(",")
    if origin.strip()
]
if _cors_origins:
    # For the extension, whose origin is chrome-extension://<id>. Credentials
    # stay off: the agent authenticates with a bearer token, so allowing cookies
    # cross-origin would widen the surface for nothing.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(profile_router.router)
app.include_router(docs_router)
app.include_router(jobs_router)
app.include_router(apps_router)
app.include_router(settings_router)
app.include_router(outreach_router)
app.include_router(runs_router)
app.include_router(funnel_router)
app.include_router(llm_router)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# Reachable without a session. /health is here so container health checks and
# uptime monitors keep working; it reports liveness and nothing about the user.
_PUBLIC_PATHS = frozenset({"/health", "/login"})
_PUBLIC_PREFIXES = ("/static/",)

# Served with a bearer token instead of a session — there is no browser here to
# show a login form to.
_AGENT_PREFIX = "/api/agent"


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def _unauthenticated(request: Request) -> Response:
    """
    Turn away a browser request, in whichever way the caller can act on.

    An HTMX request that gets a 303 will follow it and paste the login page
    into whatever fragment it was updating, so those get HX-Redirect instead,
    which makes the browser navigate.
    """
    destination = request.url.path
    if request.url.query:
        destination = f"{destination}?{request.url.query}"
    login_url = f"/login?next={quote(destination, safe='')}"

    if _is_htmx(request):
        return Response(status_code=401, headers={"HX-Redirect": login_url})
    return RedirectResponse(url=login_url, status_code=303)


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    """
    The single gate in front of everything.

    Deliberately middleware rather than a per-router dependency: a dependency
    protects the routes somebody remembered to add it to, and the failure mode
    of forgetting is an endpoint that silently serves the user's application
    history to the internet.
    """
    path = request.url.path
    # Every router builds its own Jinja2Templates, but `request` is in all of
    # their contexts, so the scope is the one channel that reaches every
    # template without threading a variable through every route signature.
    request.scope["auth_enabled"] = auth.auth_enabled()

    # /health stays up so container health checks and uptime monitors keep
    # working. Restarting would not fix a migration that cannot apply.
    if _migration_failure and path != "/health":
        return JSONResponse(
            {
                "detail": (
                    "The database schema is not up to date, so the application "
                    f"is refusing to serve against it. {_migration_failure}"
                )
            },
            status_code=503,
        )

    problem = auth.misconfiguration()
    if problem and path != "/health":
        # Configured to authenticate but unable to. Refusing is the only safe
        # reading: falling back to open access would make a misconfiguration
        # indistinguishable from a working deployment.
        return JSONResponse(
            {"detail": f"Authentication is not configured. {problem}"},
            status_code=503,
        )

    if not auth.auth_enabled() or _is_public(path):
        return await call_next(request)

    if path.startswith(_AGENT_PREFIX):
        if not auth.agent_auth_configured():
            return JSONResponse(
                {"detail": "AGENT_TOKEN is not set, so the agent API is closed."},
                status_code=503,
            )
        token = auth.bearer_token(request.headers.get("Authorization"))
        if not auth.verify_agent_token(token):
            return JSONResponse(
                {"detail": "Invalid or missing agent token."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    if auth.session_valid(request.cookies.get(auth.SESSION_COOKIE)):
        return await call_next(request)

    return _unauthenticated(request)


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
    from app.database import pool_status

    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    # The pool goes with it. Exhaustion is invisible until it is total, and then
    # every page fails at once with a message that names no request in
    # particular — so the count that would have said so lives where a monitor
    # already looks.
    return {"status": "ok", "db": db_status, "pool": pool_status()}


@app.get("/")
def root():
    return RedirectResponse(url="/apps")
