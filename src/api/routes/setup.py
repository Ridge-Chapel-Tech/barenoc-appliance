"""First-run setup wizard — /api/v1/setup/status, /account, /complete.

The wizard walks a fresh install through setup in one sitting. Two
presentations, one engine:
  - EXPRESS (home default): 4 steps — admin account → network (UniFi) →
    name & share the chat → done. Every skipped step writes a correct home
    default at /setup/complete (cloud LLM, autonomous + pi flag, UniFi
    auto-discover, backups on, email off, browser-detected TZ).
  - ADVANCED (the "Advanced setup" expander): the full 9-step path.

Until SETUP_COMPLETE is set the wizard + account endpoint are PUBLIC
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
from routes.settings import (
    _read_backup_conf, _read_env_file, _write_backup_conf, _write_env_file)
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


class SetupCompleteRequest(BaseModel):
    """Optional payload for /setup/complete (the express wizard sends the
    browser-detected timezone + site name + LLM egress choice; the advanced
    9-step path sends nothing and still receives the home defaults for any
    step it skipped)."""
    timezone: Optional[str] = None
    site_name: Optional[str] = None
    llm_egress: Optional[str] = None  # "cloud" | "local"


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


def _unifi_configured(env: dict) -> bool:
    """UniFi creds present (URL + password or API key)."""
    return bool((env.get("UNIFI_PASSWORD") or "").strip()
                or (env.get("UNIFI_API_KEY") or "").strip())


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
            # express-wizard network step: UniFi creds present
            "network": _unifi_configured(env),
            # appliance data is auto-backed-up every 6h (provision/deploy cron)
            # — informational, not a user toggle; USB/snapshot layers are
            # Settings → Backups (needs the guide's host-side setup).
            "backups": True,
        },
        "chat_url": f"{base}/chat",
        "devices_count": 0,
    }


def apply_home_defaults(env: dict, timezone: Optional[str] = None,
                        site_name: Optional[str] = None,
                        llm_egress: Optional[str] = None) -> dict:
    """Write the best-for-home defaults for every technical choice the
    express wizard does not collect. Mutates + returns env.

    Home defaults (locked v2, 08-25):
      - autonomy  = autonomous (full power) + the pi flag ON
      - UniFi     = auto-discover on + auto-adopt on (adoption happens
                    silently once creds exist)
      - TZ        = browser auto-detect (passed from the wizard)
      - site name = the wizard's "name your network" value
      - LLM egress = cloud (default) — the two-choice card maps to the SAME
                    compliance control the Security panel toggles
      - email     = OFF until a recipient is added (ALERT_EMAIL untouched)
      - backups   = on, local defaults (USB schedule written separately)
    Values already present (e.g. an advanced-path choice) are never overwritten.
    """
    if not (env.get("LLM_POLICY_PROFILE") or "").strip():
        env["LLM_POLICY_PROFILE"] = "autonomous"
        env["PI_AGENT_ENABLED"] = "true"  # bare value — the 08-17 pi-flag fix

    # UniFi auto-discover/auto-adopt are home defaults; explicit false wins.
    env.setdefault("UNIFI_AUTOSYNC_ENABLED", "true")
    env.setdefault("UNIFI_AUTO_ADOPT", "true")

    if timezone:
        tz = str(timezone).strip()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown timezone '{tz}' — use an IANA name like America/New_York")
        env["TZ"] = tz

    if site_name and str(site_name).strip():
        env["CUSTOMER_NAME"] = str(site_name).strip()

    # The express LLM card drives the SAME compliance control the Security
    # panel uses (COMPLIANCE_LLM_EGRESS + the effective LLM_EGRESS mirror).
    if llm_egress in ("cloud", "local"):
        import compliance
        compliance.set_control("llm_egress", llm_egress, env=env, persist=False)

    env["SETUP_COMPLETE"] = "true"
    return env


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
                                expires_minutes=6 * 60,
                                ver=admin.token_version or 0)
    secure = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
    response.set_cookie(key="access_token", value=token, max_age=6 * 3600,
                        secure=secure, httponly=False, samesite="lax", path="/")
    return {"access_token": token, "token_type": "bearer",
            "expires_in": 6 * 3600, "username": admin.username}


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
def setup_complete(data: Optional[SetupCompleteRequest] = None,
                   db: Session = Depends(get_db),
                   user: User = Depends(_admin_gate_or_first_run)):
    """Finish setup: stamp SETUP_COMPLETE + write the home defaults for every
    skipped step (timezone from the wizard's browser, site name, LLM egress,
    autonomous + pi flag, UniFi auto-discover, backups-on-local, auto-update
    on by default).

    The express wizard sends {timezone, site_name, llm_egress}; the advanced
    9-step path sends nothing and still receives the defaults for anything it
    left unset (already-set values are never overwritten)."""
    if not (_admin_user(db) and not _admin_user(db).must_change_password):
        raise HTTPException(status_code=400,
                            detail="Set the admin account first (Setup → Admin account)")
    data = data or SetupCompleteRequest()
    env = _read_env_file()
    apply_home_defaults(
        env,
        timezone=data.timezone,
        site_name=data.site_name,
        llm_egress=data.llm_egress,
    )
    _write_env_file(env)
    # Auto-update ON by default (2026-08-25): a fresh install lands on the
    # default weekly schedule (Sunday 03:00 local) with zero Advanced-page
    # visits. Idempotent — never overwrites an existing (opted-out) conf.
    from routes import updates as _updates
    _updates.ensure_default_update_schedule(
        db=db, actor=(user.username if user else "setup"))
    # Backups: on, local defaults (Wednesday 02:00) — the host poller reads
    # this conf; a BYO Docker host simply ignores it (no appliance host).
    _write_backup_conf({
        "USB_BACKUP_ENABLED": "true",
        "USB_BACKUP_DAY": "3",
        "USB_BACKUP_HOUR": "2",
        "RUN_USB_BACKUP_NOW": "false",
        "BACKUP_TARGET_DIR": _read_backup_conf().get("BACKUP_TARGET_DIR", ""),
    })
    # Audit the setup sweep (home defaults + auto-update schedule applied).
    from audit import log_event
    log_event(db, "settings_change", user.username if user else "setup", {
        "section": "setup",
        "fields": ["home_defaults", "update_schedule_default",
                   "backup_schedule_default"],
    })
    return {"status": "ok", "complete": True}
