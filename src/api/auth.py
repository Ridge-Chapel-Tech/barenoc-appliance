import os
import datetime
from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from models import User, ROLE_LEVELS

SECRET_KEY = os.getenv("JWT_SECRET", "change-this-to-a-random-secret")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_minutes: int = None) -> str:
    to_encode = data.copy()
    minutes = expires_minutes if expires_minutes is not None else ACCESS_TOKEN_EXPIRE_MINUTES
    expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.datetime.utcnow() + datetime.timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    user = _resolve_token_user(request, credentials, db)
    # Enforce forced password change: such users may only hit the
    # change-password endpoint (which uses get_current_user_pwchange).
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="password_change_required",
        )
    return user


def get_current_user_pwchange(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Authenticate a user even if they must change their password.

    Only safe to use for the change-password endpoint itself.
    """
    return _resolve_token_user(request, credentials, db)


def _resolve_token_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials,
    db: Session,
) -> User:
    """Resolve the current user from Authorization header or cookie."""
    # Check Authorization header first, then cookie
    token = None
    if credentials is not None:
        token = credentials.credentials
    else:
        token = request.cookies.get("access_token")

    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    username = payload.get("sub")
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    # case-insensitive usernames (tokens carry the stored username; the DB
    # match is case-insensitive too, e.g. after an admin renamed a user)
    user = db.query(User).filter(func.lower(User.username) == username.lower()).first()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


def require_role(role: str):
    """Dependency factory: require a role at or above the given tier.

    Tiers (models.ROLE_LEVELS): admin > technician/operator > readonly >
    user/tenant > agent. The technician tier therefore passes every
    ``require_role("operator")`` gate automatically.
    """
    def role_checker(user: User = Depends(get_current_user)):
        required = ROLE_LEVELS.get(role, 0)
        actual = ROLE_LEVELS.get(user.role, 0)
        if actual < required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user
    return role_checker


def require_any_role(*roles: str):
    """Dependency factory: require the user to hold ONE of the given roles.

    Exact membership, not a hierarchy — used for the `agent` service identity
    (tier-2, but distinct from operator): the agent reaches exactly the
    write endpoints it needs (device credentials, unifi sync/port writes)
    without being admin, and without widening operator's permissions.
    """
    def role_checker(user: User = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user
    return role_checker


def get_access_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> dict:
    """User + token claims (Pocket ID groups, auth method).

    Used for device-group authorization: groups come from the OIDC claims
    embedded at login; auth_method is 'oidc' (passkey) or 'password'.
    """
    user = _resolve_token_user(request, credentials, db)
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="password_change_required",
        )
    token = credentials.credentials if credentials is not None \
        else request.cookies.get("access_token")
    payload = decode_token(token) if token else {}
    return {
        "user": user,
        "groups": payload.get("groups") or [],
        "auth_method": payload.get("auth_method") or "password",
    }


def require_page_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Dependency for server-rendered pages: a valid session is required.

    Missing, expired or invalid tokens redirect (302) to /login. Unlike
    get_current_user this does NOT enforce must_change_password — flagged
    users still need to load the /change-password page.
    """
    try:
        return _resolve_token_user(request, credentials, db)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
            detail="Session expired",
        )
