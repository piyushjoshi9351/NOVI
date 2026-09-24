import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import settings
from app.core.database import get_db
from app.services import auth as auth_service

logger = logging.getLogger("novi.google_auth")

router = APIRouter(tags=["auth"])


@router.get("/auth/google", include_in_schema=False)
async def google_oauth_start(next: str = "/login"):
    """Redirect the browser to Google's consent screen.

    `next` is the frontend route (/login or /signup) the user returns to.
    The OAuth `state` is cryptographically signed with HMAC-SHA256, allowing
    stateless CSRF protection that works reliably across decoupled domains
    (e.g., Vercel frontend and Render backend).
    """
    if not auth_service.google_is_configured():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    if not _safe_next(next):
        next = "/login"

    state = auth_service.generate_oauth_state(next)
    response = RedirectResponse(auth_service.build_google_authorize_url(state), status_code=302)
    response.set_cookie(
        "google_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=not settings.DEBUG,
    )
    return response


@router.get("/auth/google/callback", include_in_schema=False)
async def google_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    """Exchange the Google code, log the user in, and bounce them back to the frontend."""
    if not auth_service.google_is_configured() or not code or not state:
        return _google_fail(request, detail="google_oauth_requires_code")

    cookie_state = request.cookies.get("google_oauth_state")
    valid, err_detail, next_path = auth_service.verify_oauth_state(state, cookie_state)
    if not valid:
        return _google_fail(request, detail=err_detail)

    try:
        user, is_new = await auth_service.google_login(code, db)
    except HTTPException as exc:
        logger.warning("google login rejected: %s", exc.detail)
        return _google_fail(request, detail="google_oauth_rejected")
    except Exception:
        logger.exception("google login failed unexpectedly", exc_info=True)
        return _google_fail(request, detail="google_oauth_failed")

    role = getattr(user.role, "value", user.role)
    token = security.create_access_token(str(user.id), str(role))
    dest = f"{settings.FRONTEND_URL.rstrip('/')}{next_path}?google_token={token}&google_new={'1' if is_new else '0'}"
    response = RedirectResponse(dest, status_code=302)
    if cookie_state:
        response.delete_cookie("google_oauth_state")
    return response


def _safe_next(path: str) -> bool:
    return auth_service.is_safe_next_path(path)


def _google_fail(request: Request, detail: str = "google_oauth_failed") -> RedirectResponse:
    response = RedirectResponse(
        f"{settings.FRONTEND_URL.rstrip('/')}/login?google_error={detail}",
        status_code=302,
    )
    if request.cookies.get("google_oauth_state"):
        response.delete_cookie("google_oauth_state")
    return response