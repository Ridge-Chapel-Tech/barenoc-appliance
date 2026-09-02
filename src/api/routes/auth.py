from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from fastapi.responses import RedirectResponse
import logging
import re

logger = logging.getLogger("auth")
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from models import User, AuthSession
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
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
import secrets as _secrets
import uuid as _uuid
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
from mfa import mfa_required_for, verify_totp, generate_secret, provisioning_uri

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ── Session + cookie helpers (P0 revocation batch 2026-08-25) ──

def _session_idle_min() -> int:
    """Compliance session policy: idle window in minutes (0 = disabled)."""
    try:
        from llm_providers import read_env_file
        return int(read_env_file().get("SESSION_IDLE_TIMEOUT_MIN", "0") or 0)
    except Exception:
        return 0


def _session_lockout_after() -> int:
    """Compliance session policy: lockout after N bad passwords (0 = off)."""
    try:
        from llm_providers import read_env_file
        return int(read_env_file().get("SESSION_LOCKOUT_AFTER", "0") or 0)
    except Exception:
        return 0


def _record_failed_login(db: Session, user: User) -> None:
    after = _session_lockout_after()
    user.failed_logins = (user.failed_logins or 0) + 1
    if after > 0 and user.failed_logins >= after:
        user.locked_until = datetime.utcnow() + timedelta(minutes=15)
        user.failed_logins = 0
    db.commit()


def _reset_failed_logins(db: Session, user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None

def _client_ip(request: Request) -> str:
    """Best-effort client IP: last X-Forwarded-For hop (nginx appends the real
    remote last), else the socket peer. None-tolerant (direct test callers)."""
    try:
        if request is None:
            return ""
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[-1].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


def _ua(request: Request) -> str:
    if request is None:
        return ""
    return (request.headers.get("user-agent") or "")[:250]


def _cookie_secure(request: Request) -> bool:
    """Secure cookies when the client is on HTTPS (prod: nginx TLS); plain HTTP
    (dev/test harnesses) keeps them insecure so the LAN flow still works."""
    try:
        if request is None:
            return False
        return (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
    except Exception:
        return False


def _mint_tokens(db: Session, user: User, request: Request,
                 groups=None, auth_method="password") -> tuple:
    """Mint access + refresh JWTs and record the refresh session (revocable).

    Returns (access_token, refresh_token). The refresh token's `jti` lands in
    auth_sessions — /logout revokes it instantly; /refresh validates it.
    """
    ver = user.token_version or 0
    jti = _uuid.uuid4().hex
    access = create_access_token({"sub": user.username, "role": user.role,
                                  "groups": groups or [], "auth_method": auth_method},
                                 ver=ver)
    refresh = create_refresh_token({"sub": user.username, "role": user.role,
                                    "groups": groups or [], "auth_method": auth_method},
                                   jti=jti, ver=ver)
    db.add(AuthSession(
        user_id=user.id, jti=jti,
        expires_at=datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        ip=_client_ip(request), user_agent=_ua(request),
    ))
    db.commit()
    return access, refresh


def _set_auth_cookies(response: Response, request: Request, access: str,
                      refresh: str = None, access_max_age: int = None):
    """Set the auth cookies on a response. The access cookie stays JS-readable
    (the mobile chat SPA reads it into localStorage); the refresh cookie is
    HttpOnly + same-site — the browser keeps it, JS never sees it."""
    secure = _cookie_secure(request)
    response.set_cookie(
        key="access_token", value=access,
        max_age=access_max_age or (ACCESS_TOKEN_EXPIRE_MINUTES * 60),
        secure=secure, httponly=False, samesite="lax", path="/")
    if refresh is not None:
        response.set_cookie(
            key="refresh_token", value=refresh,
            max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
            secure=secure, httponly=True, samesite="lax", path="/")


def _clear_auth_cookies(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")


@router.post("/login")
def login(request: LoginRequest, response: Response, db: Session = Depends(get_db),
         http_request: Request = None):
    # case-insensitive usernames: "Admin" and "admin" are the same account
    user = db.query(User).filter(
        func.lower(User.username) == request.username.strip().lower()).first()

    # Lockout (session policy): refuse a locked account before password verify
    # (no timing oracle, no brute-force window).
    if user and user.locked_until and user.locked_until > datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account temporarily locked after repeated failures — try again later",
        )

    if not user or not verify_password(request.password, user.hashed_password):
        if user:
            _record_failed_login(db, user)
        try:
            from audit import log_event as _le
            _le(db, "auth.login_failed", request.username.strip().lower() or "?",
                {"ip": _client_ip(request) or ""}, None)
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    # MFA gate (compliance): password-only admin/operator sign-in requires a
    # valid TOTP code while enforcement is on. Passkey (OIDC) login is already
    # strong auth and does not pass through here.
    if mfa_required_for(user):
        if not verify_totp(user.otp_secret, request.totp_code or ""):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MFA required: provide your TOTP code (or sign in with a passkey)",
            )

    _reset_failed_logins(db, user)

    try:
        from audit import log_event as _le
        _le(db, "auth.login", user.username,
            {"ip": _client_ip(request) or "", "method": "password"}, None)
    except Exception:
        pass

    # Update last login
    user.last_login = datetime.utcnow()
    db.commit()

    access_token, refresh_token = _mint_tokens(db, user, http_request)
    # Set cookie for template auth check (refresh is HttpOnly — revocable).
    _set_auth_cookies(response, http_request, access_token, refresh_token)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "password_change_required": bool(user.must_change_password),
    }


