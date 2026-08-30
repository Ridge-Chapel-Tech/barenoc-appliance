"""Remote/offsite backup — one S3-compatible transport, two flavors.

BareNOC-managed (subscription) and bring-your-own (BYO) share the SAME
client-side encryption + archive + upload layer; the only difference is where
the bytes land and who owns the credentials.

Security model (locked design):
  * A per-install data-encryption key (DEK) is generated on first use and kept
    in a 0600 file beside the Fernet key. The archive is encrypted BEFORE it
    leaves the appliance; BareNOC never sees plaintext.
  * The DEK is surfaced as a human-readable RECOVERY KEY exactly once — losing
    it means the offsite copy is unrecoverable (documented honestly).
  * BYO credentials are Fernet-encrypted at rest (same mechanism as device
    creds); the managed profile is gate-provisioned via env (like SMTP).

This module is imported by BOTH the Settings API routes and the host-side
offsite job (run inside the api container via `docker exec`), so keep it
dependency-light: stdlib + cryptography (already in requirements.txt).
"""

import base64
import datetime
import glob
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ── Paths (bind-mounted into the api container; the host cron sees the same) ──
APP_DIR = "/opt/barenoc"
BACKUP_DIR = os.path.join(APP_DIR, "backups")
DEK_FILE = os.path.join(APP_DIR, "volumes/db/offsite-dek.key")
OFFSITE_CONF = os.path.join(APP_DIR, "volumes/backup_status/offsite.conf")
OFFSITE_CREDS = os.path.join(APP_DIR, "volumes/backup_status/offsite-credentials")
OFFSITE_STATUS_JSON = os.path.join(APP_DIR, "volumes/backup_status/offsite_status.json")
OFFSITE_STORE = os.path.join(BACKUP_DIR, "offsite")
LATEST_ARCHIVE_POINTER = os.path.join(BACKUP_DIR, "latest_archive")

# ── Archive format (documented in docs/remote-backup.md — keep in sync) ──
MAGIC = b"BARENOC_OFFSITE_V1"
AAD = b"barenoc-offsite-v1"
DEK_BYTES = 32          # AES-256-GCM
NONCE_BYTES = 12        # GCM standard

# ── Managed (subscription) gating — beta static key ─────────────────────────
# Offline-verifiable, per-install. This is the BETA placeholder: the Stripe →
# webhook → plan-key automation (a separate later lane) will replace the static
# key with signed, expiring entitlements. Env override lets the gate provision
# a different key per box without a code change.
BETA_PLAN_KEY = "barenoc-beta-managed-2026"


def verify_plan_key(key: str) -> dict:
    """Offline-verifiable check of the managed-plan key (beta static key).

    Returns {"valid": bool, "tier": str|None, "beta": bool}. Constant-time
    compare; the key is documented and the gate provisions it per box.
    """
    key = (key or "").strip()
    expected = os.environ.get("BARENOC_BETA_PLAN_KEY", BETA_PLAN_KEY).strip()
    if key and hmac.compare_digest(key, expected):
        return {"valid": True, "tier": "managed", "beta": True}
    return {"valid": False, "tier": None, "beta": False}


# ── Recovery key (the DEK, shown once) ──────────────────────────────────────

