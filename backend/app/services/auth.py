import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import quote, unquote, urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import settings
from app.models.user import User, UserRole
from app.schemas.auth import LoginRequest, SignupRequest, UserUpdate
from app.services.providers import memory

logger = logging.getLogger("novi.auth")


async def signup(data: SignupRequest, db: Session) -> User:
    existing = db.scalar(select(User).where(User.email == data.email.lower()))
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=data.email.lower(),
        password_hash=security.hash_password(data.password),
        role=UserRole(data.role) if data.role in ("student", "parent") else UserRole.STUDENT,
        first_name=data.name or data.first_name,
        last_name=data.last_name,
        grade=data.grade,
        school=data.school,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    if user.role == UserRole.STUDENT:
        try:
            agent_id = memory.ensure_agent(
                user_id=user.id, name=user.display_name, grade=user.grade,
                school=user.school or None, existing=None,
            )
            if agent_id:
                user.letta_agent_id = agent_id
                db.commit()
        except Exception as exc:
            print(f"[auth] agent creation skipped: {exc}")
    return user


def login(data: LoginRequest, db: Session) -> User:
    user = db.scalar(select(User).where(User.email == data.email.lower()))
    if not user or not security.verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    return user


def update_profile(user: User, data: UserUpdate, db: Session) -> User:
    if data.first_name is not None:
        user.first_name = data.first_name
    if data.last_name is not None:
        user.last_name = data.last_name
    if data.grade is not None:
        user.grade = data.grade
    if data.school is not None:
        user.school = data.school
    if data.avatar is not None:
        user.avatar = data.avatar or None
    db.commit()
    db.refresh(user)
    return user


def change_password(user: User, current_password: str, new_password: str, db: Session) -> User:
    if not security.verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = security.hash_password(new_password)
    db.commit()
    db.refresh(user)
    return user


# --------------------------------------------------------------------- Google OAuth

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


def google_is_configured() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REDIRECT_URI)


def is_safe_next_path(path: str) -> bool:
    return bool(path) and path.startswith("/") and not path.startswith("//") and ".." not in path


def generate_oauth_state(next_path: str = "/login") -> str:
    """Generate a tamper-proof, timestamped OAuth state string signed with SECRET_KEY.

    Format: {nonce}:{timestamp}:{safe_next}:{signature}
    Works across decoupled frontend/backend domains without depending on browser cookies.
    """
    nonce = secrets.token_urlsafe(16)
    timestamp = str(int(time.time()))
    safe_next = quote(next_path if is_safe_next_path(next_path) else "/login", safe="")
    payload = f"{nonce}:{timestamp}:{safe_next}"
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{signature}"


def verify_oauth_state(state: str, cookie_state: str | None = None) -> tuple[bool, str, str]:
    """Verify the OAuth state parameter.

    Returns (is_valid, error_code, next_path).
    Supports:
    1. Cryptographically signed state (HMAC-SHA256): works seamlessly across decoupled domains
       and reverse proxies (e.g. Vercel frontend -> Render backend) without cookie dependency.
    2. Legacy cookie fallback: if old state format is received and cookie matches.
    """
    if not state:
        return False, "google_oauth_requires_code", "/login"

    parts = state.split(":")
    # Signed format: nonce:timestamp:safe_next:signature
    if len(parts) == 4:
        nonce, timestamp_str, next_quoted, signature = parts
        payload = f"{nonce}:{timestamp_str}:{next_quoted}"
        expected_sig = hmac.new(
            settings.SECRET_KEY.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_sig):
            logger.warning("google OAuth state signature mismatch (CSRF guard)")
            return False, "google_oauth_verification_failed", "/login"

        try:
            ts = int(timestamp_str)
        except ValueError:
            return False, "google_oauth_verification_failed", "/login"

        now = int(time.time())
        # Expire state after 10 minutes (600s), with 60s clock skew tolerance
        if (now - ts) > 600 or ts > (now + 60):
            logger.warning("google OAuth state expired (issued at %s, now %s)", ts, now)
            return False, "google_oauth_expired", "/login"

        next_path = unquote(next_quoted)
        if not is_safe_next_path(next_path):
            next_path = "/login"
        return True, "", next_path

    # Fallback to legacy cookie check (nonce:next_path)
    if len(parts) >= 2:
        csrf = parts[0]
        next_path = unquote(":".join(parts[1:]))
        if not is_safe_next_path(next_path):
            next_path = "/login"
        if not cookie_state:
            return False, "google_oauth_expired", next_path
        if csrf != cookie_state:
            return False, "google_oauth_verification_failed", next_path
        return True, "", next_path

    return False, "google_oauth_verification_failed", "/login"