@router.post("/register")
def register(request: RegisterRequest, response: Response, db: Session = Depends(get_db),
            http_request: Request = None):
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
    access_token, refresh_token = _mint_tokens(db, user, http_request)
    _set_auth_cookies(response, http_request, access_token, refresh_token)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
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
    # Revoke EVERYTHING (P0): bump the token version (kills all outstanding
    # access + refresh JWTs) and mark every session row revoked. The client
    # must sign in again with the new password — the intent of the change.
    user.token_version = (user.token_version or 0) + 1
    db.query(AuthSession).filter(
        AuthSession.user_id == user.id,
        AuthSession.revoked_at.is_(None)).update(
        {AuthSession.revoked_at: datetime.utcnow()})
    db.commit()
    return {"status": "ok"}


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    """Exchange a refresh token for a fresh access token.

    The refresh token may arrive via Bearer header (API clients) or the
    HttpOnly refresh_token cookie (browser). Fail-closed: the session row
    must exist, belong to the user, be unrevoked and unexpired, and the
    token version must match — otherwise 401, no new token.
    """
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    else:
        token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token provided")

    payload = decode_token(token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    username = payload.get("sub")
    user = db.query(User).filter(func.lower(User.username) == (username or "").lower()).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    jti = payload.get("jti")
    session = db.query(AuthSession).filter(AuthSession.jti == jti).first() if jti else None
    if not session or session.user_id != user.id:
        raise HTTPException(status_code=401, detail="Unknown session")
    if session.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Session revoked — please sign in again")
    if session.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Session expired")
    if (payload.get("ver") or 0) != (user.token_version or 0):
        raise HTTPException(status_code=401, detail="Session revoked — please sign in again")

    # Idle timeout (compliance session policy): the refresh session dies after
    # N idle minutes — the stateless access token lives out its own ≤60-min
    # expiry, but the refresh can't extend the session past inactivity.
    idle_min = _session_idle_min()
    if idle_min > 0:
        last_activity = session.last_used_at or session.created_at
        if last_activity and (datetime.utcnow() - last_activity) > timedelta(minutes=idle_min):
            session.revoked_at = datetime.utcnow()
            db.commit()
            raise HTTPException(status_code=401,
                                detail="Session idle timeout — please sign in again")

    session.last_used_at = datetime.utcnow()
    db.commit()

    new_access = create_access_token({"sub": user.username, "role": user.role,
                                      "groups": payload.get("groups") or (user.device_groups or []),
                                      "auth_method": payload.get("auth_method") or "password"},
                                     ver=user.token_version or 0)
    # Keep the browser's refresh cookie fresh (same jti — still revocable).
    _set_auth_cookies(response, request, new_access, refresh=token)
    return TokenResponse(access_token=new_access, token_type="bearer",
                         expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Revoke the current session and clear the auth cookies.

    The refresh token's session row is marked revoked immediately — the
    refresh token can never be replayed after logout. The (stateless) access
    token expires on its own ≤60-min schedule; password changes kill access
    tokens instantly via the token_version bump.
    """
    revoked = 0
    refresh = request.cookies.get("refresh_token")
    if not refresh:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            refresh = auth_header.split(" ")[1]
    if refresh:
        payload = decode_token(refresh)
        if payload and payload.get("type") == "refresh" and payload.get("jti"):
            session = db.query(AuthSession).filter(
                AuthSession.jti == payload["jti"]).first()
            if session and session.revoked_at is None:
                session.revoked_at = datetime.utcnow()
                revoked = 1
                db.commit()
    _clear_auth_cookies(response)
    return {"status": "ok", "revoked": revoked}


@router.get("/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user)):
    return user


# ── TOTP second factor (compliance MFA enforcement) ──

@router.get("/totp/enroll")
def totp_enroll(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Start TOTP enrollment: generate + store a pending secret; return the
    otpauth provisioning URI + the base32 secret (for manual entry)."""
    secret = generate_secret()
    user.otp_secret = secret
    user.otp_verified = False
    db.commit()
    return {"secret": secret, "uri": provisioning_uri(secret, user.username)}


@router.post("/totp/confirm")
def totp_confirm(data: dict, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Confirm the pending TOTP enrollment with a live code."""
    code = str(data.get("code") or "")
    if not user.otp_secret:
        raise HTTPException(status_code=400,
                            detail="No pending TOTP enrollment — start one first")
    if not verify_totp(user.otp_secret, code):
        raise HTTPException(status_code=400, detail="Invalid code")
    user.otp_verified = True
    db.commit()
    return {"status": "ok"}


@router.post("/totp/disable")
def totp_disable(data: dict, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Remove the TOTP second factor (must present a valid code; refused while
    MFA enforcement is on so the admin can't lock themselves out)."""
    code = str(data.get("code") or "")
    if user.otp_verified and not verify_totp(user.otp_secret, code):
        raise HTTPException(status_code=400, detail="Invalid code")
    if mfa_required_for(user):
        raise HTTPException(status_code=400,
                            detail="MFA enforcement is on — turn it off in Settings → Security first")
    user.otp_secret = None
    user.otp_verified = False
    db.commit()
    return {"status": "ok"}


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
    groups = claims.get("groups") or claims.get("group") or []
    if not isinstance(groups, list):
        groups = [groups]

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
            device_groups=groups,
        )
        db.add(user)
    else:
        user.oidc_sub = user.oidc_sub or (sub or None)
        if name:
            user.display_name = name
        if email:
            user.email = email
        user.role = role
        user.device_groups = groups
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
    access_token, refresh_token = _mint_tokens(db, user, request,
                                               groups=oidc_groups, auth_method="oidc")
    # Set the session cookie on the response we actually RETURN (the injected
    # `response` is discarded) — /dashboard's server-side session check needs it.
    page = _oidc_page(token=access_token)
    page.delete_cookie(FLOW_COOKIE, path="/")
    _set_auth_cookies(page, request, access_token, refresh_token)
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
