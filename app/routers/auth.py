"""Login and logout for the web UI."""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services import auth

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _client(request: Request) -> str:
    """
    Who to throttle. Behind nginx every request arrives from the proxy, so the
    forwarded address is the only thing that distinguishes callers.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _safe_next(raw: str | None) -> str:
    """
    Where to land after logging in.

    Only a path on this site: an attacker-supplied `?next=https://elsewhere`
    turns the login form into an open redirect, and `//host` is a protocol
    relative URL that browsers treat as absolute.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/apps"
    return raw


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/apps"):
    if auth.session_valid(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "auth/login.html", {"next": _safe_next(next), "error": None}
    )


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form(""), next: str = Form("/apps")):
    destination = _safe_next(next)
    client = _client(request)

    remaining = auth.locked_out(client)
    if remaining:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "next": destination,
                "error": f"Too many attempts. Try again in {remaining // 60 + 1} minute(s).",
            },
            status_code=429,
        )

    if not auth.verify_password(password):
        auth.record_failure(client)
        logger.warning("login: failed attempt from %s", client)
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"next": destination, "error": "Incorrect password."},
            status_code=401,
        )

    auth.record_success(client)
    response = RedirectResponse(url=destination, status_code=303)
    response.set_cookie(auth.SESSION_COOKIE, auth.issue_session(), **auth.cookie_kwargs())
    logger.info("login: session issued to %s", client)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response