def ensure_dek() -> bytes:
    """Get or create the per-install offsite data-encryption key (0600)."""
    os.makedirs(os.path.dirname(DEK_FILE), exist_ok=True)
    if os.path.exists(DEK_FILE):
        with open(DEK_FILE, "rb") as f:
            raw = f.read().strip()
        if len(raw) == DEK_BYTES:
            return raw
    raw = os.urandom(DEK_BYTES)
    fd = os.open(DEK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    os.chmod(DEK_FILE, 0o600)
    return raw


def encode_recovery_key(raw: bytes) -> str:
    """Human-readable form of the DEK: uppercase base32, no padding, groups of 4."""
    b32 = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(b32[i:i + 4] for i in range(0, len(b32), 4))


def decode_recovery_key(s: str) -> bytes:
    """Reverse of encode_recovery_key (whitespace/case/dashes tolerant)."""
    compact = re.sub(r"[\s\-_]", "", s).upper()
    compact += "=" * ((8 - len(compact) % 8) % 8)
    return base64.b32decode(compact)


# ── Archive encryption / decryption (client-side, before upload) ────────────

def encrypt_file_to(plain_path: str, enc_path: str) -> int:
    """Encrypt a plaintext file with the DEK → MAGIC\\nnonce\\nciphertext.

    Returns the size in bytes of the encrypted file. AES-256-GCM with AAD
    bound to the format magic, so a truncated/tampered file fails loudly.
    """
    raw = ensure_dek()
    nonce = os.urandom(NONCE_BYTES)
    with open(plain_path, "rb") as f:
        plaintext = f.read()
    aes = AESGCM(raw)
    ciphertext = aes.encrypt(nonce, plaintext, AAD)
    payload = (MAGIC + b"\n" + base64.b64encode(nonce) + b"\n"
               + base64.b64encode(ciphertext) + b"\n")
    os.makedirs(os.path.dirname(enc_path), exist_ok=True)
    umask = os.umask(0o077)
    try:
        with open(enc_path, "wb") as f:
            f.write(payload)
    finally:
        os.umask(umask)
    os.chmod(enc_path, 0o600)
    return len(payload)


def decrypt_file_to(enc_path: str, out_path: str, raw_key: bytes) -> int:
    """Decrypt an offsite archive with the raw DEK. Returns plaintext size."""
    with open(enc_path, "rb") as f:
        parts = f.read().split(b"\n", 2)
    if len(parts) != 3 or parts[0] != MAGIC:
        raise ValueError("not a BareNOC offsite archive (bad magic)")
    nonce = base64.b64decode(parts[1])
    ciphertext = base64.b64decode(parts[2])
    aes = AESGCM(raw_key)
    plaintext = aes.decrypt(nonce, ciphertext, AAD)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(plaintext)
    return len(plaintext)


def decrypt_archive_with_recovery_key(enc_path: str, out_path: str, recovery_key: str) -> int:
    """Convenience: decrypt with the human recovery-key string (for tests + docs)."""
    return decrypt_file_to(enc_path, out_path, decode_recovery_key(recovery_key))


# ── Config (written by Settings → Backups → Offsite; read by the job) ────────

def read_offsite_conf() -> dict:
    cfg = {}
    try:
        with open(OFFSITE_CONF) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


def write_offsite_conf(cfg: dict):
    os.makedirs(os.path.dirname(OFFSITE_CONF), exist_ok=True)
    keys = ("OFF_SITE_MODE", "OFF_SITE_DAY", "OFF_SITE_HOUR",
            "OFF_SITE_RETENTION_DAYS", "PLAN_KEY",
            "BYO_ENDPOINT", "BYO_BUCKET", "BYO_REGION", "BYO_PREFIX",
            "RECOVERY_KEY_SHOWN")
    lines = ["# BareNOC offsite backup config (written by Settings → Backups → Offsite)",
             "# Read by scripts/offsite_backup.sh → the api-container offsite job."]
    for k in keys:
        lines.append(f"{k}={cfg.get(k, '')}")
    with open(OFFSITE_CONF, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(OFFSITE_CONF, 0o644)
    except Exception:
        pass


def read_offsite_credentials() -> dict:
    """Decrypt BYO S3 credentials (Fernet-encrypted at rest, 0600)."""
    try:
        with open(OFFSITE_CREDS) as f:
            data = json.load(f)
    except Exception:
        return {"access_key": "", "secret": ""}
    from crypto import decrypt
    def _plain(v):
        p = decrypt(v or "")
        return "" if p == "[encrypted]" else p
    return {
        "access_key": _plain(data.get("byo_access_key", "")),
        "secret": _plain(data.get("byo_secret", "")),
    }


def write_offsite_credentials(access_key: str, secret: str):
    from crypto import encrypt
    os.makedirs(os.path.dirname(OFFSITE_CREDS), exist_ok=True)
    payload = {"byo_access_key": encrypt(access_key or ""),
               "byo_secret": encrypt(secret or "")}
    fd = os.open(OFFSITE_CREDS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    os.chmod(OFFSITE_CREDS, 0o600)


def managed_profile(env: dict | None = None) -> dict:
    """The gate-provisioned managed (BareNOC backend) profile, from env."""
    env = env if env is not None else os.environ
    site = (env.get("SITE_ID") or "1").strip() or "1"
    return {
        "endpoint": (env.get("OFFSITE_MANAGED_ENDPOINT") or "").strip(),
        "bucket": (env.get("OFFSITE_MANAGED_BUCKET") or "").strip(),
        "region": (env.get("OFFSITE_MANAGED_REGION") or "us-east-1").strip(),
        "prefix": (env.get("OFFSITE_MANAGED_PREFIX") or f"barenoc-{site}").strip().rstrip("/"),
        "access_key": (env.get("OFFSITE_MANAGED_ACCESS_KEY") or "").strip(),
        "secret": (env.get("OFFSITE_MANAGED_SECRET_KEY") or "").strip(),
    }


# ── Status record (written by the job, read by the Backups UI) ──────────────

def read_offsite_status() -> dict:
    try:
        with open(OFFSITE_STATUS_JSON) as f:
            return json.load(f)
    except Exception:
        return {}


def write_offsite_status(status: dict):
    os.makedirs(os.path.dirname(OFFSITE_STATUS_JSON), exist_ok=True)
    with open(OFFSITE_STATUS_JSON, "w") as f:
        json.dump(status, f)
    try:
        os.chmod(OFFSITE_STATUS_JSON, 0o644)
    except Exception:
        pass


def next_run(cfg: dict, now: datetime.datetime | None = None) -> str:
    """ISO timestamp of the next offsite run (daily/weekly at OFF_SITE_HOUR)."""
    now = now or datetime.datetime.now()
    hour = _int(cfg.get("OFF_SITE_HOUR", "3"), 3)
    day = (cfg.get("OFF_SITE_DAY") or "daily").strip().lower()
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    for _ in range(8):  # at most one week ahead
        if candidate > now and _day_matches(candidate, day):
            return candidate.isoformat(timespec="minutes")
        candidate += datetime.timedelta(days=1)
    return candidate.isoformat(timespec="minutes")


def _day_matches(dt: datetime.datetime, day: str) -> bool:
    if day in ("", "daily"):
        return True
    # USB schedule convention: 0=Sunday … 6=Saturday
    weekday = (dt.weekday() + 1) % 7
    return day == str(weekday)


def should_run_now(cfg: dict, now: datetime.datetime | None = None) -> bool:
    """Schedule gate for the hourly host cron (cheap; 23/24 exits do nothing)."""
    now = now or datetime.datetime.now()
    hour = _int(cfg.get("OFF_SITE_HOUR", "3"), 3)
    day = (cfg.get("OFF_SITE_DAY") or "daily").strip().lower()
    return now.hour == hour and _day_matches(now, day)


def _int(value: str, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


# ── AWS Signature V4 (stdlib only — no boto3 bloat) ─────────────────────────

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _hmac_hex(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    k = _hmac(k, region)
    k = _hmac(k, SERVICE)
    return _hmac(k, "aws4_request")


def _uri_encode_path(path: str) -> str:
    """URI-encode each path segment, preserving '/' separators."""
    if not path.startswith("/"):
        path = "/" + path
    return "/".join(urllib.parse.quote(seg, safe="~-_.!*'()") for seg in path.split("/"))


def _canonical_query(params: dict) -> str:
    items = []
    for k in sorted(params):
        v = params[k]
        items.append(f"{urllib.parse.quote(str(k), safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}")
    return "&".join(items)


def _canonical_headers(headers: dict) -> tuple:
    norm = {}
    for k, v in headers.items():
        norm[k.lower()] = " ".join(str(v).strip().split())
    names = sorted(norm)
    canon = "".join(f"{n}:{norm[n]}\n" for n in names)
    return canon, ";".join(names)


def sign_request(method: str, url: str, region: str, access_key: str, secret_key: str,
                 payload: bytes = b"", headers: dict | None = None,
                 query: dict | None = None) -> dict:
    """Return signed request headers for an S3-compatible (path-style) call."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    path = parts.path or "/"
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = _sha256_hex(payload)
    hdrs = dict(headers or {})
    hdrs["host"] = host
    hdrs["x-amz-content-sha256"] = payload_hash
    hdrs["x-amz-date"] = amz_date
    canon_headers, signed_headers = _canonical_headers(hdrs)
    canon_query = _canonical_query(query or {})
    canonical_request = "\n".join([
        method.upper(), _uri_encode_path(path), canon_query,
        canon_headers, signed_headers, payload_hash,
    ])
    scope = f"{date_stamp}/{region}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        ALGORITHM, amz_date, scope, _sha256_hex(canonical_request.encode("utf-8")),
    ])
    signature = _hmac_hex(_signing_key(secret_key, date_stamp, region), string_to_sign)
    auth = (f"{ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
    out = dict(hdrs)
    out["Authorization"] = auth
    return out


def s3_request(method: str, url: str, region: str, access_key: str, secret_key: str,
               payload: bytes = b"", query: dict | None = None,
               extra_headers: dict | None = None, timeout: int = 60) -> tuple:
    """Perform a signed request. Returns (status_code, body_bytes, headers)."""
    if query:
        sep = "&" if "?" in url else "?"
        url = url + sep + _canonical_query(query)
    signed = sign_request(method, url, region, access_key, secret_key,
                          payload=payload, headers=extra_headers, query=query)
    req = urllib.request.Request(
        url, data=payload if method.upper() in ("PUT", "POST") else None,
        method=method.upper())
    for k, v in signed.items():
        req.add_header(k, v)
    if payload and not any(h.lower() == "content-type" for h in signed):
        req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class S3Client:
    """Minimal path-style S3-compatible client (MinIO/R2/B2/Synology)."""

    def __init__(self, endpoint: str, region: str, access_key: str, secret_key: str):
        e = (endpoint or "").strip().rstrip("/")
        if not e:
            raise ValueError("empty S3 endpoint")
        if not e.startswith(("http://", "https://")):
            e = "https://" + e
        parts = urllib.parse.urlsplit(e)
        if parts.path and parts.path != "/":
            raise ValueError("endpoint must be host[:port] only (no path)")
        self.scheme = parts.scheme
        self.host = parts.netloc
        self.region = region or "us-east-1"
        self.access_key = access_key
        self.secret_key = secret_key

    def _url(self, bucket: str, key: str = "") -> str:
        return f"{self.scheme}://{self.host}/{bucket}/{key}"

    def put_object(self, bucket: str, key: str, data: bytes) -> tuple:
        return s3_request("PUT", self._url(bucket, key), self.region,
                          self.access_key, self.secret_key, payload=data)

    def get_object(self, bucket: str, key: str) -> tuple:
        return s3_request("GET", self._url(bucket, key), self.region,
                          self.access_key, self.secret_key)

    def head_object(self, bucket: str, key: str) -> tuple:
        return s3_request("HEAD", self._url(bucket, key), self.region,
                          self.access_key, self.secret_key)

    def delete_object(self, bucket: str, key: str) -> tuple:
        return s3_request("DELETE", self._url(bucket, key), self.region,
                          self.access_key, self.secret_key)

    def list_objects(self, bucket: str, prefix: str = "", max_keys: int = 1000) -> tuple:
        return s3_request("GET", self._url(bucket), self.region,
                          self.access_key, self.secret_key,
                          query={"list-type": "2", "prefix": prefix,
                                 "max-keys": str(max_keys)})


# ── Target resolution (managed vs BYO — same code path, different config) ───

class OffsiteError(Exception):
    """User-facing offsite error (message lands in the status record + UI)."""


def resolve_target(cfg: dict, env: dict | None = None) -> dict:
    """Resolve the upload target for the configured mode.

    Raises OffsiteError when the mode is gated/not-configured. Returns a dict
    the S3Client + job consume: {mode, endpoint, bucket, region, prefix,
    access_key, secret}.
    """
    mode = (cfg.get("OFF_SITE_MODE") or "off").strip().lower()
    if mode == "managed":
        plan = verify_plan_key(cfg.get("PLAN_KEY") or "")
        if not plan["valid"]:
            raise OffsiteError("Managed remote backup requires a valid plan key "
                               "(subscription) — enter it in Settings → Backups.")
        prof = managed_profile(env)
        if not (prof["endpoint"] and prof["bucket"] and prof["access_key"] and prof["secret"]):
            raise OffsiteError("Managed backend not provisioned on this box yet "
                               "(gate-side setup pending).")
        return {"mode": "managed", **prof}
    if mode == "byo":
        creds = read_offsite_credentials()
        endpoint = (cfg.get("BYO_ENDPOINT") or "").strip()
        bucket = (cfg.get("BYO_BUCKET") or "").strip()
        region = (cfg.get("BYO_REGION") or "us-east-1").strip()
        prefix = (cfg.get("BYO_PREFIX") or "").strip().rstrip("/")
        if not (endpoint and bucket and creds["access_key"] and creds["secret"]):
            raise OffsiteError("BYO storage is incomplete — fill in the endpoint, "
                               "bucket and access key/secret.")
        return {"mode": "byo", "endpoint": endpoint, "bucket": bucket,
                "region": region, "prefix": prefix,
                "access_key": creds["access_key"], "secret": creds["secret"]}
    raise OffsiteError("Offsite backup is off.")


# ── The offsite job (encrypt → upload → prune → status) ─────────────────────

def latest_archive() -> str:
    """Path of the newest app-data archive (pointer file, then glob fallback)."""
    try:
        with open(LATEST_ARCHIVE_POINTER) as f:
            p = f.read().strip()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    candidates = sorted(glob.glob(os.path.join(BACKUP_DIR, "app-backup-*.tar.gz")))
    return candidates[-1] if candidates else ""


def _object_key(prefix: str, enc_name: str) -> str:
    prefix = (prefix or "").strip().rstrip("/")
    return f"{prefix}/{enc_name}" if prefix else enc_name


def run_offsite_backup(force: bool = False, now: datetime.datetime | None = None,
                       env: dict | None = None) -> int:
    """Full offsite run. Returns 0 on success/off, 1 on failure.

    force=True (the UI "Run now") bypasses the schedule gate. Never raises —
    every failure is captured in the status record the Backups UI shows.
    """
    now = now or datetime.datetime.now()
    cfg = read_offsite_conf()
    mode = (cfg.get("OFF_SITE_MODE") or "off").strip().lower()
    status = read_offsite_status()
    status.update({"mode": mode, "enabled": mode != "off",
                   "next_run": next_run(cfg, now)})

    if mode == "off":
        write_offsite_status(status)
        return 0
    if not force and not should_run_now(cfg, now):
        write_offsite_status(status)
        return 0

    try:
        target = resolve_target(cfg, env)
    except OffsiteError as e:
        status.update({"last_failed": now.isoformat(timespec="seconds"),
                       "last_error": str(e)})
        write_offsite_status(status)
        return 1

    src = latest_archive()
    if not src:
        status.update({"last_failed": now.isoformat(timespec="seconds"),
                       "last_error": "no app-data archive to upload yet "
                                     "(the 6 h local backup creates it)"})
        write_offsite_status(status)
        return 1

    enc_name = f"offsite-{now.strftime('%Y%m%d-%H%M%S')}.enc"
    enc_path = os.path.join(OFFSITE_STORE, enc_name)
    try:
        ensure_dek()
        size = encrypt_file_to(src, enc_path)
        with open(enc_path, "rb") as f:
            data = f.read()

        client = S3Client(target["endpoint"], target["region"],
                          target["access_key"], target["secret"])
        key = _object_key(target["prefix"], enc_name)
        code, body, _ = client.put_object(target["bucket"], key, data)
        if not (200 <= code < 300):
            raise OffsiteError(f"upload failed (HTTP {code}): {_snippet(body)}")

        # Prune remote objects older than retention (managed default 30d).
        retention = _int(cfg.get("OFF_SITE_RETENTION_DAYS", "30"), 30)
        _prune_remote(client, target["bucket"], target["prefix"], retention, now)
        _prune_local(keep=2)

        status.update({
            "last_ok": now.isoformat(timespec="seconds"),
            "last_failed": None,
            "last_error": "",
            "last_size_bytes": size,
            "object_key": key,
            "uploaded_at": now.isoformat(timespec="seconds"),
        })
        write_offsite_status(status)
        return 0
    except Exception as e:  # noqa: BLE001 — the status record is the UI surface
        if os.path.exists(enc_path):
            try:
                os.remove(enc_path)
            except OSError:
                pass
        status.update({"last_failed": now.isoformat(timespec="seconds"),
                       "last_error": str(e)[:400]})
        write_offsite_status(status)
        return 1


def _snippet(body: bytes) -> str:
    return (body or b"").decode("utf-8", "replace")[:200]


def _prune_local(keep: int):
    files = sorted(glob.glob(os.path.join(OFFSITE_STORE, "offsite-*.enc")))
    for f in files[:-keep] if keep else files:
        try:
            os.remove(f)
        except OSError:
            pass


def _prune_remote(client: S3Client, bucket: str, prefix: str, retention_days: int,
                  now: datetime.datetime):
    """Delete remote objects older than the retention window (best-effort)."""
    if retention_days <= 0:
        return
    code, body, _ = client.list_objects(bucket, prefix)
    if code != 200:
        return
    try:
        doc = json.loads(body)
    except Exception:
        return
    cutoff = now - datetime.timedelta(days=retention_days)
    for obj in doc.get("Contents", []):
        key = obj.get("Key", "")
        if not key.endswith(".enc"):
            continue
        try:
            ts = datetime.datetime.fromisoformat(
                obj.get("LastModified", "").replace("Z", "+00:00"))
        except Exception:
            continue
        if ts < cutoff:
            client.delete_object(bucket, key)


def list_local_encrypted() -> list:
    """Newest-first list of local encrypted archives (for the restore UI)."""
    return sorted(glob.glob(os.path.join(OFFSITE_STORE, "offsite-*.enc")), reverse=True)