def build_google_authorize_url(state: str, next_path: str = "") -> str:
    state_val = state if (":" in state or not next_path) else f"{state}:{quote(next_path, safe='/')}"
    query = urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "state": state_val,
        "prompt": "select_account",
    })
    return f"{GOOGLE_AUTHORIZE_URL}?{query}"


async def google_login(code: str, db: Session) -> tuple[User, bool]:
    """Exchange a Google authorization code and log the user in (creating them if needed).

    Returns (user, is_new). Confirmed students default to the student role.
    """
    token_payload = await _exchange_google_code(code)
    info = await _verify_google_id_token(token_payload.get("id_token", ""))
    email = (info.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Google account has no email")
    if str(info.get("email_verified")).lower() not in ("true", "1"):
        raise HTTPException(status_code=401, detail="Google email is not verified")
    return _upsert_google_user(email, info.get("name") or "", info.get("picture"), db)


async def _exchange_google_code(code: str) -> dict:
    payload = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=payload)
    except httpx.HTTPError as exc:
        logger.error("Google token exchange HTTP error: %s", exc)
        raise HTTPException(status_code=502, detail="Google token exchange failed") from exc
    if resp.status_code != 200:
        logger.error("Google token exchange failed: status=%s body=%s", resp.status_code, resp.text)
        raise HTTPException(status_code=401, detail="Google token exchange failed")
    return resp.json()


async def _verify_google_id_token(id_token: str) -> dict:
    """Validate the Google ID token and return its verified claims (email, name, picture)."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(GOOGLE_TOKENINFO_URL, params={"id_token": id_token})
    except httpx.HTTPError as exc:
        logger.error("Google token verification HTTP error: %s", exc)
        raise HTTPException(status_code=502, detail="Google token verification failed") from exc
    if resp.status_code != 200:
        logger.error("Google token verification failed: status=%s body=%s", resp.status_code, resp.text)
        raise HTTPException(status_code=401, detail="Invalid Google token")
    info = resp.json()
    if info.get("aud") != settings.GOOGLE_CLIENT_ID:
        logger.error("Google token audience mismatch: aud=%s client_id=%s", info.get("aud"), settings.GOOGLE_CLIENT_ID)
        raise HTTPException(status_code=401, detail="Google token audience mismatch")
    return info


def _upsert_google_user(email: str, name: str, picture: str | None, db: Session) -> tuple[User, bool]:
    user = db.scalar(select(User).where(User.email == email))
    if user:
        if picture and not user.avatar:
            user.avatar = picture
            db.commit()
            db.refresh(user)
        return user, False

    parts = (name or "").strip().split()
    user = User(
        email=email,
        password_hash=security.hash_password(secrets.token_urlsafe(24)),  # no password login for Google accounts
        role=UserRole.STUDENT,
        first_name=parts[0] if parts else "",
        last_name=" ".join(parts[1:]) if len(parts) > 1 else "",
        avatar=picture or None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    try:
        agent_id = memory.ensure_agent(
            user_id=user.id, name=user.display_name, grade=user.grade,
            school=user.school or None, existing=None,
        )
        if agent_id:
            user.letta_agent_id = agent_id
            db.commit()
    except Exception as exc:
        print(f"[auth] google agent creation skipped: {exc}")
    return user, True