"""Settings — system config, API keys, email, integrations."""

import os
import json
import time
import shlex
import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from models import User
from auth import get_current_user, require_role, require_any_role
from llm_providers import load_providers, probe_provider
from audit import log_event
from database import get_db

logger = logging.getLogger("barenoc.settings")

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

BRANDING_DIR = "/opt/barenoc/volumes/branding"
MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB

# Allowed logo types: (mime prefix, extension)
LOGO_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}

# Sections and their env keys (redacted by default)
SECTIONS = {
    "general": {
        "fields": {
            "site_id": "SITE_ID",
            "customer_name": "CUSTOMER_NAME",
            "timezone": "TZ",
            "device_groups": "DEVICE_GROUPS",
            "logo": "BRANDING_LOGO",
            "queue_manager_name": "BOT_QUEUE_MANAGER_NAME",
            "assistant_name": "BOT_ASSISTANT_NAME",
            "discovery_subnets": "DISCOVERY_SUBNETS",
            "runner_concurrency": "MAX_CONCURRENT",
        },
        "redact": set(),
    },
    "tickets": {
        "fields": {},   # handled explicitly (typed per-priority config)
        "redact": set(),
    },
    "backups": {
        "fields": {},   # handled explicitly (conf-file-backed; the Proxmox host
                         # syncs the schedule from the VM every 10 min)
        "redact": set(),
    },
    "email": {
        "fields": {
            "smtp_host": "SMTP_HOST",
            "smtp_port": "SMTP_PORT",
            "smtp_user": "SMTP_USER",
            "alert_email": "ALERT_EMAIL",
            "alert_recipients": "ALERT_RECIPIENTS",
            "digest_recipients": "DIGEST_RECIPIENTS",
            "eod_recipients": "EOD_RECIPIENTS",
            "report_morning_digest": "REPORT_MORNING_DIGEST",
            "report_eod_summary": "REPORT_EOD_SUMMARY",
            "digest_hour": "DIGEST_HOUR",
            "eod_hour": "EOD_HOUR",
            "google_client_id": "GOOGLE_CLIENT_ID",
            "google_sender": "GOOGLE_SENDER",
        },
        "redact": {"smtp_password", "google_client_secret", "google_refresh_token"},
    },
    "unifi": {
        "fields": {
            "url": "UNIFI_URL",
            "username": "UNIFI_USER",
            "autosync_enabled": "UNIFI_AUTOSYNC_ENABLED",
            "autosync_interval": "UNIFI_AUTOSYNC_INTERVAL_MIN",
            "auto_adopt": "UNIFI_AUTO_ADOPT",
        },
        "redact": {"password"},
    },
    "identity": {
        "fields": {
            "enabled": "OIDC_ENABLED",
            "provider_url": "OIDC_PROVIDER_URL",
            "client_id": "OIDC_CLIENT_ID",
            "group_admin": "OIDC_GROUP_ADMIN",
            "group_operator": "OIDC_GROUP_OPERATOR",
            # Appliance identity (drives nginx, Pocket ID origin, the DNS
            # service, and step-ca) — set at install; changing requires a
            # redeploy + passkey re-enrollment (WebAuthn RP ID)
            "appliance_ip": "APPLIANCE_IP",
            "appliance_domain": "APPLIANCE_DOMAIN",
            "appliance_host": "APPLIANCE_HOST",
            # GitHub + Google OAuth login (config only for now — flows are
            # future work; toggles ship disabled until credentials exist)
            "github_enabled": "GITHUB_LOGIN_ENABLED",
            "github_client_id": "GITHUB_CLIENT_ID",
            "google_enabled": "GOOGLE_LOGIN_ENABLED",
            "google_client_id": "GOOGLE_LOGIN_CLIENT_ID",
        },
        "redact": {"client_secret", "github_client_secret", "google_client_secret"},
    },
    "policy": {
        "fields": {
            "profile": "LLM_POLICY_PROFILE",
            "risk_filters": "LLM_POLICY_RISK_FILTERS",
            "judge_required": "LLM_POLICY_JUDGE_REQUIRED",
            "write_autoexec": "LLM_POLICY_WRITE_AUTOEXEC",
            "autoexec_threshold": "LLM_POLICY_AUTOEXEC_THRESHOLD",
            "approval_priorities": "LLM_POLICY_APPROVAL_PRIORITIES",
            "patch_allowlist": "PATCH_ALLOWLIST",
            "llm_retry_interval_min": "LLM_RETRY_INTERVAL_MIN",
            "llm_retry_max_attempts": "LLM_RETRY_MAX_ATTEMPTS",
        },
        "redact": set(),
    },
    "restrictions": {
        "fields": {
            "block_actions": "RESTRICT_ACTIONS",
            "block_devices": "RESTRICT_DEVICES",
            "block_patterns": "RESTRICT_PATTERNS",
        },
        "redact": set(),
    },
}


# Field names whose VALUES must never land in the audit log (names are fine)
_SECRET_FIELDS = {
    "email": {"smtp_password", "google_client_secret", "google_refresh_token"},
    "unifi": {"password", "api_key"},
    "identity": {"client_secret", "github_client_secret", "google_client_secret"},
}

# Fields that need typed handling (bool/int) in update_section — never raw-string writes
_TYPED_FIELDS = {
    "unifi": {"autosync_enabled", "autosync_interval", "auto_adopt"},
    "email": {"report_morning_digest", "report_eod_summary", "digest_hour", "eod_hour"},
    "general": {"queue_manager_name", "assistant_name"},
    "policy": {"profile", "risk_filters", "judge_required", "write_autoexec",
                "autoexec_threshold", "approval_priorities", "patch_allowlist",
                "llm_retry_interval_min", "llm_retry_max_attempts"},
}

# Policy validation constants
POLICY_PROFILES = {"", "autonomous", "balanced", "strict"}
POLICY_RISK_CATEGORIES = {"maintenance", "security", "network", "identity", "install", "change"}
POLICY_PRIORITIES = {"P1", "P2", "P3", "P4"}
PATCH_ALLOWLIST_DEFAULTS = ["FW-6.6.55", "FW-6.6.52", "FW-7.0.1", "UBI-OS-5.1.20"]

# Effective-value presets for the GET (mirror of worker/policy.py PROFILES)
_POLICY_PROFILE_DEFAULTS = {
    "autonomous": {"risk_filters": "none", "judge_required": "true", "write_autoexec": "true",
                    "autoexec_threshold": "0.80", "approval_priorities": ""},
    "balanced":   {"risk_filters": "all", "judge_required": "true", "write_autoexec": "true",
                    "autoexec_threshold": "0.90", "approval_priorities": "P1"},
    "strict":     {"risk_filters": "all", "judge_required": "true", "write_autoexec": "false",
                    "autoexec_threshold": "0.80", "approval_priorities": "P1,P2"},
}


# ── Backups (host-side schedule; the Proxmox host syncs this file every 10 min) ──
BACKUP_CONF = "/opt/barenoc/volumes/backup_status/backup_schedule.conf"
BACKUP_STATUS_JSON = "/opt/barenoc/volumes/backup_status/status.json"

# Network-storage (NAS) backup target — BareNOC mounts the share itself so the
# user never needs root. The api container has docker.sock (root in its own
# container); host mounts are made through a short-lived PRIVILEGED container
# that enters the host mount namespace (nsenter -t 1 -m).
NET_MOUNT_POINT = "/opt/barenoc/backups/network"
NET_CREDS_FILE = "/opt/barenoc/volumes/backup_status/net-credentials"


