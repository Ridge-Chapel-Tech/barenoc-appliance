"""MFA helpers — TOTP (pyotp) enrollment/verification + the enforcement gate.

Passkey-first via Pocket ID (OIDC); TOTP is the fallback second factor for
password sign-in when MFA enforcement is on for the admin/operator tier.
Customer-tier (user/tenant) sign-in is deliberately NOT gated — home UX keeps
its streamlined defaults.
"""

import pyotp
from llm_providers import read_env_file
from models import TECH_ROLES


def _bool(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def mfa_enforced(env: dict = None) -> bool:
    env = env if env is not None else read_env_file()
    return _bool(env.get("MFA_ENFORCED"))


def mfa_required_for(user) -> bool:
    """True when this password sign-in must present a second factor.

    Passkey (OIDC) logins are inherently strong and do not pass through here —
    this gate only applies to password authentication to the admin/operator
    tier while enforcement is on.
    """
    if not mfa_enforced():
        return False
    return getattr(user, "role", "") in TECH_ROLES


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=username, issuer_name="BareNOC")


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(str(code).strip().replace(" ", ""))
    except Exception:
        return False
