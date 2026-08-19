from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from fastapi.responses import RedirectResponse
import logging
import re

logger = logging.getLogger("auth")
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from models import User
from schemas import LoginRequest, TokenResponse, UserResponse, ChangePasswordRequest, RegisterRequest
from auth import (
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    get_current_user_pwchange,
    hash_password,
    SECRET_KEY,
    ALGORITHM,
)
import secrets as _secrets
import urllib.parse
from datetime import datetime, timedelta
from jose import jwt as _jwt
from oidc import (
    oidc_config,
    discovery,
    generate_verifier,
    authorize_url,
    exchange_code,
    fetch_userinfo,
    role_from_groups,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login")
def login(request: LoginRequest, response: Response, db: Session = Depends(get_db)):
    # case-insensitive usernames: "Admin" and "admin" are the same account
    user = db.query(User).filter(
        func.lower(User.username) == request.username.strip().lower()).first()
    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    # Update last login
    from datetime import datetime
    user.last_login = datetime.utcnow()
    db.commit()

    access_token = create_access_token({"sub": user.username, "role": user.role,
                                        "groups": [], "auth_method": "password"})

    # Set cookie for template auth check
    response.set_cookie(
        key="access_token",
        value=access_token,
        max_age=3600,
        secure=False,  # HTTP for dev; set True for production HTTPS
        httponly=False,  # False so JS can read it; True for prod
        samesite="lax",
        path="/",
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600,
        "password_change_required": bool(user.must_change_password),
    }


@router.post("/register")
def register(request: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    """Self-registration — "first login = admin, everyone after = user".

    Gated by TENANT_REGISTRATION_ENABLED (default true for hospitality):
    the admin can turn self-signup off and create users by hand instead.
    New accounts are ALWAYS role=user (the customer tier); the admin promotes
    them to technician/admin later (Settings → Users).
    """
    try:
        from llm_providers import read_env_file
        raw = (read_env_file().get("TENANT_REGISTRATION_ENABLED") or "").strip().lower()
        enabled = raw not in ("0", "false", "no", "off") if raw else True
    except Exception:
        enabled = True
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-registration is disabled — ask the administrator for an account",
        )

    # case-insensitive: store usernames lowercase, match case-insensitively
    username = request.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", username):
        raise HTTPException(status_code=422,
                            detail="Username: 3–64 chars, letters/digits/._- only")
    if db.query(User).filter(func.lower(User.username) == username).first():
        raise HTTPException(status_code=409, detail="Username is already taken")
    email = (request.email or "").strip().lower() or None
    if email:
        if db.query(User).filter(User.email == email).first():
            raise HTTPException(status_code=409, detail="Email is already in use")

    user = User(
        username=username,
        email=email,
        display_name=None,
        hashed_password=hash_password(request.password),
        role="user",
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Auto-login (same response shape as /login so the mobile page can switch
    # straight into chat).
    from datetime import datetime
    user.last_login = datetime.utcnow()
    db.commit()
    access_token = create_access_token({"sub": user.username, "role": user.role,
                                        "groups": [], "auth_method": "password"})
    response.set_cookie(
        key="access_token",
        value=access_token,
        max_age=3600,
        secure=False,
        httponly=False,
        samesite="lax",
        path="/",
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600,
        "password_change_required": False,
        "user": {"username": username, "role": "user"},
    }


@router.post("/change-password")
def change_password(
    data: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_pwchange),
):
    """Change your own password (also clears the forced-change flag).

    Uses get_current_user_pwchange so users with must_change_password=True
    (who are blocked from every other endpoint) can still complete this.
    """
    if not verify_password(data.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(data.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    if verify_password(data.new_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="New password must be different from the current one")

    user.hashed_password = hash_password(data.new_password)
    user.must_change_password = False
    db.commit()
    return {"status": "ok"}


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, db: Session = Depends(get_db)):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No token provided")

    token = auth_header.split(" ")[1]
    payload = decode_token(token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    username = payload.get("sub")
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    new_access = create_access_token({"sub": user.username, "role": user.role,
                                       "groups": [], "auth_method": "password"})
    return TokenResponse(access_token=new_access, token_type="bearer", expires_in=3600)


@router.get("/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user)):
    return user


# ── OIDC / Pocket ID passkey login ──

FLOW_COOKIE = "oidc_flow"


def _flow_token(payload: dict) -> str:
    data = {
        **payload,
        "exp": datetime.utcnow() + timedelta(minutes=5),
        "type": "oidc_flow",
    }
    return _jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)


def _read_flow_token(token: str) -> dict:
    if not token:
        return None
    try:
        payload = _jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None
    if payload.get("type") != "oidc_flow":
        return None
    return payload


def _oidc_page(token: str = None, error: str = None) -> Response:
    """Minimal HTML page: sets localStorage token and redirects, or shows error."""
    if token:
        body = f"""<!doctype html><html><head><title>BareNOC</title></head>
<body><script>
localStorage.setItem('access_token', '{token}');
window.location.href = '/dashboard';
</script></body></html>"""
    else:
        err = urllib.parse.quote(error or "OIDC login failed")
        body = f"""<!doctype html><html><head><title>BareNOC</title></head>
<body><h3>Login failed</h3><p>{error or 'OIDC login failed'}</p>
<a href="/login?error={err}">Back to login</a></body></html>"""
    return Response(content=body, media_type="text/html")


def upsert_oidc_user(db: Session, cfg: dict, claims: dict) -> User:
    """Find or create the local user from OIDC claims; set role from groups."""
    sub = str(claims.get("sub", "") or "")
    email = claims.get("email") or None
    name = claims.get("name") or None
    role = role_from_groups(cfg, claims)

    user = None
    if sub:
        user = db.query(User).filter(User.oidc_sub == sub).first()
    if not user and email:
        user = db.query(User).filter(func.lower(User.email) == email.lower()).first()

    if not user:
        user = User(
            username=f"oidc_{sub[:24]}" if sub else f"oidc_{_secrets.token_hex(6)}",
            email=email,
            display_name=name,
            hashed_password=hash_password(_secrets.token_urlsafe(32)),  # no password; passkey is the credential
            role=role,
            is_active=True,
            must_change_password=False,
            oidc_sub=sub or None,
        )
        db.add(user)
    else:
        user.oidc_sub = user.oidc_sub or (sub or None)
        if name:
            user.display_name = name
        if email:
            user.email = email
        user.role = role
        user.must_change_password = False  # passkey auth is strong auth

    db.commit()
    db.refresh(user)
    return user


def _redirect_uri(request: Request) -> str:
    """Build the OIDC callback URL honoring the nginx proxy scheme."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}/api/v1/auth/oidc/callback"


@router.get("/oidc/login")
def oidc_login(request: Request, db: Session = Depends(get_db)):
    """Start the Pocket ID passkey login (PKCE authorization-code flow)."""
    cfg = oidc_config()
    if not cfg["enabled"] or not cfg["client_id"]:
        raise HTTPException(status_code=400, detail="OIDC is not enabled")
    if not discovery(cfg):
        raise HTTPException(status_code=502, detail="OIDC provider unreachable")

    verifier = generate_verifier()
    state = generate_verifier()
    redirect_uri = _redirect_uri(request)
    url = authorize_url(cfg, verifier, state, redirect_uri)

    # NOTE: the state/verifier cookie must be set on the response we actually
    # RETURN (FastAPI's injected `response` is discarded when a redirect is
    # returned) — otherwise the callback can never validate the state.
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        key=FLOW_COOKIE,
        value=_flow_token({"state": state, "verifier": verifier}),
        max_age=300,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@router.get("/oidc/callback")
def oidc_callback(
    request: Request,
    response: Response,
    code: str = None,
    state: str = None,
    error: str = None,
    db: Session = Depends(get_db),
):
    """Handle the Pocket ID redirect: exchange code, provision user, issue JWT."""
    if error:
        return _oidc_page(error=f"Provider error: {error}")
    cfg = oidc_config()
    if not cfg["enabled"]:
        return _oidc_page(error="OIDC is not enabled")

    flow = _read_flow_token(request.cookies.get(FLOW_COOKIE))
    if not flow or flow.get("state") != state:
        return _oidc_page(error="State validation failed — please try again")

    redirect_uri = _redirect_uri(request)
    try:
        token = exchange_code(cfg, code, flow["verifier"], redirect_uri)
        claims = fetch_userinfo(cfg, token["access_token"])
    except Exception as e:
        return _oidc_page(error=f"OIDC exchange failed: {e}")

    user = upsert_oidc_user(db, cfg, claims)
    if not user.is_active:
        return _oidc_page(error="Account is disabled")

    user.last_login = datetime.utcnow()
    db.commit()

    oidc_groups = claims.get("groups") or claims.get("group") or []
    if not isinstance(oidc_groups, list):
        oidc_groups = [oidc_groups]
    logger.debug("OIDC callback: role=%s groups=%r", role_from_groups(cfg, claims), oidc_groups)
    access_token = create_access_token({"sub": user.username, "role": user.role,
                                        "groups": oidc_groups, "auth_method": "oidc"})
    # Set the session cookie on the response we actually RETURN (the injected
    # `response` is discarded) — /dashboard's server-side session check needs it.
    page = _oidc_page(token=access_token)
    page.delete_cookie(FLOW_COOKIE, path="/")
    page.set_cookie(
        key="access_token",
        value=access_token,
        max_age=3600,
        secure=False,
        httponly=False,
        samesite="lax",
        path="/",
    )
    return page


# ── GitHub / Google OAuth login (future work) ──
# Config scaffolding ships in Settings → Identity (toggles off by default).
# The authorization-code flows land here later; these stubs keep the login
# page buttons from 404ing if someone enables a provider ahead of time.

@router.get("/github/login")
def github_login():
    raise HTTPException(status_code=501, detail="GitHub login is not implemented yet (planned feature)")


@router.get("/google/login")
def google_login():
    raise HTTPException(status_code=501, detail="Google login is not implemented yet (planned feature)")
