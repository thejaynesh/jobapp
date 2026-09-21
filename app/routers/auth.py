"""Login and logout for the web UI."""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from app.templating import build as build_templates

from app.services import auth

logger = logging.getLogger(__name__)

router = APIRouter()
templates = build_templates()


def _client(request: Request) -> str:
    """
    Who to throttle. Behind Caddy every request arrives from the proxy, so the
    forwarded address is the only thing that distinguishes callers.

    The **last** entry, not the first. `X-Forwarded-For` is a client-supplied
    header and Caddy *appends* the peer address to whatever arrived rather than
    replacing it — which is why `trusted_proxies` exists — so a request
    carrying `X-Forwarded-For: 1.2.3.4` reaches us as `1.2.3.4, <real ip>` and
    reading the first entry returns whatever the caller typed. Rotating it per
    request then meant `MAX_ATTEMPTS` and `LOCKOUT_SECONDS` never engaged, on
    a form whose whole threat model is that there is one password and no user
    database.

    The only entry in an XFF chain worth trusting is the one your own hop
    added, and that is the last one.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
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


@router.get("/auth/check")
def auth_check(request: Request):
    """
    Whether this request carries a valid session. For the proxy, not for people.

    Caddy serves `/storage/*` off the shared volume, so those requests never
    reach this application and the middleware that protects every other route
    never ran — which put every generated resume, with the user's full name,
    address, phone and work history, behind nothing but an unguessable path.
    `forward_auth` asks here first.

    204 and 401 rather than a body, because the proxy reads the status and
    discards the rest; and no redirect, because a download is not a navigation
    and a 303 to the login form would be saved as a PDF.
    """
    if not auth.auth_enabled():
        return Response(status_code=204)
    if auth.session_valid(request.cookies.get(auth.SESSION_COOKIE)):
        return Response(status_code=204)
    return Response(status_code=401)


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response
