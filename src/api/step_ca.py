"""step-ca integration — device certificates for adoption (Phase F).

The appliance runs an internal CA (smallstep/step-ca). Devices enroll with a
one-time JWT that BareNOC mints using the `barenoc-devices` JWK provisioner's
private key. Once enrolled, a device presents its certificate over mTLS
(nginx validates against the CA root) and BareNOC links it to the inventory
record.

JWT format replicated from `step ca token` output (verified against the live
CA): header {alg, kid, typ}, payload {aud, exp, iat, iss, jti, nbf, sans, sha,
sub, user}.
"""

import datetime
import json
import os
import secrets
import uuid

from jose import jwt  # python-jose (already in requirements)

from cryptography.hazmat.primitives.serialization import load_pem_private_key

ENV_FILE = "/opt/barenoc/.env"


def read_env() -> dict:
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception:
        pass
    for k, v in os.environ.items():
        env.setdefault(k, v)
    return env


# Paths (mounted read-only into the api container by compose)
PROVISIONER_KEY_PATH = "/opt/barenoc/volumes/step-ca/secrets/barenoc-devices.pem"
CA_ROOT_PATH = "/opt/barenoc/volumes/step-ca/certs/root_ca.crt"
PROVISIONER_NAME = "barenoc-devices"

_cfg_cache = None


def _config() -> dict:
    """CA config: URL, provisioner key/kid, root cert + fingerprint. Cached."""
    global _cfg_cache
    if _cfg_cache:
        return _cfg_cache
    env = read_env()
    ca_url = (env.get("STEPCA_URL") or "https://stepca.barenoc.local:8443").rstrip("/")
    cfg = {"ca_url": ca_url, "provisioner_name": PROVISIONER_NAME}
    try:
        with open(PROVISIONER_KEY_PATH, "rb") as f:
            key = load_pem_private_key(f.read(), password=None)
        pub = key.public_key()
        from cryptography.hazmat.primitives import serialization as _ser
        spki = pub.public_bytes(
            _ser.Encoding.DER, _ser.PublicFormat.SubjectPublicKeyInfo)
        from cryptography.hazmat.primitives.asymmetric import ec
        nums = pub.public_numbers()
        size = (pub.curve.key_size + 7) // 8
        jwk = {
            "crv": "P-256", "kty": "EC",
            "x": _b64url(nums.x.to_bytes(size, "big")),
            "y": _b64url(nums.y.to_bytes(size, "big")),
        }
        import hashlib
        canon = json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode()
        cfg["key"] = key
        cfg["kid"] = _b64url(hashlib.sha256(canon).digest())
    except Exception as e:
        raise RuntimeError(f"step-ca provisioner key unavailable: {e}") from e
    # root fingerprint (what devices must trust) — step's fingerprint is the
    # sha256 of the DER-encoded certificate, NOT the PEM file bytes
    try:
        import hashlib
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(open(CA_ROOT_PATH, "rb").read())
        der = cert.public_bytes(__import__("cryptography.hazmat.primitives.serialization",
                                            fromlist=["Encoding"]).Encoding.DER)
        cfg["root_fingerprint"] = hashlib.sha256(der).hexdigest()
    except Exception:
        cfg["root_fingerprint"] = ""
    _cfg_cache = cfg
    return cfg


def _b64url(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def reset_config_cache():
    global _cfg_cache
    _cfg_cache = None


def root_fingerprint() -> str:
    return _config().get("root_fingerprint", "")


def mint_token(cn: str, sans: "list[str] | None" = None, ttl: int = 600) -> str:
    """Mint a one-time device enrollment JWT (signed with the provisioner key)."""
    cfg = _config()
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    header = {"alg": "ES256", "kid": cfg["kid"], "typ": "JWT"}
    payload = {
        "aud": f"{cfg['ca_url']}/1.0/sign",
        "exp": now + ttl,
        "iat": now,
        "iss": cfg["provisioner_name"],
        "jti": secrets.token_hex(32),
        "nbf": now,
        "sans": sans or [cn],
        "sha": cfg.get("root_fingerprint", ""),
        "sub": cn,
        "user": {},
    }
    return jwt.encode(payload, cfg["key"], algorithm="ES256", headers=header)


def device_cn(device_name: str) -> str:
    """Canonical certificate CN for a device — stable + safe for a subject."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in device_name)
    return f"device-{safe}"