def _host_run(script: str, timeout: int = 120) -> tuple:
    """Run a shell script in the HOST's mount namespace via a throwaway
    privileged container (docker.sock). Returns (exit_code, output).
    The caller builds the script with shlex.quote() around any variable parts."""
    import httpx
    import time as _t
    body = {
        "Image": "barenoc-api",
        "Cmd": ["sh", "-c", script],
        "HostConfig": {"Privileged": True, "PidMode": "host",
                       "Binds": ["/:/host", "/lib/modules:/lib/modules:ro"]},
    }
    name = "barenoc-net-helper"
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds="/var/run/docker.sock"),
                          timeout=timeout + 10) as c:
            try:
                old = c.get(f"http://docker/containers/{name}/json")
                if old.status_code == 200:
                    c.delete(f"http://docker/containers/{name}")
            except httpx.HTTPError:
                pass
            r = c.post(f"http://docker/containers/create?name={name}", json=body)
            if r.status_code != 201:
                return (1, f"docker create failed: {r.status_code} {r.text[:200]}")
            cid = r.json()["Id"]
            c.post(f"http://docker/containers/{cid}/start").raise_for_status()
            st = {}
            for _ in range(timeout):
                try:
                    st = c.get(f"http://docker/containers/{cid}/json").json()
                except httpx.HTTPError:
                    break  # removed — treat as finished
                if not st.get("State", {}).get("Running"):
                    break
                _t.sleep(1)
            try:
                logs = c.get(f"http://docker/containers/{cid}/logs?stdout=1&stderr=1").text
            except httpx.HTTPError:
                logs = ""
            try:
                c.delete(f"http://docker/containers/{cid}")
            except httpx.HTTPError:
                pass
            return (st.get("State", {}).get("ExitCode", -1), logs.strip())
    except httpx.HTTPError as e:
        return (1, f"docker socket: {e}")


def _net_mounted() -> bool:
    """True when the network backup share is mounted (checked host-side — the
    api container's own mount namespace doesn't propagate host mounts)."""
    try:
        code, out = _host_run(
            f"nsenter -t 1 -m -- awk '$2==\"{NET_MOUNT_POINT}\" {{print \"mounted\"}}' /proc/mounts")
        return code == 0 and "mounted" in out
    except Exception:
        return False


def _net_mount(proto: str, host: str, share: str) -> tuple:
    """Mount the NAS share at NET_MOUNT_POINT in the host namespace.
    Returns (exit_code, output). The credentials file must already exist."""
    mkdir = f"mkdir -p {shlex.quote('/host' + NET_MOUNT_POINT)}"
    if proto == "nfs":
        src = shlex.quote(f"{host}:/{share.strip('/')}")
        opts = "vers=4,nofail,noatime"
    else:  # cifs
        src = shlex.quote(f"//{host}/{share.strip('/')}")
        creds = shlex.quote(NET_CREDS_FILE)
        opts = f"credentials={creds},uid=1000,gid=988,nofail,iocharset=utf8"
    script = (f"{mkdir} && nsenter -t 1 -m -- mount -t {proto} {src} "
              f"{shlex.quote(NET_MOUNT_POINT)} -o {opts}")
    return _host_run(script)


def _net_unmount() -> tuple:
    return _host_run(f"nsenter -t 1 -m -- umount {shlex.quote(NET_MOUNT_POINT)}")


def _remount_net_backup():
    """Best-effort reconnect of the configured NAS share at api startup
    (mounts don't survive a reboot; the api brings them back)."""
    try:
        cfg = _read_backup_conf()
        proto = (cfg.get("NET_PROTO") or "").strip().lower()
        host = (cfg.get("NET_HOST") or "").strip()
        share = (cfg.get("NET_SHARE") or "").strip()
        if not (proto and host and share) or _net_mounted():
            return
        code, out = _net_mount(proto, host, share)
        if code == 0:
            logger.info("NAS backup share reconnected (%s %s/%s)", proto, host, share)
        else:
            logger.warning("NAS backup share reconnect failed: %s", out.strip()[-200:])
    except Exception as e:  # never block startup on a mount issue
        logger.warning("NAS backup reconnect skipped: %s", e)


