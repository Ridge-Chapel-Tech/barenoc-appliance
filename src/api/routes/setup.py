"""First-run setup wizard — /api/v1/setup/status, /account, /complete.

The wizard walks a fresh install through the minimal config (one sitting):
admin account (set-your-own — first-run only), LLM key, timezone, site name +
alert email, autonomy profile, backups, first device, and the shareable chat
URL. Until SETUP_COMPLETE is set the wizard + account endpoint are PUBLIC
(no admin session exists yet); afterwards everything is admin-gated.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func
import re

from auth import require_role, create_access_token, hash_password
from database import get_db
from models import Device, User
from routes.settings import _read_backup_conf, _read_env_file, _write_env_file
from typing import Optional

router = APIRouter(prefix="/api/v1/setup", tags=["setup"])


class SetupAccountRequest(BaseModel):
    """Set the appliance admin (first-run only)."""
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    # optional: rotate the OS-level barenoc login (SSH/console) to the same value
    os_password: Optional[str] = Field(None, min_length=8, max_length=128)


class SetupOsPasswordRequest(BaseModel):
    """Rotate the appliance OS login (barenoc) without touching the app admin."""
    password: str = Field(..., min_length=8, max_length=128)


def _rotate_os_password(password: str) -> None:
    """Change the appliance's barenoc OS user password via the docker socket
    (the api container has /var/run/docker.sock but no host root; run a
    throwaway container with the host /etc mounted and chpasswd it)."""
    import httpx
    script = f"echo 'barenoc:{password}' | chpasswd"
    body = {
        "Image": "barenoc-api",  # local, has chpasswd (Debian slim)
        "Cmd": ["sh", "-c", script],
        "HostConfig": {"Binds": ["/etc:/etc"]},
    }
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds="/var/run/docker.sock"),
                          timeout=60.0) as c:
            r = c.post("http://docker/containers/create?name=barenoc-os-passwd",
                       json=body)
            if r.status_code != 201:
                raise RuntimeError(f"docker create: {r.status_code} {r.text[:200]}")
            cid = r.json()["Id"]
            c.post(f"http://docker/containers/{cid}/start").raise_for_status()
            st = {}
            for _ in range(30):
                try:
                    st = c.get(f"http://docker/containers/{cid}/json").json()
                except httpx.HTTPError:
                    break  # already removed — treat as finished
                if not st.get("State", {}).get("Running"):
                    break
                import time
                time.sleep(1)
            try:
                logs = c.get(f"http://docker/containers/{cid}/logs?stdout=1&stderr=1")
                c.delete(f"http://docker/containers/{cid}")
            except httpx.HTTPError:
                pass
            status = st.get("State", {}).get("ExitCode", 0)
            if status != 0:
                raise RuntimeError(f"chpasswd exit {status}: {logs.text[:200]}")
    except httpx.HTTPError as e:
        raise RuntimeError(f"docker socket: {e}")


def _first_run(env: dict) -> bool:
    return str(env.get("SETUP_COMPLETE", "")).strip().lower() not in ("1", "true", "yes")


def _admin_user(db: Session):
    """The appliance admin (the seeded account the wizard claims)."""
    return (db.query(User).filter(User.role == "admin")
            .order_by(User.id.asc()).first())


def _llm_configured(env: dict) -> bool:
    """A provider counts as configured with an API key (hosted) OR as a
    keyless on-prem box (e.g. Ollama) that has a base URL + model."""
    names = {k[len("LLM_PROVIDER_"):-len("_TYPE")].lower() for k in env
             if k.startswith("LLM_PROVIDER_") and k.endswith("_TYPE")}
    for n in names:
        pre = f"LLM_PROVIDER_{n.upper()}"
        if env.get(f"{pre}_API_KEY"):
            return True
        if (env.get(f"{pre}_DEPLOYMENT", "").strip().lower() == "on_prem"
                and env.get(f"{pre}_BASE_URL", "").strip()
                and env.get(f"{pre}_CHAT_MODEL", "").strip()):
            return True
    return False


def _status_dict(env: dict, request: Request, db: Session) -> dict:
    llm_configured = _llm_configured(env)
    base = str(request.base_url).rstrip("/")
    admin = _admin_user(db)
    account_set = bool(admin and not admin.must_change_password)
    return {
        "complete": not _first_run(env),
        "site_name": env.get("CUSTOMER_NAME") or "BareNOC",
        "steps": {
            "account": account_set,
            "llm": llm_configured,
            "timezone": bool((env.get("TZ") or "").strip()),
            "site_name": bool((env.get("CUSTOMER_NAME") or "").strip()),
            "email": bool((env.get("ALERT_EMAIL") or "").strip()),
            "autonomy": bool((env.get("LLM_POLICY_PROFILE") or "").strip()),
            # appliance data is auto-backed-up every 6h (provision/deploy cron)
            # — informational, not a user toggle; USB/snapshot layers are
            # Settings → Backups (needs the guide's host-side setup).
            "backups": True,
        },
        "chat_url": f"{base}/chat",
        "devices_count": 0,
    }


def _admin_gate_or_first_run(request: Request, db: Session = Depends(get_db)):
    """Allow when the setup is still incomplete (pre-admin), else require an
    admin session (manual resolution so first-run stays PUBLIC)."""
    if _first_run(_read_env_file()):
        return None
    from auth import decode_token, security
    token = None
    try:
        creds = security(request)
        token = creds.credentials
    except Exception:
        token = request.cookies.get("access_token")
    payload = decode_token(token) if token else None
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(User).filter(func.lower(User.username) == payload["sub"].lower()).first()
    if user is None or not user.is_active or user.role != "admin":
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return user


@router.get("/status")
def setup_status(request: Request, db: Session = Depends(get_db),
                 _: User = Depends(_admin_gate_or_first_run)):
    s = _status_dict(_read_env_file(), request, db)
    s["devices_count"] = db.query(Device).count()
    return s


@router.post("/account")
def setup_account(data: SetupAccountRequest, request: Request, response: Response,
                  db: Session = Depends(get_db),
                  _: User = Depends(_admin_gate_or_first_run)):
    """Claim the appliance admin (set-your-own username + password).

    First-run only. Case-insensitive usernames: stored lowercase, uniqueness
    is case-insensitive.
    """
    username = data.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", username):
        raise HTTPException(status_code=422,
                            detail="Username: 3–64 chars, letters/digits/._- only")
    admin = _admin_user(db)
    if admin is None:
        raise HTTPException(status_code=409, detail="No admin account to claim — use the login page")
    clash = db.query(User).filter(
        func.lower(User.username) == username,
        User.id != admin.id).first()
    if clash:
        raise HTTPException(status_code=409, detail="Username is already taken")
    admin.username = username
    admin.hashed_password = hash_password(data.password)
    admin.must_change_password = False
    admin.is_active = True
    db.commit()

    # also rotate the OS-level barenoc login if requested (fresh installs:
    # the ISO seed OS password is a documented static that this replaces)
    if data.os_password:
        try:
            _rotate_os_password(data.os_password)
        except Exception as e:
            raise HTTPException(status_code=500,
                                detail=f"Admin account saved, but rotating the SSH login failed: {e}")

    # session like /auth/login so the rest of the wizard is authenticated.
    # Longer than the normal login token (6 h): a fresh-install sitting routinely
    # runs past 60 min (hunting for API keys), and the wizard has no refresh.
    from datetime import datetime as _dt
    admin.last_login = _dt.utcnow()
    db.commit()
    token = create_access_token({"sub": admin.username, "role": admin.role,
                                 "groups": [], "auth_method": "password"},
                                expires_minutes=6 * 60)
    response.set_cookie(key="access_token", value=token, max_age=6 * 3600,
                        secure=False, httponly=False, samesite="lax", path="/")
    return {"access_token": token, "token_type": "bearer",
            "expires_in": 3600, "username": admin.username}


@router.post("/ospassword")
def setup_ospassword(data: SetupOsPasswordRequest, request: Request, response: Response,
                     db: Session = Depends(get_db),
                     user: User = Depends(_admin_gate_or_first_run)):
    """Rotate the appliance's OS login (barenoc — SSH/console)."""
    try:
        _rotate_os_password(data.password)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rotating the SSH login failed: {e}")
    return {"status": "ok"}


@router.post("/complete")
def setup_complete(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(_admin_gate_or_first_run)):
    if not (_admin_user(db) and not _admin_user(db).must_change_password):
        raise HTTPException(status_code=400,
                            detail="Set the admin account first (Setup → Admin account)")
    env = _read_env_file()
    env["SETUP_COMPLETE"] = "true"
    _write_env_file(env)
    return {"status": "ok", "complete": True}