def _net_write_creds(user: str, password: str):
    """SMB credentials for the kernel mount — 0600, never in the 0644 conf."""
    os.makedirs(os.path.dirname(NET_CREDS_FILE), exist_ok=True)
    fd = os.open(NET_CREDS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        if user:
            f.write(f"username={user}\n")
        if password:
            f.write(f"password={password}\n")
    os.chmod(NET_CREDS_FILE, 0o600)


def _read_backup_conf() -> dict:
    """Read the backup-schedule conf the host cron poller consumes."""
    cfg = {}
    try:
        with open(BACKUP_CONF) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


def _write_backup_conf(cfg: dict):
    os.makedirs(os.path.dirname(BACKUP_CONF), exist_ok=True)
    lines = [
        "# BareNOC backup schedule (written by Settings → Backups; the Proxmox",
        "# host's sync-backup-schedule cron reconciles its own cron from this file)",
    ]
    for k in ("USB_BACKUP_ENABLED", "USB_BACKUP_DAY", "USB_BACKUP_HOUR",
              "RUN_USB_BACKUP_NOW", "BACKUP_TARGET_DIR"):
        lines.append(f"{k}={cfg.get(k, '')}")
    with open(BACKUP_CONF, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(BACKUP_CONF, 0o644)   # barenoc (host poller) reads it
    except Exception:
        pass


def _appliance_host(status: dict) -> bool:
    """True when a Proxmox host is actively pushing backup status — i.e. this
    is the shipped appliance. On bring-your-own deployments the status file
    never appears/updates (the host's update-backup-status.sh writes it after
    every backup). Uses a 48 h staleness window (the daily 1 AM backup keeps
    it fresh on the appliance).
    """
    from datetime import datetime, timezone
    updated = (status.get("updated") or "").strip()
    if not updated:
        return False
    try:
        ts = datetime.fromisoformat(updated)
    except Exception:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() < 48 * 3600


def _backups_section() -> dict:
    cfg = _read_backup_conf()
    enabled = (cfg.get("USB_BACKUP_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")
    day = (cfg.get("USB_BACKUP_DAY") or "3").strip() or "3"
    hour = _env_int(cfg.get("USB_BACKUP_HOUR", "2"), 2)
    status = {}
    try:
        with open(BACKUP_STATUS_JSON) as f:
            status = json.load(f)
    except Exception:
        pass
    return {
        "appliance_host": _appliance_host(status),
        "usb_backup_enabled": enabled,
        "usb_backup_day": day,
        "usb_backup_hour": hour,
        "usb_present": status.get("usb_present") is True,
        "usb_encrypted": status.get("usb_encrypted") is True,
        "usb_keyslots": status.get("usb_keyslots") or 0,
        "usb_last_backup": status.get("usb_last_backup") or "none",
        "vm_snapshot_last": status.get("vm_snapshot_last") or "none",
        "status_updated": status.get("updated") or "",
        "backup_target_dir": cfg.get("BACKUP_TARGET_DIR") or "",
        "net_proto": (cfg.get("NET_PROTO") or "").strip().lower(),
        "net_host": cfg.get("NET_HOST") or "",
        "net_share": cfg.get("NET_SHARE") or "",
        "net_mounted": _net_mounted(),
    }


def _read_env_file() -> dict:
    """Read .env into a dict, falling back to process env."""
    env = {}
    path = "/opt/barenoc/.env"
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    except Exception:
        pass
    # Fall back to process environment (env_file injected these)
    if not env:
        for key, value in os.environ.items():
            if key in ("SITE_ID", "CUSTOMER_NAME", "TZ", "DEVICE_GROUPS",
                       "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
                       "ALERT_EMAIL", "UNIFI_URL", "UNIFI_USER", "BRANDING_LOGO",
                       "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "GOOGLE_SENDER"):
                env[key] = value
    return env


def _write_env_file(env: dict):
    """Write dict back to .env preserving comments."""
    path = "/opt/barenoc/.env"
    lines_out = []
    try:
        with open(path) as f:
            lines = f.readlines()
    except Exception:
        lines = []
    env = dict(env)
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines_out.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in env:
                lines_out.append(f"{key}={env.pop(key)}\n")
            # else: key was deliberately removed (e.g. logo deleted) — drop the line
            continue
        lines_out.append(line)
    # Append any remaining new keys
    for key, value in env.items():
        lines_out.append(f"{key}={value}\n")
    with open(path, "w") as f:
        f.writelines(lines_out)


def _api_key_status():
    """Return API key presence status for all integrations."""
    env = _read_env_file()
    llm_configured = any(p.get("api_key") for p in load_providers(env).values())
    return {
        "llm": llm_configured,
        "smtp": bool(env.get("SMTP_PASSWORD")),
        "gmail": bool(env.get("GOOGLE_REFRESH_TOKEN") and env.get("GOOGLE_CLIENT_ID")),
        "unifi": bool(env.get("UNIFI_PASSWORD")),
    }


PROVIDER_SECRET_FILE = "/opt/barenoc/volumes/secrets/llm_provider.json"


def _write_provider_secret():
    """Keep the pi-agent provider secret (key/model) in sync with Settings.
    Written whenever the LLM config changes (and on startup); pi-agent reads it
    so the on-appliance coding agent uses the same keys as everything else."""
    try:
        env = _read_env_file()
        active = (env.get("LLM_ACTIVE_PROVIDER", "") or "").strip().lower()
        if not active:
            providers = load_providers(env)
            active = next(iter(providers), "")
        prefix = f"LLM_PROVIDER_{active.upper()}"
        ptype = (env.get(f"{prefix}_TYPE", "openai") or "openai").lower()
        base = (env.get(f"{prefix}_BASE_URL", "") or "").lower()
        model = env.get(f"{prefix}_CHAT_MODEL", "") or "deepseek-v4-flash"
        key = env.get(f"{prefix}_API_KEY", "")
        provider = ("deepseek" if ("deepseek" in base or active.startswith("deepseek"))
                    else ("anthropic" if ptype == "anthropic"
                          else ("google" if ptype == "gemini" else "openai")))
        payload = json.dumps({"provider": provider, "model": model, "api_key": key})
        # Optional on-LAN fallback (Ollama/LM Studio): the pi runner retries
        # with this when the primary cloud provider fails. Same file — it's
        # pi-agent-readable (0640 root:pi-agent).
        fenv = _read_env_file()
        fb_base = (fenv.get("LLM_PROVIDER_OLLAMA_BASE_URL", "") or "").strip()
        fb_model = (fenv.get("LLM_PROVIDER_OLLAMA_CHAT_MODEL", "")
                    or fenv.get("LLM_PROVIDER_OLLAMA_REASONER_MODEL", "") or "").strip()
        if fb_base and fb_model:
            cfg = json.loads(payload)
            cfg["fallback"] = {
                "provider": "ollama",
                "base_url": fb_base.rstrip("/") + "/v1",
                "model": fb_model,
                "api_key": "ollama",
            }
            payload = json.dumps(cfg)
        with open(PROVIDER_SECRET_FILE, "w") as f:
            f.write(payload)
        os.chmod(PROVIDER_SECRET_FILE, 0o640)
        # pi-agent must be able to read it (runner passes the key to pi). The
        # container's /etc/group has no pi-agent entry, so fall back to the
        # secrets dir's group (host-side setgid keeps it pi-agent-owned).
        try:
            import grp
            gid = grp.getgrnam("pi-agent").gr_gid
        except Exception:
            gid = None
        try:
            if gid is None:
                gid = os.stat(os.path.dirname(PROVIDER_SECRET_FILE)).st_gid
            os.chgrp(PROVIDER_SECRET_FILE, gid)
        except Exception:
            pass
    except Exception:
        pass


@router.get("/status")
def get_settings_status(user: User = Depends(require_role("admin"))):
    """Get which integrations are configured (presence only, no secrets)."""
    return _api_key_status()


TICKET_CHECKIN_DEFAULT_HOURS = {"P1": 1, "P2": 4, "P3": 24, "P4": 24}
TICKET_CLOSE_DEFAULT_DAYS = {"P1": 3, "P2": 3, "P3": 3, "P4": 3}


@router.get("/remote-access")
def remote_access(user: User = Depends(require_role("admin"))):
    """Live Tailscale state: the VM's own node (written by the VM's
    tailscale-status timer) + the Proxmox host's node (pushed by the host's
    cron via scp — same pattern as backup_status)."""
    def read(path: str) -> dict:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}

    vm = read("/opt/barenoc/volumes/remote_access/self.json")
    host_raw = read("/opt/barenoc/volumes/remote_access/host.json")
    host = {}
    if host_raw:
        host = {
            "online": bool(host_raw.get("Self", {}).get("Online")),
            "hostname": host_raw.get("Self", {}).get("HostName"),
            "tailscale_ip": (host_raw.get("TailscaleIPs") or [None])[0],
            "needs_login": host_raw.get("BackendState") in ("NeedsLogin", "LoggedOut"),
            "auth_url": host_raw.get("AuthURL", ""),
        }
    return {"vm": vm, "host": host}


@router.get("/appliance-identity")
def appliance_identity(user: User = Depends(require_any_role("admin", "agent"))):
    """The appliance's own identity — the agent service reads this for
    self-protection (pi-agent can't read the 0600 .env, so the API is the
    source of truth for "what is me" when the runner denies targets)."""
    env = _read_env_file()
    return {"ip": env.get("APPLIANCE_IP") or ""}


@router.get("/{section}")
def get_section(section: str, user: User = Depends(require_role("admin"))):
    """Get a settings section with secrets redacted."""
    if section == "llm":
        return _llm_section(_read_env_file())
    if section == "backups":
        return _backups_section()
    if section not in SECTIONS:
        raise HTTPException(status_code=404, detail="Unknown settings section")
    env = _read_env_file()
    section_def = SECTIONS[section]
    result = {}
    for field, env_key in section_def["fields"].items():
        result[field] = env.get(env_key, "")
    # Special handling for redacted secret fields
    if section == "email":
        result["smtp_password_configured"] = bool(env.get("SMTP_PASSWORD"))
        result["smtp_password"] = "••••••••" if result["smtp_password_configured"] else ""
        result["google_client_secret_configured"] = bool(env.get("GOOGLE_CLIENT_SECRET"))
        result["google_client_secret"] = "••••••••" if result["google_client_secret_configured"] else ""
        result["google_refresh_token_configured"] = bool(env.get("GOOGLE_REFRESH_TOKEN"))
        result["google_refresh_token"] = "••••••••" if result["google_refresh_token_configured"] else ""
        # Report config (typed)
        result["report_morning_digest"] = _env_bool(env.get("REPORT_MORNING_DIGEST", "true"))
        result["report_eod_summary"] = _env_bool(env.get("REPORT_EOD_SUMMARY", "true"))
        result["digest_hour"] = _env_int(env.get("DIGEST_HOUR", "7"), 7)
        result["eod_hour"] = _env_int(env.get("EOD_HOUR", "18"), 18)
    if section == "unifi":
        result["password_configured"] = bool(env.get("UNIFI_PASSWORD"))
        result["password"] = "••••••••" if result["password_configured"] else ""
        result["autosync_enabled"] = _env_bool(env.get("UNIFI_AUTOSYNC_ENABLED", ""))
        result["autosync_interval"] = _env_int(env.get("UNIFI_AUTOSYNC_INTERVAL_MIN", "5"), 5)
        result["auto_adopt"] = _env_bool(env.get("UNIFI_AUTO_ADOPT", "true") or "true")
    if section == "identity":
        result["client_secret_configured"] = bool(env.get("OIDC_CLIENT_SECRET"))
        result["client_secret"] = "••••••••" if result["client_secret_configured"] else ""
        result["github_client_secret_configured"] = bool(env.get("GITHUB_CLIENT_SECRET"))
        result["github_client_secret"] = "••••••••" if result["github_client_secret_configured"] else ""
        result["google_client_secret_configured"] = bool(env.get("GOOGLE_LOGIN_CLIENT_SECRET"))
        result["google_client_secret"] = "••••••••" if result["google_client_secret_configured"] else ""
        result["github_enabled"] = _env_bool(env.get("GITHUB_LOGIN_ENABLED", ""))
        result["google_enabled"] = _env_bool(env.get("GOOGLE_LOGIN_ENABLED", ""))
        # Appliance identity + DNS helper (defaults when unset)
        ip = result.get("appliance_ip") or "192.168.4.207"
        host = result.get("appliance_host") or "app.barenoc.com"
        domain = result.get("appliance_domain") or "barenoc.com"
        result["appliance_ip"] = ip
        result["appliance_host"] = host
        result["appliance_domain"] = domain
        # passkeys need a public-suffix registrable domain (no .local/.lan/etc.)
        tld = (domain.rsplit(".", 1)[-1] if "." in domain else domain).lower()
        private_tlds = {"local", "lan", "internal", "home", "corp", "home.arpa", "localhost"}
        result["passkey_viable"] = tld not in private_tlds
        result["passkey_warning"] = "" if result["passkey_viable"] else (
            f"'{tld}' is not a public-suffix domain — Chrome/Edge/Safari refuse "
            "passkeys on it. Use a real domain you own (it only needs to resolve "
            "inside your network).")
        result["dns_record"] = f"A {host} -> {ip}"
        result["hosts_lines"] = f"{ip} {host}"
    if section == "policy":
        # Effective values: explicit env override wins, else the profile's
        # preset (mirrors worker policy.py). Never inject a default that
        # contradicts an intentionally-empty save (approval priorities!).
        profile = (env.get("LLM_POLICY_PROFILE") or "").strip().lower()
        base = _POLICY_PROFILE_DEFAULTS.get(profile, {})

        def _effective(key: str, env_key: str, fallback):
            v = env.get(env_key)
            if v is not None and v != "":
                return v
            return base.get(key, fallback)

        result["judge_required"] = _env_bool(str(_effective(
            "judge_required", "LLM_POLICY_JUDGE_REQUIRED", False)))
        result["write_autoexec"] = _env_bool(str(_effective(
            "write_autoexec", "LLM_POLICY_WRITE_AUTOEXEC", False)))
        try:
            result["autoexec_threshold"] = float(_effective(
                "autoexec_threshold", "LLM_POLICY_AUTOEXEC_THRESHOLD", "0.80"))
        except ValueError:
            result["autoexec_threshold"] = 0.80
        result["risk_filters"] = _effective(
            "risk_filters", "LLM_POLICY_RISK_FILTERS", "all")
        result["approval_priorities"] = _effective(
            "approval_priorities", "LLM_POLICY_APPROVAL_PRIORITIES", "P1,P2")
        result["patch_allowlist"] = env.get("PATCH_ALLOWLIST", "") or ",".join(PATCH_ALLOWLIST_DEFAULTS)
        # LLM-call retries (worker schedules retries instead of failing instantly)
        result["llm_retry_interval_min"] = _env_int(env.get("LLM_RETRY_INTERVAL_MIN", "2"), 2)
        result["llm_retry_max_attempts"] = _env_int(env.get("LLM_RETRY_MAX_ATTEMPTS", "10"), 10)
    if section == "restrictions":
        # Hard deny caps (comma lists) — enforced by the worker even in
        # autonomous mode: actions never allowed, devices never acted on,
        # request phrases that get blocked outright.
        result["block_actions"] = env.get("RESTRICT_ACTIONS", "")
        result["block_devices"] = env.get("RESTRICT_DEVICES", "")
        result["block_patterns"] = env.get("RESTRICT_PATTERNS", "")
    if section == "general":
        result["chat_client_enabled"] = _env_bool(env.get("CHAT_CLIENT_ENABLED", "true"))
    if section == "tickets":
        # Ticket lifecycle: per-priority check-in interval (hours) + auto-close
        # (days after the AI resolved the ticket). 0 = never.
        result["checkin_enabled"] = _env_bool(env.get("TICKET_CHECKIN_ENABLED", "true"))
        result["checkin_email"] = _env_bool(env.get("TICKET_CHECKIN_EMAIL", "true"))
        result["autoclose_enabled"] = _env_bool(env.get("TICKET_AUTOCLOSE_ENABLED", "true"))
        result["checkin_hours"] = {
            p: _env_int(env.get(f"TICKET_CHECKIN_HOURS_{p}", str(TICKET_CHECKIN_DEFAULT_HOURS[p])),
                        TICKET_CHECKIN_DEFAULT_HOURS[p])
            for p in ("P1", "P2", "P3", "P4")}
        result["close_after_days"] = {
            p: _env_int(env.get(f"TICKET_CLOSE_AFTER_DAYS_{p}", str(TICKET_CLOSE_DEFAULT_DAYS[p])),
                        TICKET_CLOSE_DEFAULT_DAYS[p])
            for p in ("P1", "P2", "P3", "P4")}
    return result


@router.put("/{section}")
def update_section(section: str, config: dict, db: Session = Depends(get_db),
                   user: User = Depends(require_role("admin"))):
    """Update a settings section (audit-logged as settings_change)."""
    if section == "llm":
        return _update_llm(config, db, user)
    if section == "backups":
        return _update_backups(config, db, user)
    if section not in SECTIONS:
        raise HTTPException(status_code=404, detail="Unknown settings section")
    env = _read_env_file()
    section_def = SECTIONS[section]
    updated = 0
    changed = []
    values = {}

    # ── Ticket lifecycle (check-ins + auto-close, typed per priority) ──
    if section == "tickets":
        def _toggle(field: str, env_key: str):
            nonlocal updated
            if field in config:
                env[env_key] = "true" if _env_bool(str(config[field])) else "false"
                updated += 1
                changed.append(field)
                values[field] = env[env_key]

        def _int_field(field: str, env_key: str, lo: int, hi: int):
            nonlocal updated
            v = config.get(field)
            if v is None:
                return
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{field} must be a number")
            if not lo <= iv <= hi:
                raise HTTPException(status_code=400,
                                    detail=f"{field} must be between {lo} and {hi} (0 = never)")
            env[env_key] = str(iv)
            updated += 1
            changed.append(field)
            values[field] = str(iv)

        _toggle("checkin_enabled", "TICKET_CHECKIN_ENABLED")
        _toggle("checkin_email", "TICKET_CHECKIN_EMAIL")
        _toggle("autoclose_enabled", "TICKET_AUTOCLOSE_ENABLED")

        def _per_priority(container_key: str, env_prefix: str, lo: int, hi: int):
            nonlocal updated
            d = config.get(container_key)
            if not isinstance(d, dict):
                return
            for p in ("P1", "P2", "P3", "P4"):
                v = d.get(p)
                if v is None:
                    continue
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400,
                                        detail=f"{container_key}.{p} must be a number")
                if not lo <= iv <= hi:
                    raise HTTPException(status_code=400,
                                        detail=f"{container_key}.{p} must be between {lo} and {hi} (0 = never)")
                env[f"{env_prefix}_{p}"] = str(iv)
                updated += 1
                changed.append(f"{container_key}_{p}")
                values[f"{container_key}_{p}"] = str(iv)

        _per_priority("checkin_hours", "TICKET_CHECKIN_HOURS", 0, 168)
        _per_priority("close_after_days", "TICKET_CLOSE_AFTER_DAYS", 0, 90)
        if not changed:
            return {"status": "ok", "updated": 0}
        _write_env_file(env)
        log_event(db, "settings_change", user.username, {
            "section": "tickets", "fields": sorted(set(changed)), "values": values,
        })
        return {"status": "ok", "updated": updated}
    # Timezone must be a valid IANA zone (reject typos before writing .env)
    if section == "general" and config.get("timezone"):
        tz = str(config["timezone"]).strip()
        if tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)
            except Exception:
                raise HTTPException(status_code=400, detail=f"Unknown timezone '{tz}' — use an IANA name like America/New_York")
    # Bot names (Juniper = Queue Manager, Lily = AI assistant) — validated
    if section == "general":
        for field, env_key in (("queue_manager_name", "BOT_QUEUE_MANAGER_NAME"),
                               ("assistant_name", "BOT_ASSISTANT_NAME")):
            if field in config:
                name = str(config[field]).strip()
                if not (1 <= len(name) <= 40):
                    raise HTTPException(status_code=400, detail=f"{field} must be 1-40 characters")
                env[env_key] = name
                updated += 1
                changed.append(field)
                values[field] = name
        # Desktop chat client enable/disable (typed bool)
        if "chat_client_enabled" in config:
            val = "true" if _env_bool(str(config["chat_client_enabled"])) else "false"
            env["CHAT_CLIENT_ENABLED"] = val
            updated += 1
            changed.append("chat_client_enabled")
            values["chat_client_enabled"] = val
    # Fields needing typed handling (bool/int) — skip raw-string writes
    skip_generic = _TYPED_FIELDS
    for field, env_key in section_def["fields"].items():
        if field in config and field not in skip_generic.get(section, set()):
            env[env_key] = str(config[field])
            updated += 1
            changed.append(field)
            if field not in _SECRET_FIELDS.get(section, set()):
                values[field] = str(config[field])
    # Secret fields — only update if not masked; values never enter the audit log
    secret_map = {
        "email": {"smtp_password": "SMTP_PASSWORD",
                  "google_client_secret": "GOOGLE_CLIENT_SECRET",
                  "google_refresh_token": "GOOGLE_REFRESH_TOKEN"},
        "unifi": {"password": "UNIFI_PASSWORD"},
        "identity": {"client_secret": "OIDC_CLIENT_SECRET",
                     "github_client_secret": "GITHUB_CLIENT_SECRET",
                     "google_client_secret": "GOOGLE_LOGIN_CLIENT_SECRET"},
    }
    if section in secret_map:
        for field, env_key in secret_map[section].items():
            if field in config and config[field] and "••" not in str(config[field]):
                env[env_key] = str(config[field])
                updated += 1
                changed.append(field)
    if section == "unifi":
        # Auto-sync toggle + interval (typed values)
        if "autosync_enabled" in config:
            val = "true" if _env_bool(str(config["autosync_enabled"])) else "false"
            env["UNIFI_AUTOSYNC_ENABLED"] = val
            updated += 1
            changed.append("autosync_enabled")
            values["autosync_enabled"] = val
        if "autosync_interval" in config:
            try:
                iv = int(config["autosync_interval"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="autosync_interval must be minutes (number)")
            if iv not in (5, 10, 15, 30, 60):
                raise HTTPException(status_code=400, detail="autosync_interval must be 5, 10, 15, 30, or 60 minutes")
            env["UNIFI_AUTOSYNC_INTERVAL_MIN"] = str(iv)
            updated += 1
            changed.append("autosync_interval")
            values["autosync_interval"] = str(iv)
        if "auto_adopt" in config:
            val = "true" if _env_bool(str(config["auto_adopt"])) else "false"
            env["UNIFI_AUTO_ADOPT"] = val
            updated += 1
            changed.append("auto_adopt")
            values["auto_adopt"] = val
    if section == "email":
        # Report toggles + hours (typed values)
        if "report_morning_digest" in config:
            val = "true" if _env_bool(str(config["report_morning_digest"])) else "false"
            env["REPORT_MORNING_DIGEST"] = val
            updated += 1
            changed.append("report_morning_digest")
            values["report_morning_digest"] = val
        if "report_eod_summary" in config:
            val = "true" if _env_bool(str(config["report_eod_summary"])) else "false"
            env["REPORT_EOD_SUMMARY"] = val
            updated += 1
            changed.append("report_eod_summary")
            values["report_eod_summary"] = val
        for field, env_key, default in (("digest_hour", "DIGEST_HOUR", 7), ("eod_hour", "EOD_HOUR", 18)):
            if field in config:
                try:
                    h = int(config[field])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{field} must be an hour (0-23)")
                if not 0 <= h <= 23:
                    raise HTTPException(status_code=400, detail=f"{field} must be between 0 and 23")
                env[env_key] = str(h)
                updated += 1
                changed.append(field)
                values[field] = str(h)
    if section == "policy":
        # Autonomy policy (typed + validated before writing .env)
        def _bool_field(field, env_key):
            if field in config:
                val = "true" if _env_bool(str(config[field])) else "false"
                env[env_key] = val
                changed.append(field)
                values[field] = val

        if "profile" in config:
            profile = str(config["profile"]).strip().lower()
            if profile not in POLICY_PROFILES:
                raise HTTPException(status_code=400,
                                    detail="profile must be autonomous, balanced, strict, or empty")
            env["LLM_POLICY_PROFILE"] = profile
            changed.append("profile")
            values["profile"] = profile or "(inherit)"
        if "risk_filters" in config:
            rf = str(config["risk_filters"]).strip().lower()
            tokens = [t for t in rf.split(",") if t.strip()]
            if rf not in ("all", "none") and not all(t in POLICY_RISK_CATEGORIES for t in tokens):
                raise HTTPException(status_code=400,
                                    detail="risk_filters must be all, none, or a subset of "
                                           "maintenance,security,network,identity,install,change")
            env["LLM_POLICY_RISK_FILTERS"] = rf or "all"
            changed.append("risk_filters")
            values["risk_filters"] = rf or "all"
        _bool_field("judge_required", "LLM_POLICY_JUDGE_REQUIRED")
        _bool_field("write_autoexec", "LLM_POLICY_WRITE_AUTOEXEC")
        if "autoexec_threshold" in config:
            try:
                thr = float(config["autoexec_threshold"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="autoexec_threshold must be a number")
            if not 0.0 <= thr <= 1.0:
                raise HTTPException(status_code=400, detail="autoexec_threshold must be between 0 and 1")
            env["LLM_POLICY_AUTOEXEC_THRESHOLD"] = str(thr)
            changed.append("autoexec_threshold")
            values["autoexec_threshold"] = str(thr)
        if "approval_priorities" in config:
            raw = str(config["approval_priorities"]).strip().upper()
            prios = [p for p in raw.split(",") if p.strip()]
            if prios and not all(p in POLICY_PRIORITIES for p in prios):
                raise HTTPException(status_code=400, detail="approval_priorities must be P1-P4, comma-separated")
            env["LLM_POLICY_APPROVAL_PRIORITIES"] = ",".join(prios)
            changed.append("approval_priorities")
            values["approval_priorities"] = ",".join(prios) or "(none)"
        if "patch_allowlist" in config:
            raw = str(config["patch_allowlist"]).strip()
            if raw == "*":
                env["PATCH_ALLOWLIST"] = "*"
            else:
                patches = [p.strip() for p in raw.split(",") if p.strip()]
                env["PATCH_ALLOWLIST"] = ",".join(patches)
            changed.append("patch_allowlist")
            values["patch_allowlist"] = env["PATCH_ALLOWLIST"] or "(empty = defaults)"
        if "llm_retry_interval_min" in config:
            try:
                iv = int(config["llm_retry_interval_min"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="llm_retry_interval_min must be minutes (number)")
            if not 1 <= iv <= 120:
                raise HTTPException(status_code=400, detail="llm_retry_interval_min must be between 1 and 120 minutes")
            env["LLM_RETRY_INTERVAL_MIN"] = str(iv)
            changed.append("llm_retry_interval_min")
            values["llm_retry_interval_min"] = str(iv)
        if "llm_retry_max_attempts" in config:
            try:
                ma = int(config["llm_retry_max_attempts"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="llm_retry_max_attempts must be a number")
            if not 1 <= ma <= 100:
                raise HTTPException(status_code=400, detail="llm_retry_max_attempts must be between 1 and 100")
            env["LLM_RETRY_MAX_ATTEMPTS"] = str(ma)
            changed.append("llm_retry_max_attempts")
            values["llm_retry_max_attempts"] = str(ma)
        updated = len(changed)  # policy block counted via changed[]
    if not changed:
        return {"status": "ok", "updated": 0}
    _write_env_file(env)
    log_event(db, "settings_change", user.username, {
        "section": section,
        "fields": sorted(set(changed)),
        "values": values,
    })
    return {"status": "ok", "updated": updated}


def _env_bool(value: str) -> bool:
    """Parse a truthy env value ('1', 'true', 'yes', 'on')."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _env_int(value: str, default: int) -> int:
    """Parse an int env value, falling back to default."""
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


# ── LLM Providers ──

def _update_backups(config: dict, db: Session, user: User) -> dict:
    """Save the host backup schedule (Settings → Backups).

    Writes the conf the Proxmox host's sync-backup-schedule cron reconciles
    against every 10 minutes — no restart needed on either side.
    """
    cfg = _read_backup_conf()
    changed = []
    values = {}
    if "usb_backup_enabled" in config:
        val = "true" if _env_bool(str(config["usb_backup_enabled"])) else "false"
        cfg["USB_BACKUP_ENABLED"] = val
        changed.append("usb_backup_enabled")
        values["usb_backup_enabled"] = val
    if "usb_backup_day" in config:
        d = str(config["usb_backup_day"]).strip().lower()
        if d != "daily" and d not in ("0", "1", "2", "3", "4", "5", "6"):
            raise HTTPException(status_code=400,
                                detail="usb_backup_day must be 'daily' or 0-6 (0=Sunday)")
        cfg["USB_BACKUP_DAY"] = d
        changed.append("usb_backup_day")
        values["usb_backup_day"] = d
    if "usb_backup_hour" in config:
        try:
            h = int(config["usb_backup_hour"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="usb_backup_hour must be 0-23")
        if not 0 <= h <= 23:
            raise HTTPException(status_code=400, detail="usb_backup_hour must be 0-23")
        cfg["USB_BACKUP_HOUR"] = str(h)
        changed.append("usb_backup_hour")
        values["usb_backup_hour"] = str(h)
    if config.get("run_usb_backup_now") is True:
        cfg["RUN_USB_BACKUP_NOW"] = "true"
        changed.append("run_usb_backup_now")
        values["run_usb_backup_now"] = "true"
    elif changed:
        # a plain schedule edit cancels a pending run-now
        cfg["RUN_USB_BACKUP_NOW"] = "false"
    if "backup_target_dir" in config:
        t = str(config["backup_target_dir"]).strip()
        if t and not t.startswith("/"):
            raise HTTPException(status_code=400,
                                detail="backup_target_dir must be an absolute path (or empty to disable)")
        cfg["BACKUP_TARGET_DIR"] = t
        changed.append("backup_target_dir")
        values["backup_target_dir"] = t
    # NAS config (protocol/host/share; the password lives in the 0600 creds
    # file, never in the 0644 conf). net_pass is consumed here and dropped.
    if "net_proto" in config or "net_host" in config or "net_share" in config:
        proto = str(config.get("net_proto", cfg.get("NET_PROTO", ""))).strip().lower()
        host = str(config.get("net_host", cfg.get("NET_HOST", ""))).strip()
        share = str(config.get("net_share", cfg.get("NET_SHARE", ""))).strip()
        if proto and proto not in ("cifs", "nfs"):
            raise HTTPException(status_code=400, detail="net_proto must be cifs (SMB) or nfs")
        cfg["NET_PROTO"] = proto
        cfg["NET_HOST"] = host
        cfg["NET_SHARE"] = share
        changed.append("net_proto")
        values["net_proto"] = proto
    if "net_user" in config or "net_pass" in config:
        _net_write_creds(str(config.get("net_user", "")), str(config.get("net_pass", "")))
        changed.append("net_user")
        values["net_user"] = str(config.get("net_user", ""))
    if not changed:
        return {"status": "ok", "updated": 0}
    _write_backup_conf(cfg)
    log_event(db, "settings_change", user.username, {
        "section": "backups", "fields": sorted(set(changed)), "values": values,
    })
    return {"status": "ok", "updated": len(set(changed))}


@router.post("/backups/net-mount")
def backup_net_mount(config: dict, user: User = Depends(require_role("admin"))):
    """Save NAS config + mount the share (no root needed by the user)."""
    proto = str(config.get("proto", "")).strip().lower()
    host = str(config.get("host", "")).strip()
    share = str(config.get("share", "")).strip()
    net_user = str(config.get("user", "")).strip()
    net_pass = str(config.get("pass", ""))
    if proto not in ("cifs", "nfs"):
        raise HTTPException(status_code=400, detail="Pick SMB (most home NAS) or NFS")
    if not host or not share:
        raise HTTPException(status_code=400, detail="Enter the NAS host and share name")
    if proto == "cifs" and not net_user:
        # guest access is a legitimate home-NAS option
        pass
    # persist (creds 0600, conf keeps proto/host/share + target dir)
    _net_write_creds(net_user, net_pass)
    cfg = _read_backup_conf()
    cfg.update({"NET_PROTO": proto, "NET_HOST": host, "NET_SHARE": share,
                "BACKUP_TARGET_DIR": NET_MOUNT_POINT})
    _write_backup_conf(cfg)
    log_event(db, "settings_change", user.username, {
        "section": "backups-net", "fields": ["net_proto", "net_host", "net_share"],
        "values": {"net_proto": proto, "net_host": host, "net_share": share},
    })
    # mount (unmount anything stale at the point first)
    if _net_mounted():
        _net_unmount()
    code, out = _net_mount(proto, host, share)
    if code != 0:
        detail = (out or "mount failed").strip()[-250:]
        raise HTTPException(status_code=502, detail=f"Couldn't mount the share: {detail}")
    return {"status": "ok", "mounted": True,
            "detail": f"✅ Connected to {host} ({proto}). Every 6 h the app-data archive is copied there (kept 30 days)."}


@router.post("/backups/net-unmount")
def backup_net_unmount(user: User = Depends(require_role("admin"))):
    """Disconnect the NAS share (config is kept for reconnect)."""
    if not _net_mounted():
        return {"status": "ok", "mounted": False, "detail": "Already disconnected."}
    code, out = _net_unmount()
    if code != 0:
        raise HTTPException(status_code=502, detail=f"Couldn't unmount: {(out or '').strip()[-200:]}")
    return {"status": "ok", "mounted": False,
            "detail": "Disconnected. The share reconnects when you click Connect, or after a reboot (appliance start)."}


@router.post("/backups/test-target")
def test_backup_target(target: dict, user: User = Depends(require_role("admin"))):
    """Check a network backup folder (mounted share) before trusting it.

    The api container sees /opt/barenoc/backups (bind-mounted), so shares
    mounted under it are visible + testable directly. Checks: exists /
    writable / on a separate filesystem (a real mount)."""
    path = str(target.get("dir", "")).strip().rstrip("/") or "/"
    if not path.startswith("/"):
        raise HTTPException(status_code=400,
                            detail="Path must be absolute (e.g. /opt/barenoc/backups/network)")
    if not os.path.isdir(path):
        return {"status": "error",
                "detail": "Folder not visible from the app — mount your share first (try "
                           "/opt/barenoc/backups/network), then re-test. Mount one-liners: "
                           "Backups wiki."}
    writable = os.access(path, os.W_OK)
    mounted = False
    try:
        dev = os.stat(path).st_dev
        pdev = os.stat(os.path.dirname(path)).st_dev
        mounted = dev != pdev
    except Exception:
        pass
    if not writable:
        return {"status": "error",
                "detail": "Folder exists but is not writable — check the share permissions "
                           "(the backup runs as the appliance user)."}
    note = " on a separate filesystem (a real mount)" if mounted \
        else " on the same disk — mount a share for true off-appliance copies"
    return {"status": "ok", "mounted": mounted, "writable": True,
            "detail": f"✅ Writable and{note}."}


def _llm_section(env: dict) -> dict:
    """Serialize the LLM provider registry (secrets redacted)."""
    providers = load_providers(env)
    out = []
    for name, p in providers.items():
        key_configured = bool(p.get("api_key"))
        out.append({
            "name": name,
            "type": p.get("type", "openai"),
            "base_url": p.get("base_url", ""),
            "deployment": (p.get("deployment") or "hosted"),
            "chat_model": p.get("chat_model", ""),
            "reasoner_model": p.get("reasoner_model", ""),
            "thinking": p.get("thinking", "auto"),
            "api_key_configured": key_configured,
            "api_key": "••••••••" if key_configured else "",
            "input_price": p.get("input_price", 0),
            "output_price": p.get("output_price", 0),
            "price_mode": p.get("price_mode", "auto"),
        })
    from llm_providers import provider_order
    active = env.get("LLM_ACTIVE_PROVIDER", "deepseek") or "deepseek"
    return {
        "active_provider": active,
        "provider_order": provider_order(env),
        "providers": out,
    }


def _update_llm(config: dict, db: Session, user: User) -> dict:
    """Save the LLM provider registry + active provider selection (audit-logged)."""
    env = _read_env_file()
    updated = 0
    changed = []

    if config.get("active_provider"):
        env["LLM_ACTIVE_PROVIDER"] = str(config["active_provider"]).strip().lower()
        updated += 1
        changed.append("active_provider")

    # Failover order: primary, secondary, tertiary (≤3, unique, must exist)
    if config.get("provider_order") is not None:
        order = [str(n).strip().lower() for n in (config["provider_order"] or []) if str(n).strip()]
        if len(order) > 3:
            raise HTTPException(status_code=400, detail="provider_order supports at most 3 providers (primary, secondary, tertiary)")
        if len(set(order)) != len(order):
            raise HTTPException(status_code=400, detail="provider_order contains duplicates")
        env["LLM_PROVIDER_ORDER"] = ",".join(order)
        if order:
            env["LLM_ACTIVE_PROVIDER"] = order[0]  # primary alias (drives the pi runner secret)
        updated += 1
        changed.append("provider_order")

    for p in (config.get("providers") or []):
        name = str(p.get("name", "")).strip().lower()
        if not name:
            continue
        prefix = f"LLM_PROVIDER_{name.upper()}"
        ptype = str(p.get("type", "openai")).lower()
        if ptype not in ("openai", "anthropic", "gemini"):
            raise HTTPException(status_code=400, detail=f"Invalid provider type for {name}: {ptype}")
        env[f"{prefix}_TYPE"] = ptype
        env[f"{prefix}_BASE_URL"] = str(p.get("base_url", "")).strip()
        deployment = str(p.get("deployment") or "hosted").strip().lower()
        if deployment not in ("hosted", "on_prem"):
            raise HTTPException(status_code=400, detail=f"deployment must be hosted or on_prem (provider {name})")
        env[f"{prefix}_DEPLOYMENT"] = deployment
        env[f"{prefix}_CHAT_MODEL"] = str(p.get("chat_model", "")).strip()
        env[f"{prefix}_REASONER_MODEL"] = str(p.get("reasoner_model", "")).strip() or str(p.get("chat_model", "")).strip()
        thinking = str(p.get("thinking", "auto")).strip().lower()
        if thinking not in ("auto", "disabled", "enabled"):
            raise HTTPException(status_code=400, detail=f"thinking must be auto, disabled, or enabled (provider {name})")
        env[f"{prefix}_THINKING"] = thinking
        env[f"{prefix}_INPUT_PRICE"] = str(p.get("input_price", 0))
        env[f"{prefix}_OUTPUT_PRICE"] = str(p.get("output_price", 0))
        env[f"{prefix}_PRICE_MODE"] = str(p.get("price_mode", "auto")).lower()
        key = str(p.get("api_key", ""))
        if key and "••" not in key:
            env[f"{prefix}_API_KEY"] = key
            updated += 1
        changed.append(name)  # name only — never the API key itself
        updated += 1

    # Providers removed in the UI: drop their env keys (and prune the order)
    submitted = {str(p.get("name", "")).strip().lower() for p in (config.get("providers") or [])}
    removed = []
    for k in list(env.keys()):
        if k.startswith("LLM_PROVIDER_") and k.endswith("_TYPE"):
            name = k[len("LLM_PROVIDER_"):-len("_TYPE")].lower()
            if name not in submitted:
                for kk in list(env.keys()):
                    if kk.startswith(f"LLM_PROVIDER_{name.upper()}_"):
                        del env[kk]
                removed.append(name)
    if removed:
        order = [n for n in str(env.get("LLM_PROVIDER_ORDER", "")).split(",") if n.strip().lower() not in removed]
        env["LLM_PROVIDER_ORDER"] = ",".join(order)
        remaining = [n for n in (order or list(submitted)) if n]
        active = env.get("LLM_ACTIVE_PROVIDER", "").lower()
        if active not in remaining and remaining:
            env["LLM_ACTIVE_PROVIDER"] = remaining[0]
        changed.append("providers_removed:" + ",".join(sorted(removed)))
        updated += 1

    if not changed:
        return {"status": "ok", "updated": 0}
    _write_env_file(env)
    log_event(db, "settings_change", user.username, {
        "section": "llm",
        "fields": sorted(set(changed)),
    })
    _write_provider_secret()  # keep the pi-agent key in sync with Settings
    return {"status": "ok", "updated": updated}


@router.post("/llm/test")
def test_llm(config: dict, user: User = Depends(require_role("admin"))):
    """Probe an LLM provider (admin only). Accepts either a provider NAME
    (reads the saved config; optional api_key override) or a full inline
    provider dict (pre-save test from the wizard / Settings form)."""
    name = str(config.get("provider", "")).strip().lower()
    if config.get("base_url"):  # inline provider config (pre-save test)
        provider = {
            "type": str(config.get("type", "openai")).lower(),
            "base_url": str(config.get("base_url", "")).strip(),
            "chat_model": str(config.get("chat_model", "")).strip(),
            "reasoner_model": str(config.get("reasoner_model", "")).strip(),
            "api_key": str(config.get("api_key", "")),
            "deployment": str(config.get("deployment", "hosted")).lower(),
        }
    else:
        if not name:
            raise HTTPException(status_code=400, detail="provider is required")
        providers = load_providers(_read_env_file())
        provider = providers.get(name)
        if not provider:
            raise HTTPException(status_code=404, detail=f"Provider '{name}' not configured")
        # Optional one-time key override (first-time setup before saving)
        override = str(config.get("api_key", ""))
        if override and "••" not in override:
            provider = dict(provider)
            provider["api_key"] = override
    result = probe_provider(provider)
    if not result.get("ok"):
        err = str(result.get("error", "Probe failed"))
        if "401" in err:
            err = "Provider rejected the API key (401) — check the key"
        elif "404" in err and "chat/completions" in err:
            err = "Provider returned 404 for chat/completions — check the base URL (OpenAI-compatible APIs want the /v1 suffix)"
        elif "403" in err:
            err = "Provider denied access (403) — check the key scopes/permissions"
        raise HTTPException(status_code=502, detail=err)
    return {"status": "ok", "provider": name or str(config.get("name", "")), **result}


@router.post("/test/email")
def test_email(config: dict = None, user: User = Depends(require_role("admin"))):
    """Send a test email using current config (or form values before saving).
    Supports Gmail OAuth2 (google_* fields) and SMTP (smtp_* fields)."""
    from emailer import send_email
    config = config or {}
    overrides = {}
    for k, env_k in (("smtp_host", "SMTP_HOST"), ("smtp_port", "SMTP_PORT"),
                     ("smtp_user", "SMTP_USER"), ("smtp_password", "SMTP_PASSWORD"),
                     ("alert_email", "ALERT_EMAIL"),
                     ("google_client_id", "GOOGLE_CLIENT_ID"),
                     ("google_client_secret", "GOOGLE_CLIENT_SECRET"),
                     ("google_refresh_token", "GOOGLE_REFRESH_TOKEN"),
                     ("google_sender", "GOOGLE_SENDER")):
        val = config.get(k)
        if val and "••" not in str(val):
            overrides[env_k] = val

    alert_email = overrides.get("ALERT_EMAIL") or ""
    if not alert_email:
        raise HTTPException(status_code=400, detail="Alert recipient email is required")

    ok, err = send_email(
        alert_email,
        "BareNOC Test Email",
        body_html="<p>This is a test email from BareNOC. If you received this, email delivery is working.</p>",
        body_text="This is a test email from BareNOC. If you received this, email delivery is working.",
        overrides=overrides,
    )
    if ok:
        return {"status": "sent", "to": alert_email}
    raise HTTPException(status_code=502, detail=f"Email failed: {err}")


@router.post("/email/test-report")
def test_report(config: dict = None, user: User = Depends(require_role("admin"))):
    """Send a sample morning digest / EOD summary to the report's current recipients.
    Body: {"type": "morning_digest" | "eod_summary"}"""
    from alerting import build_report
    from emailer import send_email, get_recipients

    config = config or {}
    rtype = str(config.get("type", "")).strip().lower()
    if rtype not in ("morning_digest", "eod_summary"):
        raise HTTPException(status_code=400, detail="type must be 'morning_digest' or 'eod_summary'")

    kind = "digest" if rtype == "morning_digest" else "eod"
    recipients = get_recipients(kind)
    if not recipients:
        raise HTTPException(status_code=400, detail="No recipients configured for this report (set them in Settings → Email)")

    report = build_report(rtype)
    ok, err = send_email(
        recipients, report["subject"],
        body_html=report["body_html"], body_text=report["body_text"],
    )
    if ok:
        return {"status": "sent", "to": recipients, "subject": report["subject"]}
    raise HTTPException(status_code=502, detail=f"Email failed: {err}")


# ── Customer Logo ──

def _logo_path(name: str) -> str:
    """Safe path for a logo filename (rejects path traversal)."""
    if os.path.basename(name) != name:
        raise HTTPException(status_code=400, detail="Invalid logo filename")
    return os.path.join(BRANDING_DIR, name)


def _delete_logo_file(name: str):
    """Remove a stored logo file if it exists."""
    if not name:
        return
    try:
        os.remove(_logo_path(name))
    except FileNotFoundError:
        pass
    except Exception:
        pass


@router.post("/logo", status_code=201)
async def upload_logo(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Upload a customer logo (PNG/JPEG/WebP/SVG, max 2 MB)."""
    content_type = (file.content_type or "").lower()
    ext = LOGO_EXTENSIONS.get(content_type)
    if not ext:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Use PNG, JPEG, WebP, or SVG.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="Logo too large (max 2 MB)")

    os.makedirs(BRANDING_DIR, exist_ok=True)
    name = f"logo_{int(time.time() * 1000)}{ext}"
    with open(_logo_path(name), "wb") as f:
        f.write(data)

    # Replace the old logo
    env = _read_env_file()
    old = env.get("BRANDING_LOGO", "")
    env["BRANDING_LOGO"] = name
    _write_env_file(env)
    _delete_logo_file(old)
    log_event(db, "settings_change", user.username, {
        "section": "general",
        "fields": ["logo"],
        "values": {"logo": name},
    })

    return {"status": "ok", "filename": name}


@router.delete("/logo")
def delete_logo(db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    """Remove the customer logo (audit-logged)."""
    env = _read_env_file()
    old = env.pop("BRANDING_LOGO", "")
    _write_env_file(env)
    _delete_logo_file(old)
    log_event(db, "settings_change", user.username, {
        "section": "general",
        "fields": ["logo"],
        "values": {"logo": "(removed)" if old else "(none)"},
    })
    return {"status": "ok"}
