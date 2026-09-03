#!/usr/bin/env python3
"""Pi Agent Runner — watches /jobs/incoming/, executes safe actions, writes results."""

import os
import sys
import json
import time
import shutil
import logging
import subprocess
import datetime
import re
import uuid
import zlib
import urllib.request
import urllib.error
import ssl
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

# Allow self-signed certs for local API calls
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("pi-agent")

# Paths
BASE = "/opt/barenoc"
JOBS_INCOMING = os.path.join(BASE, "jobs", "incoming")
JOBS_RUNNING = os.path.join(BASE, "jobs", "running")
JOBS_COMPLETED = os.path.join(BASE, "jobs", "completed")
SCRIPTS_DIR = os.path.join(BASE, "scripts")
LOGS_DIR = os.path.join(BASE, "volumes", "logs", "agent")
API_CREDENTIALS_FILE = os.path.join(BASE, "agent", "credentials")


def _api_credentials() -> dict:
    """Read the agent service-account credentials (0600, pi-agent-owned).
    Never hardcode API creds in code — the agent user is provisioned by
    scripts/setup_agent_credentials.sh and rotated on every deploy."""
    creds = {}
    try:
        with open(API_CREDENTIALS_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    creds[k.strip()] = v.strip()
    except Exception as e:
        logger.error(f"Cannot read agent credentials {API_CREDENTIALS_FILE}: {e} "
                     f"(run scripts/setup_agent_credentials.sh)")
        return {}
    if not creds.get("username") or not creds.get("password"):
        logger.error(f"Agent credentials file {API_CREDENTIALS_FILE} is incomplete")
        return {}
    return creds


# ── Login token cache + backoff ──────────────────────────────────────────────
# The runner logs in at ~11 call sites; without a cache every API call mints a
# new token, which under load trips RATE_LIMIT_LOGIN (429) and loses job
# results (the /jobs/result POST then goes out with an empty Bearer -> 401 ->
# the ticket never updates -> the watchdog falsely escalates a job that
# succeeded). Reuse one token for up to 5 minutes, re-login on 401/expiry, and
# back off on 429/5xx so a transient rate-limit never costs a job result.
TOKEN_TTL_SECONDS = 300  # ≤5 min reuse

_TOKEN_CACHE = {"token": None, "expires_at": 0.0}
_TOKEN_CACHE_LOCK = threading.Lock()
_LOGIN_LOCK = threading.Lock()


def _retry_backoff(attempt: int) -> float:
    """Backoff in seconds for a 429/5xx retry (attempt is 0-based)."""
    return min(0.5 * (2 ** attempt), 8.0)


def _sleep(seconds: float) -> None:
    """Sleep indirection so tests can patch the wait out."""
    time.sleep(seconds)


def _api_login(force: bool = False) -> str:
    """Return a cached agent access token (reused ≤5 min), logging in when the
    cache is cold/expired, force=True, or a previous login was rejected (401).
    429/5xx are retried with backoff; a 401 (bad creds) surfaces as ""."""
    with _TOKEN_CACHE_LOCK:
        if not force and _TOKEN_CACHE["token"] and time.time() < _TOKEN_CACHE["expires_at"]:
            return _TOKEN_CACHE["token"]
    # Serialize actual logins so a burst of cold-cache callers (job threads +
    # the main loop) doesn't stampede the login endpoint into a 429.
    with _LOGIN_LOCK:
        with _TOKEN_CACHE_LOCK:
            if not force and _TOKEN_CACHE["token"] and time.time() < _TOKEN_CACHE["expires_at"]:
                return _TOKEN_CACHE["token"]
        creds = _api_credentials()
        if not creds:
            return ""
        last_err = "unknown"
        for attempt in range(4):
            if attempt:
                _sleep(_retry_backoff(attempt - 1))
            try:
                login_data = json.dumps({"username": creds["username"],
                                         "password": creds["password"]}).encode()
                login_req = urllib.request.Request(
                    "https://localhost/api/v1/auth/login",
                    data=login_data, headers={"Content-Type": "application/json"},
                    method="POST",
                )
                login_resp = urllib.request.urlopen(login_req, timeout=5, context=SSL_CTX)
                token = json.loads(login_resp.read().decode()).get("access_token", "")
                if token:
                    with _TOKEN_CACHE_LOCK:
                        _TOKEN_CACHE["token"] = token
                        _TOKEN_CACHE["expires_at"] = time.time() + TOKEN_TTL_SECONDS
                    return token
                last_err = "empty access_token in login response"
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    logger.error("Agent API login rejected (401) — check agent credentials")
                    with _TOKEN_CACHE_LOCK:
                        _TOKEN_CACHE["token"] = None
                        _TOKEN_CACHE["expires_at"] = 0.0
                    return ""
                if e.code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {e.code}"
                    continue  # retry with backoff
                last_err = f"HTTP {e.code}"
                break
            except Exception as e:
                last_err = str(e)
                # network/timeout — retry with backoff
        logger.error(f"Agent API login failed after retries: {last_err}")
        return ""

# Config
POLL_INTERVAL = 3  # seconds
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2") or 2)
JOB_TIMEOUT = 300  # seconds (install_chat_client needs time for remote package installs)
MAX_RETRIES = 3

# Self-protection (kept in sync with worker/restrictions.py SELF_PATTERNS):
# the appliance may never harm itself or take itself offline — no override.
SELF_PATTERNS = [
    "shutdown the appliance", "shut down the appliance", "power off the appliance",
    "poweroff the appliance", "reboot the appliance", "restart the appliance",
    "stop the appliance", "turn off the appliance",
    "reboot barenoc", "shutdown barenoc", "stop barenoc",
    "docker compose down", "docker compose stop", "stop all containers",
    "remove all containers", "delete all containers", "docker rm",
    "delete the database", "wipe the database", "erase the database",
    "delete the backups", "erase the backups", "wipe the backups",
    "delete /opt/barenoc", "erase /opt/barenoc", "rm -rf /opt/barenoc",
    "delete the credentials", "delete the .env", "erase the .env",
    "format the disk", "mkfs", "wipe the disk", "dd if=/dev/zero",
    "flush the firewall", "flush iptables", "flush nftables", "drop all firewall rules",
    "change the appliance ip", "change the appliance gateway",
    "change the appliance dns", "change the appliance's ip",
]


def _self_blocked(text: str) -> str:
    """Return the matched self-protection phrase (or "") before pi runs."""
    t = (text or "").lower()
    for pat in SELF_PATTERNS:
        if pat in t:
            return pat
    return ""


def _self_target(target: str) -> bool:
    """True when the action target is the appliance itself (own IP / names)."""
    t = (target or "").strip().lower()
    if not t:
        return False
    ips = [i for i in (_appliance_identity() or "").split(",") if i.strip()]
    ips += [x.lower() for x in
            ("127.0.0.1", "localhost", "bareNOC", "bareNOC.local", "app.barenoc.com")]
    return any(i and i in t for i in ips)


_APPLIANCE_IDENTITY = None


def _appliance_identity() -> str:
    """The appliance's own IP — fetched from the API once (pi-agent can't read
    the 0600 .env; the api exposes it for the agent service account)."""
    global _APPLIANCE_IDENTITY
    if _APPLIANCE_IDENTITY is not None:
        return _APPLIANCE_IDENTITY
    _APPLIANCE_IDENTITY = ""
    try:
        token = _api_login()
        if not token:
            return _APPLIANCE_IDENTITY
        req = urllib.request.Request(
            "https://localhost/api/v1/settings/appliance-identity",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = urllib.request.urlopen(req, timeout=5, context=SSL_CTX)
        _APPLIANCE_IDENTITY = str(json.loads(resp.read().decode()).get("ip") or "").strip()
    except Exception as e:
        logger.warning(f"appliance identity lookup failed: {e}")
    return _APPLIANCE_IDENTITY


# Allowed actions mapped to their scripts
ACTION_SCRIPTS = {
    "ping_test": "ping_check.sh",
    "snmp_poll": "snmp_poll.sh",
    "device_status": "ping_check.sh",  # Use ping as basic status check
    "reboot_device": "reboot_device.sh",
    "apply_patch": "apply_patch.sh",
    "collect_logs": "collect_logs.sh",
    "network_discovery": "discover.sh",
    "network_info": "network_info.sh",
    "system_time": "system_time.sh",
    "ticket_status": "ticket_status.sh",
    "unifi_clients": "unifi_clients.sh",
    "unifi_devices": "unifi_devices.sh",
    "unifi_ports": "unifi_ports.sh",
    "unifi_client_port": "unifi_client_port.sh",
    "unifi_firewall_rules": "unifi_firewall_rules.sh",
    "fingerprint_device": "fingerprint.sh",
    "unifi_port_config": "unifi_port.sh",
    "unifi_restart": "unifi_restart.sh",
    "unifi_port_bounce": "unifi_port_bounce.sh",
    "unifi_port_rename": "unifi_port_rename.sh",
    "unifi_ensure_wireless_uplinks": "unifi_ensure_wireless_uplinks.sh",
    "unifi_set_ssid_password": "unifi_set_ssid_password.sh",
    "unifi_network_create": "unifi_network_create.sh",
    "install_chat_client": "install_chat_client.sh",
    "enroll_device": "enroll_device.sh",
    "snmp_sweep": "snmp_sweep.sh",
    "windows_diag": "windows_diag.sh",
    "windows_cleanup": "windows_cleanup.sh",
    # escalate_human is a logical action, not a script
}

# Managed device cache (hostname/IP lookup)
MANAGED_DEVICES = {}
# name -> MAC (for UniFi port actions whose target must be the switch MAC)
MANAGED_MACS = {}
# ip -> device id (for fetching a device's stored SSH credentials)
DEVICE_BY_IP = {}

# Fallback SSH identity when a device has no stored credentials
DEFAULT_SSH_KEY = "/opt/barenoc/volumes/secrets/ssh/id_ed25519"
# Temp key files written for the current job (cleaned up after the run)
_TEMP_KEYS = []


def load_managed_devices():
    """Load device list from API using the agent service account."""
    global MANAGED_DEVICES, MANAGED_MACS, DEVICE_BY_IP
    try:
        # First get a token
        token = _api_login()
        if not token:
            return

        # Then fetch devices
        req = urllib.request.Request(
            "https://localhost/api/v1/devices",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = urllib.request.urlopen(req, timeout=5, context=SSL_CTX)
        data = json.loads(resp.read().decode())
        devices = data if isinstance(data, list) else data.get("devices", [])
        # Build new maps, then swap atomically — job worker threads resolve
        # targets concurrently, so an in-place clear() would show them a
        # partially-empty map mid-reload.
        new_devices, new_macs, new_by_ip = {}, {}, {}
        for d in devices:
            new_devices[d["name"]] = d["ip_address"]
            new_devices[d["ip_address"]] = d["ip_address"]
            if d.get("hostname"):
                new_devices[d["hostname"]] = d["ip_address"]
            new_by_ip[d["ip_address"]] = d["id"]
            # MAC passthrough — UniFi port actions target the switch MAC
            if d.get("mac_address"):
                new_devices[d["mac_address"]] = d["mac_address"]
                new_macs[d["name"]] = d["mac_address"]
                new_macs[d["ip_address"]] = d["mac_address"]  # IP targets resolve too
        MANAGED_DEVICES, MANAGED_MACS, DEVICE_BY_IP = new_devices, new_macs, new_by_ip
        logger.info(f"Loaded {len(devices)} managed devices")
    except Exception:
        pass  # Silently skip


def resolve_target(target: str) -> str:
    """Resolve device name to IP address."""
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", target):
        return target  # Already an IP
    ip = MANAGED_DEVICES.get(target)
    if ip:
        return ip
    return target  # Return as-is, let the script handle it


def _device_ssh_creds(ip: str) -> "dict | None":
    """Fetch a managed device's decrypted SSH credentials from the API
    (agent service account is admin). Returns {"ssh_user", "ssh_key"} or
    None when the device is unknown / has no stored credentials."""
    dev_id = DEVICE_BY_IP.get(ip or "")
    if not dev_id:
        return None
    try:
        token = _api_login()
        if not token:
            return None
        req = urllib.request.Request(
            f"https://localhost/api/v1/devices/{dev_id}/credentials",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = urllib.request.urlopen(req, timeout=5, context=SSL_CTX)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def _temp_key_path() -> str:
    """Path for a decrypted temp SSH key (0600, pi-agent-owned, removed after
    the job finishes)."""
    path = f"/tmp/pi-agent-{uuid.uuid4().hex}.key"
    _TEMP_KEYS.append(path)
    return path


def _resolve_ssh(target: str, params: dict) -> tuple:
    """(ssh_user, ssh_key_path) for an SSH-based action on `target` (an IP).
    Precedence: explicit job params > device's stored credentials > defaults."""
    ssh_user = str(params.get("ssh_user") or "").strip()
    ssh_key = str(params.get("ssh_key") or "").strip()
    if not ssh_user or not ssh_key:
        creds = _device_ssh_creds(target)
        if creds:
            if not ssh_user and creds.get("ssh_user"):
                ssh_user = creds["ssh_user"]
            if not ssh_key and creds.get("ssh_key"):
                key_path = _temp_key_path()
                key = creds["ssh_key"]
                if not key.endswith("\n"):
                    key += "\n"  # ssh-keygen rejects keys without the trailing newline (OpenSSL 3.0)
                with open(key_path, "w") as f:
                    f.write(key)
                os.chmod(key_path, 0o600)
                ssh_key = key_path
    # Last-resort default: the dedicated `barenoc` control user that the
    # appliance's onboarding flows create (Linux). Stored creds / explicit
    # params always win when present.
    return ssh_user or "barenoc", ssh_key or DEFAULT_SSH_KEY


def validate_job(job: dict) -> tuple[bool, str]:
    """Validate a job before execution."""
    if "action" not in job:
        return False, "Missing 'action' field"
    if job["action"] == "batch":
        # batch has no script — execute_job expands it. Sub-jobs were already
        # validated by the worker's action_validator.
        return True, ""
    if job["action"] == "pi_task":
        # logical action — execute_job runs the Pi Coding Agent headlessly
        return True, ""
    if job["action"] not in ACTION_SCRIPTS:
        return False, f"Unknown action: {job['action']}"
    if job["action"] == "escalate_human":
        return False, "Escalation requires human intervention"
    return True, ""


def resolve_port_target(target: str) -> str:
    """Resolve a device NAME to its MAC for UniFi port actions (target must be
    the switch MAC). MACs and unknown names pass through — the script rejects
    non-MAC targets with a clear error."""
    if not target:
        return target
    if re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", target):
        return target
    if not MANAGED_MACS:
        load_managed_devices()  # defensive: ensure the name->MAC map is fresh
    return MANAGED_MACS.get(target, target)


_UPLINK_CACHE = {}  # device_mac -> (uplink_mac, uplink_port) from the topology


def resolve_uplink(target: str) -> "tuple | None":
    """For an AP/device target, return its uplink (switch_mac, switch_port) via
    the topology endpoint — so 'tag the uplink of an Outdoor AP' targets the right
    switch port instead of the AP itself. Returns None when not resolvable."""
    mac = resolve_port_target(target)
    if not re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", mac or ""):
        return None
    if mac in _UPLINK_CACHE:
        return _UPLINK_CACHE[mac]
    try:
        token = _api_login()
        if not token:
            return None
        req = urllib.request.Request(
            "https://localhost/api/v1/unifi/topology",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = urllib.request.urlopen(req, timeout=8, context=SSL_CTX)
        topo = json.loads(resp.read().decode())
        for d in topo.get("devices", []):
            if (d.get("mac") or "").lower() == mac.lower():
                up_mac = d.get("uplink_mac") or ""
                up_port = d.get("uplink_remote_port")
                if up_mac:
                    _UPLINK_CACHE[mac] = (up_mac, up_port)
                    return _UPLINK_CACHE[mac]
                break
    except Exception:
        pass
    return None


def _port_target_and_idx(target: str, port_idx):
    """For UniFi port actions: resolve to the switch MAC. If the target is an
    AP/device with an uplink, use the uplink SWITCH + port, so 'tag the uplink
    of an Outdoor AP' targets the right switch port. Returns (switch_mac, port_idx)."""
    mac = resolve_port_target(target)
    up = resolve_uplink(target)
    if up and up[0]:
        idx = up[1] if port_idx in (None, "", 0) else port_idx
        return up[0], idx
    return mac, port_idx


_MAC_TARGET_ACTIONS = {"unifi_port_config", "unifi_port_bounce",
                        "unifi_port_rename", "unifi_ports", "unifi_restart"}


def _build_cmd(action: str, target: str, params: dict) -> list:
    """Build the bash command for one action (no script existence checks)."""
    script = ACTION_SCRIPTS.get(action)
    script_path = os.path.join(SCRIPTS_DIR, script)
    if action == "ping_test":
        return ["bash", script_path, target]
    if action == "ticket_status":
        return ["bash", script_path, str(params.get("ticket_id", ""))]
    if action == "network_discovery":
        return ["bash", script_path, target]
    if action == "unifi_port_config":
        tagged = params.get("tagged", "")
        if isinstance(tagged, list):
            tagged = ",".join(tagged)
        native = params.get("native", "-")
        mac, port_idx = _port_target_and_idx(target, params.get("port_idx"))
        return ["bash", script_path, mac, str(port_idx),
                str(tagged), str(native)]
    if action == "unifi_port_bounce":
        mac, port_idx = _port_target_and_idx(target, params.get("port_idx"))
        return ["bash", script_path, mac, str(port_idx)]
    if action == "unifi_port_rename":
        mac, port_idx = _port_target_and_idx(target, params.get("port_idx"))
        return ["bash", script_path, mac, str(port_idx),
                str(params.get("name", ""))]
    if action == "install_chat_client":
        ssh_user, ssh_key = _resolve_ssh(target, params)
        return ["bash", script_path, target, ssh_user, ssh_key]
    if action == "enroll_device":
        ssh_user, ssh_key = _resolve_ssh(target, params)
        ttl = str(params.get("ttl") or "600")
        return ["bash", script_path, target, ssh_user, ssh_key, ttl]
    if action == "snmp_sweep":
        return ["bash", script_path, target, str(params.get("community", "public"))]
    if action == "unifi_set_ssid_password":
        return ["bash", script_path, str(params.get("ssid", "")),
                str(params.get("password", ""))]
    if action == "unifi_network_create":
        # name + vlan required; subnet/dhcp optional (script defaults them)
        return ["bash", script_path, str(params.get("name", "")),
                str(params.get("vlan", "")),
                str(params.get("subnet", "")),
                str(params.get("dhcp", "true"))]
    if action in ("network_info", "unifi_firewall_rules", "unifi_ensure_wireless_uplinks", "system_time"):
        return ["bash", script_path]
    if action == "unifi_devices":
        # optional filters: device_type (ap|switch|gateway), status (online|offline)
        return ["bash", script_path, str(params.get("device_type", "")),
                str(params.get("status", ""))]
    if action == "unifi_clients":
        # optional filters: online (true|false), wired (true|false)
        return ["bash", script_path,
                str(params.get("online", "")).lower(),
                str(params.get("wired", "")).lower()]
    if action == "snmp_poll":
        return ["bash", script_path, target, params.get("community", "public")]
    if action in ("reboot_device", "apply_patch", "collect_logs"):
        ssh_user, ssh_key = _resolve_ssh(target, params)
        if action == "apply_patch":
            return ["bash", script_path, target, params.get("patch_id", "latest"),
                    ssh_user, ssh_key]
        if action == "collect_logs":
            return ["bash", script_path, target, str(params.get("lines", 50)),
                    ssh_user, ssh_key]
        return ["bash", script_path, target, ssh_user, ssh_key]
    if action in ("windows_diag", "windows_cleanup"):
        # Windows PCs are SSH-only for now: resolve the stored control creds
        # and pass the (optional, configurable) offender list for cleanup.
        ssh_user, ssh_key = _resolve_ssh(target, params)
        argv = ["bash", script_path, target, ssh_user, ssh_key]
        if action == "windows_cleanup":
            offenders = params.get("offenders") or []
            if isinstance(offenders, list):
                argv.append(",".join(str(o) for o in offenders if str(o).strip()))
        return argv
    return ["bash", script_path, target]


def _run_sweep(cmd: list, ticket_id: str, env: dict, action: str,
               target: str) -> dict:
    """Run a subnet sweep (network_discovery / snmp_sweep) with live progress
    notes streamed from the script's stderr (PROGRESS: lines), and the same
    JOB_TIMEOUT abort as any other step — a sweep never hangs the runner.

    The 08-19 fix: whole-subnet sweeps are parallel + capped inside the script,
    so this path only needs to (1) relay progress and (2) abort cleanly at the
    deadline.
    """
    tone = _ProgressTone(ticket_id)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)

    def _relay():
        try:
            for line in iter(proc.stderr.readline, ""):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("PROGRESS:"):
                    _post_progress(ticket_id, line[len("PROGRESS:"):].strip(), tone)
        except Exception:
            pass

    relay = threading.Thread(target=_relay, daemon=True)
    relay.start()
    timed_out = False
    try:
        # Sweep stdout is one small JSON object; the relay thread owns stderr,
        # so we wait for exit then drain stdout (never communicate() — that
        # would race the stderr reader).
        proc.wait(timeout=JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    stdout = (proc.stdout.read() or "") if proc.stdout else ""
    try:
        relay.join(timeout=2)
    except Exception:
        pass
    for pipe in (proc.stdout, proc.stderr):
        try:
            if pipe:
                pipe.close()
        except Exception:
            pass
    if timed_out:
        logger.warning(f"Sweep timed out after {JOB_TIMEOUT}s (ticket {ticket_id})")
        return {"success": False, "error": f"Timed out after {JOB_TIMEOUT}s",
                "target": target, "action": action}
    stdout = (stdout or "").strip()
    try:
        output = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        output = {"raw_output": stdout}
    return {"success": proc.returncode == 0, "exit_code": proc.returncode,
            "output": output, "target": target, "action": action}


def _run_single(action: str, target: str, params: dict, ticket_id: str,
                env_tz: str = "") -> dict:
    """Run ONE action script and parse its JSON output."""
    script = ACTION_SCRIPTS.get(action)
    if not script:
        return {"success": False, "error": f"No script for action: {action}"}
    script_path = os.path.join(SCRIPTS_DIR, script)
    if not os.path.exists(script_path):
        return {"success": False, "error": f"Script not found: {script_path}"}
    cmd = _build_cmd(action, target, params)
    logger.info(f"Executing: {' '.join(cmd[:4])}... (ticket {ticket_id})")
    env = os.environ.copy()
    if env_tz:
        # The appliance timezone (worker reads .env; pi-agent can't).
        env["TZ"] = env_tz
    try:
        if action in ("network_discovery", "snmp_sweep"):
            return _run_sweep(cmd, ticket_id, env, action, target)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=JOB_TIMEOUT, env=env)
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            try:
                output = json.loads(stdout) if stdout else {}
            except json.JSONDecodeError:
                output = {"raw_output": stdout}
            return {
                "success": result.returncode == 0,
                "exit_code": result.returncode,
                "output": output,
                "stderr": stderr if stderr else None,
                "target": target,
                "action": action,
            }
        except subprocess.TimeoutExpired:
            logger.warning(f"Job timed out after {JOB_TIMEOUT}s (ticket {ticket_id})")
            return {"success": False, "error": f"Timed out after {JOB_TIMEOUT}s"}
    finally:
        # Remove any decrypted temp SSH keys written for this job
        while _TEMP_KEYS:
            p = _TEMP_KEYS.pop()
            try:
                os.unlink(p)
            except Exception:
                pass


PI_PROVIDER_SECRET_FILE = "/opt/barenoc/volumes/secrets/llm_provider.json"
WEB_RESEARCH_SECRET_FILE = "/opt/barenoc/volumes/secrets/web_research.json"


def _web_research_enabled() -> bool:
    """Deployment-level L3 egress opt-in (compliance control web_research ->
    WEB_RESEARCH_ENABLED), mirrored to this pi-agent-readable file by the API.
    The per-ticket opt-in is checked separately; both must be on before any
    network egress (opt-in, never implicit)."""
    try:
        with open(WEB_RESEARCH_SECRET_FILE) as f:
            return bool(json.load(f).get("enabled"))
    except Exception:
        return False


def _pi_provider_config() -> dict:
    """Provider/model/api-key for pi, straight from BareNOC's Settings.
    Prefers the API-written secrets file (llm_provider.json — kept in sync on
    every settings save), falls back to reading .env. Maps the BareNOC provider
    block to a provider pi recognizes.

    Local-only egress (compliance): the secret file carries base_url + local
    flag — pi runs the on-prem endpoint as PRIMARY (no cloud).
    """
    try:
        with open(PI_PROVIDER_SECRET_FILE) as f:
            d = json.load(f)
        if d.get("base_url"):
            return {"provider": d.get("provider", "openai"),
                    "model": d["model"], "api_key": d.get("api_key") or "ollama",
                    "base_url": d["base_url"]}
        if d.get("api_key") and d.get("model"):
            return {"provider": d.get("provider", "deepseek"),
                    "model": d["model"], "api_key": d["api_key"]}
    except Exception:
        pass
    env = {}
    try:
        with open("/opt/barenoc/.env") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    except Exception:
        pass
    # Local-only fallback: pick the first on-prem provider from the order.
    egress = (env.get("LLM_EGRESS", "cloud") or "cloud").strip().lower()
    if egress in ("local", "local-only", "on_prem"):
        order = (env.get("LLM_PROVIDER_ORDER", "") or "").strip()
        names = [n.strip().lower() for n in order.split(",") if n.strip()]
        if not names:
            names = [(env.get("LLM_ACTIVE_PROVIDER", "") or "").strip().lower()]
            names = [n for n in names if n]
        for name in names:
            prefix = f"LLM_PROVIDER_{name.upper()}"
            dep = (env.get(f"{prefix}_DEPLOYMENT", "hosted") or "hosted").lower()
            if dep != "on_prem":
                continue
            ptype = (env.get(f"{prefix}_TYPE", "openai") or "openai").lower()
            provider = ("anthropic" if ptype == "anthropic"
                        else ("google" if ptype == "gemini" else "openai"))
            return {
                "provider": provider,
                "model": env.get(f"{prefix}_CHAT_MODEL", "") or "",
                "api_key": env.get(f"{prefix}_API_KEY", "") or "ollama",
                "base_url": (env.get(f"{prefix}_BASE_URL", "") or "").rstrip("/"),
            }
        return {"provider": "openai", "model": "", "api_key": "ollama"}
    active = env.get("LLM_ACTIVE_PROVIDER", "deepseek").strip().lower() or "deepseek"
    prefix = f"LLM_PROVIDER_{active.upper()}"
    ptype = (env.get(f"{prefix}_TYPE", "openai") or "openai").lower()
    base = (env.get(f"{prefix}_BASE_URL", "") or "").lower()
    model = env.get(f"{prefix}_CHAT_MODEL", "") or "deepseek-v4-flash"
    key = env.get(f"{prefix}_API_KEY", "")
    if "deepseek" in base or active.startswith("deepseek"):
        provider = "deepseek"
    elif ptype == "anthropic":
        provider = "anthropic"
    elif ptype == "gemini":
        provider = "google"
    else:
        provider = "openai"
    return {"provider": provider, "model": model, "api_key": key}


# Chat-tone safety net for live progress notes. A verbose pi session may emit
# notes full of internals (paths, sudo/perm analysis, package names, API
# detail). The sysctx asks for friendly one-liners, but this is the backstop:
# technical-looking notes are replaced with a short, category-matched friendly
# phrase before they reach the chat. The raw text stays in the pi session
# transcript (and the runner log) — the customer never sees internals. Final
# answers are cleaned separately in src/api/tone_filter.py (jobs.py result
# formatting).
#
# The pool + keyword cues + tech patterns below are a VENDORED copy of
# src/api/tone_pool.py (the canonical source of truth shared with the API-side
# queue_status). The runner deploys as a single self-contained file, so it
# cannot import that module at runtime; src/agent/test_runner.py asserts the
# two copies stay in sync.

# ── Activity categories (order = tie-break priority) ─────────────────────
_CATEGORIES = ("investigating", "connecting", "applying", "verifying", "waiting")

_CATEGORY_KEYWORDS = {
    "investigating": (
        "check", "checking", "read", "reading", "fetch", "fetching",
        "list", "listing", "look", "looking", "find", "finding", "search",
        "searching", "scan", "scanning", "inspect", "inspecting", "review",
        "reviewing", "examine", "examining", "diagnose", "diagnosing",
        "investigate", "investigating", "gather", "gathering", "query",
        "querying", "explore", "exploring", "trace", "tracing", "audit",
        "auditing", "browse", "browsing", "discover", "probe", "probing",
        "dig", "digging", "peek", "glance", "look into",
    ),
    "connecting": (
        "connect", "connecting", "ssh", "login", "log in", "reach",
        "reaching", "ping", "talk", "talking", "device", "laptop",
        "gateway", "switch", "access point", "unifi", "enroll",
        "enrolling", "adopt", "adopting", "handshake", "establish",
        "link", "linking", "attach", "interface", "network", "contact",
        "session", "remote", "handshaking",
    ),
    "applying": (
        "apply", "applying", "change", "changing", "set", "setting",
        "install", "installing", "update", "updating", "upgrade",
        "upgrading", "configure", "configuring", "config", "patch",
        "patching", "deploy", "deploying", "enable", "enabling", "disable",
        "disabling", "restart", "restarting", "reboot", "rebooting",
        "start", "starting", "stop", "stopping", "modify", "modifying",
        "create", "creating", "add", "adding", "remove", "removing",
        "delete", "deleting", "write", "writing", "push", "pushing",
        "roll", "rolling", "replace", "replacing", "move", "moving", "fix",
        "fixing", "adjust", "adjusting", "tune", "tuning", "edit",
        "editing", "put in",
    ),
    "verifying": (
        "verify", "verifying", "confirm", "confirming", "test", "testing",
        "validate", "validating", "ensure", "ensuring", "double-check",
        "doublecheck", "double check", "compare", "comparing", "assert",
        "finalize", "finalizing", "wrap", "wrapping", "finish", "finishing",
        "complete", "completing", "cleanup", "clean up", "tidy", "tidying",
        "almost done", "make sure", "works now", "end to end", "recheck",
        "check it",
    ),
    "waiting": (
        "wait", "waiting", "hold", "holding", "while", "long", "minute",
        "minutes", "be patient", "patience", "taking a", "takes a",
        "running", "processing", "compiling", "building", "downloading",
        "uploading", "syncing", "still", "moment", "hang tight",
        "bear with", "longer", "chug", "progress", "ongoing", "be patient",
    ),
}

# The friendly phrase pool — one list per activity category.
_TONE_POOL = {
    "investigating": [
        "Taking a look at that now…",
        "Let me check on that for you…",
        "Looking into it — one moment…",
        "Digging into the details now…",
        "Reading through the current setup…",
        "Checking the latest state of things…",
        "Reviewing what's there before I change anything…",
        "Gathering the information I need…",
        "Scanning for the source of that…",
        "Tracing through the logs now…",
        "Seeing what's going on behind the scenes…",
        "Investigating — this won't take long…",
        "Pulling up the current details…",
        "Having a closer look at your setup…",
    ],
    "connecting": [
        "Connecting to the device now…",
        "Reaching out to the device…",
        "Getting a secure connection set up…",
        "Talking to the device — one sec…",
        "Making contact with your network…",
        "Linking up with the hardware…",
        "Establishing the connection…",
        "Opening a line to the device…",
        "Handshaking with the device…",
        "Connecting to your network gear…",
        "Reaching the device now…",
        "Touching base with the device…",
        "Getting through to the device…",
        "Bringing the device online…",
    ],
    "applying": [
        "Applying that change now…",
        "Making the change you asked for…",
        "Installing it now…",
        "Setting things up as requested…",
        "Applying the update…",
        "Rolling out the new setting…",
        "Writing the change into place…",
        "Putting the fix in now…",
        "Configuring that for you…",
        "Swapping in the new settings…",
        "Deploying the change…",
        "Updating things now…",
        "Making that adjustment…",
        "Setting it up — this part takes a moment…",
    ],
    "verifying": [
        "Verifying everything looks right…",
        "Confirming the change took effect…",
        "Double-checking my work…",
        "Testing that it works now…",
        "Making sure it's all good…",
        "Checking the result is correct…",
        "Validating the new setup…",
        "Confirming it works end to end…",
        "Running a quick check to be sure…",
        "Just confirming the details…",
        "Wrapping up and verifying…",
        "Almost done — just verifying…",
        "Giving it a final once-over…",
        "Confirming everything is in place…",
    ],
    "waiting": [
        "Still on it — one moment…",
        "Hang tight, this is taking a little longer…",
        "Still working away on this…",
        "This step takes a few minutes…",
        "Running the longer part now…",
        "Working through it — please bear with me…",
        "Still making progress…",
        "This one's a longer task…",
        "Chugging through it — won't be much longer…",
        "Still going — thanks for waiting…",
        "Processing now — this can take a bit…",
        "Moving along — a few more moments…",
        "Almost there — thank you for waiting…",
        "Keeping at it — a little more time…",
    ],
}

# Backward-compat flat list (every phrase in the pool).
_FRIENDLY_PROGRESS = [p for c in _CATEGORIES for p in _TONE_POOL[c]]

# Known personal identifiers (the developer / owner) that must never appear in
# any agent-visible text — the injected context, work notes, progress notes, or
# answers. VENDORED copy of src/api/tone_pool.py IDENTITY_PATTERNS (the runner
# deploys as a single self-contained file); test_runner asserts parity.
_IDENTITY_PATTERNS = (
    re.compile(r"yery\.odell@[a-z0-9.\-]*", re.IGNORECASE),  # tailnet login + known emails
    re.compile(r"yery@odell\.dev", re.IGNORECASE),            # dev email
    re.compile(r"yery[.\-_]odell", re.IGNORECASE),            # dot/dash/underscore-joined
    re.compile(r"yery\s+o['’]?dell", re.IGNORECASE),          # full name "Yery O'Dell"
    re.compile(r"\byery\b", re.IGNORECASE),                   # handle / first name
    re.compile(r"\bo['’]dell\b", re.IGNORECASE),              # surname "O'Dell"
)


def _redact_identities(text: str, replacement: str = "[redacted]") -> str:
    """Replace the known personal identifiers with `replacement` (never a
    generic name scan). Applied to the ticket context/task BEFORE the agent
    sees it so the agent cannot read or re-surface the developer/owner
    identity."""
    if not text:
        return text
    for pattern in _IDENTITY_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_TECH_NOTE_PATTERNS = [
    re.compile(r"`"),                       # backticked command / `code`
    re.compile(r"~/", re.IGNORECASE),       # home-path shorthand
    re.compile(r"/(etc|opt|usr|var|home|tmp|sbin|bin)/", re.IGNORECASE),
    re.compile(r"\\"),                    # Windows path separator
    re.compile(r"\b(uids?|gids?|pids?|nopasswd|sudoers|passwordless|passwd)\b",
               re.IGNORECASE),
    re.compile(r"\b(sudo|ssh|scp|rsync|dnf|apt|apt-get|yum|apk|zypper|curl|wget|"
               r"systemctl|journalctl|chmod|chown|usermod|nmap|ping|traceroute)\b",
               re.IGNORECASE),
    re.compile(r"\b(localhost|127\.0\.0\.1|0\.0\.0\.0)\b", re.IGNORECASE),
    re.compile(r"(api/|/api/v\d|endpoint|https?://)", re.IGNORECASE),
    re.compile(r"\.json\b", re.IGNORECASE),
    re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"),   # bare IPv4 addresses
    re.compile(r"\b(TKT-\d|ticket_id|access_token|bearer)\b", re.IGNORECASE),
    # Tailnet account logins ("name.name@" with nothing after the @ — the
    # peer owner login shown by `tailscale status`, e.g. "name.name@").
    # Never reaches the customer (the 08-26 identity-leak lesson).
    re.compile(r"\b[a-z0-9][a-z0-9._-]*@(?![a-z0-9])", re.IGNORECASE),
    # Known personal identifiers (developer/owner) — any note naming one is
    # treated as technical and scrubbed to a friendly generic (_IDENTITY_PATTERNS).
    *_IDENTITY_PATTERNS,
]

# A friendly progress note is one short sentence; anything this long is almost
# certainly several sentences of internal detail.
_PROGRESS_MAX_FRIENDLY_LEN = 220

# Elapsed-time heartbeat for long pi tasks: after this long with no distinct
# activity, the runner injects a keep-alive note so a long run doesn't look
# hung. Every Nth heartbeat carries the elapsed-time text; the others draw a
# varied phrase from the "waiting" category.
HEARTBEAT_AFTER_SECS = 120
HEARTBEAT_INTERVAL_SECS = 45
HEARTBEAT_ELAPSED_EVERY = 3


def _is_technical_note(text: str) -> bool:
    """True when a live progress note looks like internal/technical detail."""
    t = (text or "").strip()
    if not t:
        return False
    if len(t) > _PROGRESS_MAX_FRIENDLY_LEN:
        return True
    if any(p.search(t) for p in _TECH_NOTE_PATTERNS):
        return True
    # ">N chars of jargon": snake_case / long / digit-letter tokens.
    words = re.findall(r"[A-Za-z0-9_]+", t)
    jargon = 0
    for w in words:
        if len(w) > 18 or "_" in w or (
                re.search(r"\d", w) and re.search(r"[A-Za-z]", w) and len(w) >= 4):
            jargon += 1
    return jargon >= 2


def _categorize(text: str) -> str:
    """Map a raw (technical) note to an activity category via keyword cues.
    Highest-scoring category wins; ties break by _CATEGORIES order; no match
    falls back to "investigating"."""
    low = (text or "").lower()
    best, best_score = _CATEGORIES[0], -1
    for category in _CATEGORIES:
        score = sum(
            1 for kw in _CATEGORY_KEYWORDS[category]
            if re.search(r"\b" + re.escape(kw) + r"\b", low))
        if score > best_score:
            best, best_score = category, score
    return best


def _pick_phrase(category: str, seed: int, recent=()) -> str:
    """Pick a phrase from a category's pool, avoiding `recent` when possible.
    Deterministic for a given (category, seed, recent)."""
    pool = _TONE_POOL.get(category) or _TONE_POOL[_CATEGORIES[0]]
    recent_set = set(recent or ())
    avail = [p for p in pool if p not in recent_set]
    if not avail:
        avail = pool
    return avail[seed % len(avail)]


def _friendly_progress_note(text: str, seed: int = 0, recent=()) -> "tuple[str, bool]":
    """Return (chat_safe_note, was_filtered) for a live progress note.

    User-facing notes pass through untouched; technical-looking notes are
    replaced with a category-matched friendly phrase. `seed` is a stable
    per-ticket integer (same note -> same phrase); `recent` is an iterable of
    recently-used phrases so consecutive notes differ."""
    if not _is_technical_note(text):
        return (text or "").strip(), False
    category = _categorize(text)
    base = (seed or 0) ^ (zlib.crc32((text or "").encode("utf-8")) & 0xffffffff)
    return _pick_phrase(category, base, recent), True


def _elapsed_heartbeat(elapsed_seconds: float) -> str:
    """The elapsed-time keep-alive text for a long task ("about N min in")."""
    mins = max(1, int(round(elapsed_seconds / 60.0)))
    if mins < 60:
        return f"Still working — about {mins} min in — this one's a longer task…"
    h, m = divmod(mins, 60)
    return f"Still working — about {h}h {m}m in — this one's a longer task…"


def _heartbeat_phrase(elapsed_seconds: float, nth: int, recent=()) -> str:
    """A heartbeat phrase: every Nth carries elapsed time; the rest are varied
    "waiting" phrases so long runs stay friendly but not repetitive."""
    if nth > 0 and nth % HEARTBEAT_ELAPSED_EVERY == 0:
        return _elapsed_heartbeat(elapsed_seconds)
    return _pick_phrase("waiting", int(elapsed_seconds) ^ (nth * 7919), recent)


class _ProgressTone:
    """Per-ticket progress-note state: a stable seed, no-repeat memory, and the
    elapsed-time heartbeat for long tasks. One instance per pi run."""

    def __init__(self, ticket_id: str, started_at: "float | None" = None):
        self.ticket_seed = zlib.crc32((ticket_id or "").encode("utf-8")) & 0xffffffff
        self.started_at = started_at if started_at is not None else time.time()
        self.recent = deque(maxlen=4)   # last N phrases, to avoid repeats
        self.heartbeat_count = 0
        self.last_note_at = self.started_at

    def friendly(self, text: str) -> "tuple[str, bool]":
        """Filter a raw note through the pool (category-matched, no-repeat)."""
        text, was_filtered = _friendly_progress_note(
            text, self.ticket_seed, self.recent)
        if was_filtered:
            self.recent.append(text)
        return text, was_filtered

    def heartbeat(self, now: "float | None" = None) -> "str | None":
        """Return a keep-alive note when the task has run long with no distinct
        activity, else None. Caller owns the post throttle (min-gap / cap)."""
        now = now if now is not None else time.time()
        if now - self.started_at < HEARTBEAT_AFTER_SECS:
            return None
        if now - self.last_note_at < HEARTBEAT_INTERVAL_SECS:
            return None
        self.heartbeat_count += 1
        phrase = _heartbeat_phrase(now - self.started_at, self.heartbeat_count,
                                   self.recent)
        self.recent.append(phrase)
        self.last_note_at = now
        return phrase

# Progress-note cap: high enough that a real pi answer fits (the 08-17 incident
# stored a 250-char slice of a dnf check-update answer mid-sentence, no
# ellipsis), low enough to keep a note from ballooning. MUST MATCH
# src/api/routes/tickets.py (PROGRESS_NOTE_MAX_CHARS) — the stored note is the
# smaller of the two layers.
PROGRESS_NOTE_MAX_CHARS = 2000


def _ellipsize(text: str, limit: int = PROGRESS_NOTE_MAX_CHARS) -> str:
    """Trim `text` to at most `limit` chars. If content was removed, append a
    Unicode ellipsis (…) so the note reads as a snippet — a truncation is never
    silent. Cut on a word boundary when possible so a word isn't split mid-run."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def _assistant_complete_text(msg: dict) -> "str | None":
    """The concatenated text of an assistant session message, or None when the
    message is still streaming/incomplete (no terminal stopReason, or the
    reserved 'pending' value). pi persists only finalized messages, so this is
    a guard against posting a mid-word fragment as a progress note."""
    if not isinstance(msg, dict):
        return None
    stop = msg.get("stopReason")
    if stop in (None, "pending"):
        return None
    text = "".join(
        (c.get("text") or "") for c in (msg.get("content") or [])
        if isinstance(c, dict) and c.get("type") == "text")
    return text.strip() or None


def _post_progress(ticket_id: str, text: str, tone: "_ProgressTone | None" = None) -> None:
    """Post a brief live progress note to the ticket (agent_progress event)."""
    raw = text
    if tone is not None:
        text, was_filtered = tone.friendly(text)
    else:
        text, was_filtered = _friendly_progress_note(text)
    if was_filtered:
        # The technical original lives in the pi session transcript; log it here
        # too so the runner log (support bundle) keeps the full record.
        logger.info(f"Ticket {ticket_id}: progress note filtered for chat — "
                    f"raw kept in session transcript: {raw[:400]}")
    try:
        token = _api_login()
        if not token:
            return
        payload = json.dumps({"detail": _ellipsize(text)}).encode()
        req = urllib.request.Request(
            f"https://localhost/api/v1/tickets/{ticket_id}/progress",
            data=payload, headers={"Content-Type": "application/json",
                                   "Authorization": f"Bearer {token}"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5, context=SSL_CTX)
    except Exception:
        pass  # progress notes are best-effort; never fail the job for one


def _pi_fallback_config() -> "dict | None":
    """Optional on-LAN fallback for pi (Ollama/LM Studio) from the api-written
    secrets file (pi-agent can read it; .env is 0600 barenoc). Returns
    {provider, model, base_url, api_key} or None."""
    try:
        with open(PI_PROVIDER_SECRET_FILE) as f:
            d = json.load(f)
        fb = d.get("fallback") or {}
        if fb.get("provider") and fb.get("model") and fb.get("base_url"):
            return {"provider": fb["provider"], "model": fb["model"],
                    "base_url": fb["base_url"],
                    "api_key": fb.get("api_key") or "ollama"}
    except Exception:
        pass
    return None


def _ensure_pi_models_json(fb: dict) -> str:
    """Register the on-LAN provider in pi's ~/.pi/agent/models.json (merged,
    idempotent) so `pi -p --provider <fb> --model <m>` resolves the endpoint."""
    path = os.path.expanduser("~/.pi/agent/models.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}
    providers = cfg.setdefault("providers", {})
    providers[fb["provider"]] = {
        "baseUrl": fb["base_url"],
        "api": "openai-completions",
        "apiKey": fb.get("api_key") or "ollama",
        "compat": {"supportsDeveloperRole": False,
                    "supportsReasoningEffort": False},
        "models": [{"id": fb["model"], "reasoning": False}],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    return path


# Per-ticket pi-task dedup: a ticket may have at most ONE pi session running.
# The failover/retry path historically double-spawned two pi tasks for one
# ticket (08-17 incident: two 'label=fallback' lines). This set makes the
# launch idempotent per ticket while MAX_CONCURRENT still gates concurrency
# across DIFFERENT tickets.
_ACTIVE_PI_TICKETS = set()
_ACTIVE_PI_LOCK = threading.Lock()


# ── Checkpoint + rollback (agent-foresight — never a half-applied mystery) ──
# The 08-19 incident: pi timed out mid-execution after half-applying UniFi
# port_overrides (port natives cleared, overrides trimmed, the .4.x segment
# stranded). Fix: every infra change captures a checkpoint of the FULL
# before-state, and a mid-flight timeout reports "applied step N of M, rollback
# state at <path>" with the restore command — never a half-applied mystery.

CHECKPOINT_BASE = os.getenv("PI_AGENT_CHECKPOINT_DIR",
                            os.path.join(BASE, "pi-work", "checkpoints"))
RESTORE_SCRIPT = "/opt/barenoc/scripts/infra_checkpoint.py"


def _checkpoint_dir(ticket_id: str) -> str:
    return os.path.join(CHECKPOINT_BASE, (ticket_id or "unknown"))


def _checkpoint_file(ticket_id: str) -> str:
    return os.path.join(_checkpoint_dir(ticket_id), "checkpoint.json")


def _write_checkpoint(ticket_id: str, state, step=None, total=None) -> str:
    """Capture the full before-state to a checkpoint file. `state` is the
    complete port_overrides array (+ native/tagged assignments) the agent read
    BEFORE any change. Returns the checkpoint path."""
    d = _checkpoint_dir(ticket_id)
    os.makedirs(d, exist_ok=True)
    doc = {
        "ticket_id": ticket_id,
        "captured_at": datetime.datetime.utcnow().isoformat() + "Z",
        "state": state,
    }
    if step is not None:
        doc["step"] = step
    if total is not None:
        doc["total"] = total
    path = _checkpoint_file(ticket_id)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    return path


def _read_checkpoint(ticket_id: str) -> "dict | None":
    try:
        with open(_checkpoint_file(ticket_id)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _rollback_hint(ticket_id: str) -> dict:
    """What a mid-flight timeout should surface: where the agent stopped, the
    checkpoint path, and the restore command. The incident-replay shape:
    'applied step N of M, rollback state at <path>'."""
    cp = _read_checkpoint(ticket_id)
    path = _checkpoint_file(ticket_id)
    restore_cmd = f"python3 {RESTORE_SCRIPT} restore --checkpoint {path}"
    if cp:
        step = cp.get("step")
        total = cp.get("total")
        if step is not None and total is not None:
            message = f"applied step {step} of {total}, rollback state at {path}"
        elif step is not None:
            message = f"applied step {step}, rollback state at {path}"
        else:
            message = f"checkpoint captured, rollback state at {path}"
        return {"checkpoint": path, "step": step, "total": total,
                "message": message, "restore_command": restore_cmd}
    return {"checkpoint": None, "step": None, "total": None,
            "message": ("no checkpoint captured before the timeout — a human "
                        "must inspect the device state before continuing"),
            "restore_command": restore_cmd}


def _timeout_result(ticket_id: str, timeout: int) -> dict:
    """The result for a mid-flight pi timeout: where it stopped, the checkpoint
    path, and the restore command — the 08-19 incident replay shape."""
    hint = _rollback_hint(ticket_id)
    logger.warning(f"Ticket {ticket_id}: pi timed out mid-execution "
                   f"after {timeout}s; {hint['message']}")
    return {"success": False, "timed_out": True,
            "error": f"pi timed out mid-execution ({timeout}s)",
            "output": {"checkpoint": hint["checkpoint"],
                       "step": hint["step"],
                       "total": hint["total"],
                       "message": hint["message"],
                       "restore": hint["restore_command"]}}


# The INFRA-CHANGE CONTRACT — hard rules for any port/VLAN/network/switch/
# gateway action. Kept as one constant so the sysctx builder and the tests
# assert the SAME text.
INFRA_CHANGE_CONTRACT = (
    "INFRA-CHANGE CONTRACT (port/VLAN/network/switch/gateway actions — HARD RULES):\n"
    "- ENUMERATE CURRENT STATE FIRST: before ANY change, read the FULL current state — "
    "the complete port_overrides array (bash /opt/barenoc/scripts/unifi_ports.sh <switch_mac>), "
    "what is connected per port, which ports are uplinks, and where the appliance + "
    "management ride. Never guess a port's role.\n"
    "- BLAST-RADIUS REASONING: identify what could break BEFORE acting — downstream "
    "devices, the management plane, or the appliance itself. State it in the ticket work "
    "notes (auditable reasoning). Never strand the network the appliance manages.\n"
    "- PLAN -> CAPTURE-BEFORE -> VERIFIED STEPS -> ROLLBACK-ON-FAILURE:\n"
    "  1. CAPTURE the full 'before' state (the complete port_overrides array + native/tagged "
    "assignments) to the checkpoint file BEFORE any change. Never write a partial array "
    "(replace-array semantics).\n"
    "  2. Apply ONE change, then VERIFY (the device re-informs / the path stays up), then "
    "the next change.\n"
    "  3. On ANY verification failure, RESTORE the captured state automatically and report "
    "what happened.\n"
    "- NEVER change the ports carrying the appliance, the gateway uplink, or a management "
    "path without explicit reasoning AND a plan covering the fallback.\n"
    "- A hard blast-radius gate runs in the appliance API: a port VLAN change or disable "
    "that would remove the appliance's own segment or a management VLAN is refused (403) "
    "unless an admin confirms — never attempt to work around it.\n"
    "Checkpoint helper: python3 /opt/barenoc/scripts/infra_checkpoint.py capture <switch_mac> "
    "[--checkpoint DIR] [--step N] [--total M] writes the before-state; "
    "python3 /opt/barenoc/scripts/infra_checkpoint.py restore --checkpoint <path> rolls back."
)


def _checkpoint_block(checkpoint_dir: str) -> str:
    """The per-run checkpoint directory, injected into the sysctx so the agent
    writes its before-state captures where the runner can surface them."""
    if not checkpoint_dir:
        return ""
    return (f"CHECKPOINT DIRECTORY (this run): {checkpoint_dir}\n"
            "- Write every before-state capture to a file under this directory BEFORE each "
            "change (e.g. infra_checkpoint.py capture). The runner reads the newest "
            "checkpoint here if you time out and reports the rollback state.")


def _environment_digest() -> str:
    """The knowledge-layer environment digest (L1) for the sysctx. Fetches the
    compact summary text from the API; returns "" on ANY failure so a digest
    outage never blocks a pi run. No secrets are ever included in the digest."""
    try:
        token = _api_login()
        if not token:
            return ""
        req = urllib.request.Request(
            "https://localhost/api/v1/environment/summary",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = urllib.request.urlopen(req, timeout=5, context=SSL_CTX)
        data = json.loads(resp.read().decode())
        text = str(data.get("text") or "").strip()
        return f"ENVIRONMENT DIGEST:\n{text}" if text else ""
    except Exception as e:
        logger.warning(f"environment digest fetch failed: {e}")
        return ""


# ── L3 web research (opt-in egress) sysctx guidance ───────────────────────
# The pi agent already has bash; these blocks make the egress contract explicit
# and keep the runner's env gate (WEB_RESEARCH_ALLOWED) as the hard enforcement.
# Tests assert both blocks so a wording drift never silently re-enables egress.
WEB_RESEARCH_ENABLED_BLOCK = (
    "WEB RESEARCH (L3 — ENABLED for this ticket):\n"
    "- You may research the public web to ground your answer. Use ONLY the "
    "sanctioned READ-ONLY tools: `bash /opt/barenoc/scripts/web_search.sh "
    "\"<query>\" [count]` and `bash /opt/barenoc/scripts/web_fetch.sh \"<url>\"`. "
    "Search prints JSON {results:[{title,url,snippet}]}; fetch prints JSON "
    "{title,text,links}.\n"
    "- FETCH → SUMMARIZE → CITE: read what you fetch, then in your final answer "
    "summarize the findings and cite each source as a URL so the customer can "
    "verify. Never pass a search snippet off as your own knowledge without the "
    "source.\n"
    "- These tools cache results per topic/URL (the cost lever) — reuse them "
    "instead of re-fetching. They are HTTP-GET-only and SSRF-guarded (public "
    "internet only; they refuse LAN/private/appliance addresses). Do NOT work "
    "around them with curl/wget — that is never allowed.\n"
    "- If the tools fail or are blocked, say so and answer from local knowledge "
    "— never fabricate a source or a URL."
)

WEB_RESEARCH_DISABLED_BLOCK = (
    "WEB RESEARCH (L3 — DISABLED):\n"
    "- Web research is OFF for this ticket (opt-in egress). Do NOT fetch or "
    "search the public web (no curl/wget to external sites). Answer from local "
    "knowledge and the ticket context only, and say web research is not enabled "
    "if the question needs current external information."
)


def _build_sysctx(context: str = "", checkpoint_dir: str = "",
                  env_digest: str = "", web_research: bool = False) -> str:
    """The pi system context: operations guide + hard rules + ticket context.
    One function so tests can assert the script guidance and the 'not yours'
    creds line stay intact. The INFRA-CHANGE CONTRACT (agent-foresight) rides
    here so every port/VLAN/network action is plan-first + checkpointed. The
    knowledge-layer environment digest (L1) is appended when provided, and the
    L3 web-research contract is appended per the ticket's effective opt-in."""
    sysctx = (
        "You are Lily, the BareNOC network operations assistant, working an autonomous "
        "ticket session with FULL tool access (bash, file reads, the UniFi controller "
        "API, and the bundled UniFi CLI scripts under /opt/barenoc/scripts). "
        "Work autonomously and answer the ticket request directly.\n\n"
        "DEVICE OPERATIONS — USE THE SANCTIONED SCRIPTS FIRST:\n"
        "- For ANY device operation (status, updates, logs, reboot, 'the Laptop', or "
        "any device by name or IP) use the ready-made scripts under "
        "/opt/barenoc/scripts FIRST. They resolve the device, fetch + decrypt the "
        "stored SSH credentials, and handle authentication themselves. Do NOT "
        "reverse-engineer the API auth, do NOT mint an access token, and do NOT "
        "hand-roll curl/ssh against the appliance API to reach a device.\n"
        "- Main entry point: bash /opt/barenoc/scripts/device_ssh.sh <ip> <command>\n"
        "  (find the device's IP in the ticket context / device inventory by its name, "
        "then pass the IP — device_ssh.sh decrypts its stored SSH key and runs the "
        "command over SSH for you).\n"
        "- Whole-subnet sweeps: use discover.sh <subnet-or-cidr> (e.g. "
        "bash /opt/barenoc/scripts/discover.sh 192.0.2.0/24). It is parallel + "
        "capped and prints JSON of the live hosts — NEVER run a sequential "
        "`for i in ...; do ping ...; done` loop over a subnet (it hangs the session). "
        "It also never scans 100.64.0.0/10 (CGNAT/Tailscale overlay).\n"
        "- Other ready-made helpers: ping_check.sh <ip> (reachability), "
        "collect_logs.sh <ip> (system logs), reboot_device.sh, enroll_device.sh, "
        "fingerprint.sh, network_info.sh, ticket_status.sh, and the unifi_*.sh "
        "scripts for UniFi controller actions. Prefer these over ad-hoc commands.\n\n"
        "AGENT API CREDENTIALS ARE NOT YOURS:\n"
        "- The agent API service account (/opt/barenoc/agent/credentials, AGENT_TOKEN, "
        "and the https://localhost/api/v1/auth/* login endpoint) belongs to the "
        "APPLIANCE's own scripts and the job runner — NOT to you. Do not read that "
        "credentials file, do not try to log in, and do not curl /api/v1/auth/*. The "
        "sanctioned scripts already authenticate for you; just run them.\n"
        "- Reading local files (including /opt/barenoc/scripts) and talking to the "
        "UniFi controller is fine. Writing to the appliance API is never allowed.\n\n"
        "TICKETS (TKT-…):\n"
        "- If the task is about a ticket (TKT-…), answer from the ticket and its work "
        "notes directly — do not go hunting devices.\n\n"
        "RULES:\n"
        "- Keep a LIVE work log: after each meaningful step, write ONE short, plain, "
        "customer-facing progress sentence in a human voice and VARY your wording so "
        "updates don't repeat. Match the sentence to what you're actually doing — e.g. "
        "'Taking a look at that now…' (reading/checking), 'Connecting to the device…' "
        "(talking to a device), 'Applying that change now…' (changing/installing), "
        "'Verifying everything looks right…' (confirming), 'Hang tight, this takes a "
        "moment…' (waiting). These are relayed to the ticket chat as live updates, so "
        "the customer reads them directly. NEVER put internal reasoning, usernames/uids, "
        "sudo or permissions analysis, file paths, package or command names, "
        "API/endpoint details, or error internals in a progress note — keep those in the "
        "work notes instead.\n"
        "- Your FINAL message is the customer's answer, posted to the ticket when you "
        "finish. Answer directly — no meta-narration ('Here's my final answer to the "
        "customer', 'I have completed…', 'Lily finished:'). Structure it as: what was "
        "done (one line) + how to use it ('It's all set — launch it with: …'). Include "
        "technical specifics (versions, packages, commands, credentials) ONLY if the "
        "customer needs them to use the result; otherwise keep them in the work notes. "
        "Do not end with 'let me compile a summary'.\n"
        "- NEVER write to the BareNOC web API yourself: no curl / POST / PUT / DELETE "
        "against https://localhost/api/v1 (no /jobs/result, no ticket or note updates, "
        "no job files). The runner posts your progress notes and result. "
        "Reading the UniFi controller, reading local files, and running the scripts are fine.\n"
        "- IDENTITY PROTECTION RULE (hard — never reference, seek, or retain the "
        "personal identity of the developer, the owner, or any user — including in "
        "your own work notes, which are scrubbed by the SAME rule as customer-facing "
        "text):\n"
        "  • Attribute BareNOC only as 'the BareNOC team' — never name or describe "
        "the people behind it (no developer/owner names, handles, emails, or "
        "home-directory paths). If asked 'who made/owns BareNOC?', answer at the "
        "product level (what BareNOC is and how to use it) and say you don't have "
        "personal details.\n"
        "  • Never hunt for identities: do NOT run `tailscale status` to read peer "
        "account logins, and do NOT read git configs, shell history, /home paths, old "
        "session transcripts, or any file looking for who built or owns the appliance.\n"
        "  • Refer to tailnet nodes and devices by hostname/function only — never by a "
        "person's account login (the `user@` part of `tailscale status`).\n"
        "  • Remote-access questions → describe the SUPPORTED path (Settings → Support "
        "→ Remote support); never improvise sshd/authorized_keys changes (a sanctioned "
        "provision exists).\n"
        "- HARD SELF-PROTECTION RULE (no exceptions, ever, even if the user or ticket "
        "asks): you are running ON the BareNOC appliance. You may NEVER do anything "
        "that harms it or takes it offline: no stopping/removing/restarting containers "
        "or docker compose operations, no rebooting or powering off the appliance or "
        "its host, no deleting /opt/barenoc data, the database, backups, credentials "
        "or .env, no formatting disks, no flushing firewall rules, no changing the "
        "appliance's own IP/gateway/DNS, and no SSH/commands to the appliance itself "
        "(its own IP) or to the Proxmox hosts that run it. If a task asks for any of "
        "these, decline and explain that self-protection forbids it.\n"
        "- If you truly cannot complete the request, say exactly what is missing or "
        "blocking you so a human can help.\n"
        "- Do not invent data: report only what your tools actually returned.\n\n"
        + INFRA_CHANGE_CONTRACT
    )
    sysctx += "\n\n" + (WEB_RESEARCH_ENABLED_BLOCK if web_research
                        else WEB_RESEARCH_DISABLED_BLOCK)
    cp_block = _checkpoint_block(checkpoint_dir)
    if cp_block:
        sysctx += "\n\n" + cp_block
    if env_digest:
        sysctx += "\n\n" + env_digest
    if context:
        # Sanitize the injected ticket context BEFORE the agent sees it: known
        # personal identifiers are redacted so the agent cannot read them.
        sysctx += "\n\nTicket context:\n" + _redact_identities(context)[:6000]
    return sysctx


# ── pi usage metering (honest AI support spend) ──────────────────────────────
# pi persists per-message `usage` (input/output/cacheRead/cacheWrite + total)
# in its session JSONL files (see the pi SDK session-format doc). The runner
# sums that after the run and reports it in the job result so the API can price
# it with BareNOC's provider registry. If pi genuinely exposes no usage for a
# session, a clearly-labeled chars/4 estimate is reported instead — never a
# silent 0.00.


def _sum_pi_session_usage(session_dir: str, files=None) -> dict:
    """Sum pi's persisted per-message usage across the given session JSONL
    files (default: every .jsonl in `session_dir`).

    Returns a flat dict {input, output, cache_read, cache_write, reasoning} or
    {} when no usage was persisted (e.g. an ephemeral run). pi stores `usage`
    on assistant `message` entries and on compaction/branch-summary entries.
    """
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
              "reasoning": 0}
    if files is None:
        try:
            files = sorted(f for f in os.listdir(session_dir)
                           if f.endswith(".jsonl"))
        except OSError:
            return {}
    found = False
    for fn in files:
        try:
            with open(os.path.join(session_dir, fn)) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    usage = None
                    if entry.get("type") == "message":
                        msg = entry.get("message") or {}
                        if isinstance(msg, dict):
                            usage = msg.get("usage")
                    else:
                        usage = entry.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    totals["input"] += int(usage.get("input") or 0)
                    totals["output"] += int(usage.get("output") or 0)
                    totals["cache_read"] += int(usage.get("cacheRead") or 0)
                    totals["cache_write"] += int(usage.get("cacheWrite") or 0)
                    totals["reasoning"] += int(usage.get("reasoning") or 0)
                    found = True
        except OSError:
            continue
    return totals if found else {}


def _pi_usage_block(session_dir: str, task_text: str, response_text: str,
                    files=None) -> dict:
    """The job-result usage block for a pi run.

    Real summed usage when pi persisted it (metered, not an estimate);
    otherwise a documented chars/4 estimate over the task + final response,
    clearly labeled so the API/UI can mark it as an estimate. `files` scopes
    the sum to THIS run's session files so a re-dispatch never double-counts
    an earlier run's usage in the same session dir.
    """
    s = _sum_pi_session_usage(session_dir, files)
    if s:
        return {
            "input_tokens": s["input"],
            "output_tokens": s["output"],
            "cache_read_tokens": s["cache_read"],
            "cache_write_tokens": s["cache_write"],
            "total_tokens": s["input"] + s["output"] + s["cache_read"] + s["cache_write"],
            "estimated": False,
        }
    # Estimate: transcript tokens ≈ chars / 4 (the documented 08-13 fallback).
    return {
        "input_tokens": max(1, int(len(task_text or "") / 4)),
        "output_tokens": max(0, int(len(response_text or "") / 4)),
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": max(1, int(len(task_text or "") / 4)) + max(0, int(len(response_text or "") / 4)),
        "estimated": True,
        "note": "pi did not expose token usage for this session; tokens are an estimate (chars/4).",
    }


def _run_pi_task(task: str, context: str, ticket_id: str, timeout: int = 600,
                 web_research: bool = False) -> dict:
    """Run the Pi Coding Agent headlessly on a ticket task.

    Idempotent per ticket: if a pi session for this ticket is already running,
    the duplicate launch is merged (skipped) instead of spawning a second one.
    """
    with _ACTIVE_PI_LOCK:
        if ticket_id in _ACTIVE_PI_TICKETS:
            logger.warning(f"Ticket {ticket_id}: pi session already running — merging duplicate launch")
            return {"success": True, "merged": True,
                    "output": {"response": "",
                               "note": "A pi session for this ticket is already running; "
                                       "this duplicate launch was merged (no second session spawned)."}}
        _ACTIVE_PI_TICKETS.add(ticket_id)
    try:
        return _run_pi_task_impl(task, context, ticket_id, timeout,
                                 web_research=web_research)
    finally:
        with _ACTIVE_PI_LOCK:
            _ACTIVE_PI_TICKETS.discard(ticket_id)


def _run_pi_task_impl(task: str, context: str, ticket_id: str, timeout: int = 600,
                      web_research: bool = False) -> dict:
    """Implementation: uses `pi -p` (non-interactive) — task + context passed as
    system context, final response captured on stdout. Runs as pi-agent with the
    provider/model/api-key read live from BareNOC's .env. While the agent works,
    its session transcript is polled and brief assistant messages are relayed to
    the ticket as LIVE work notes. web_research=True enables the read-only L3
    web tools (gated again here via WEB_RESEARCH_ALLOWED in the child env)."""
    pi_bin = os.getenv("PI_AGENT_BIN", "/home/pi-agent/.local/share/pi-node/current/bin/pi")
    pi_dir = os.path.dirname(os.path.dirname(pi_bin))
    workdir = os.getenv("PI_AGENT_WORKDIR", "/opt/barenoc/pi-work")
    os.makedirs(workdir, exist_ok=True)
    session_dir = os.path.join(workdir, "sessions", ticket_id)
    os.makedirs(session_dir, exist_ok=True)
    cfg = _pi_provider_config()
    # Local-only egress (compliance): register the on-LAN endpoint in pi's
    # models.json so `pi -p --provider <local> --model <m>` resolves it.
    if cfg.get("base_url"):
        _ensure_pi_models_json({"provider": cfg["provider"], "model": cfg["model"],
                                "base_url": cfg["base_url"],
                                "api_key": cfg.get("api_key") or "ollama"})
    cdir = _checkpoint_dir(ticket_id)
    os.makedirs(cdir, exist_ok=True)
    sysctx = _build_sysctx(context, checkpoint_dir=cdir,
                           env_digest=_environment_digest(),
                           web_research=web_research)

    def run_once(provider, model, api_key, session_subdir, label):
        sdir = os.path.join(workdir, "sessions", session_subdir)
        os.makedirs(sdir, exist_ok=True)
        # Snapshot pre-existing session files so the usage sum is scoped to
        # THIS run — a ticket re-dispatched later reuses the same session dir,
        # and summing stale files would double-count an earlier run's tokens.
        pre_files = set(f for f in os.listdir(sdir) if f.endswith(".jsonl"))
        cmd = [pi_bin, "-p", "--provider", provider, "--model", model,
               "--api-key", api_key, "--thinking", "off",
               "--session-dir", sdir,
               "--append-system-prompt", sysctx, _redact_identities(task)[:8000]]
        env = dict(os.environ)
        env["PATH"] = f"{pi_dir}/bin:" + env.get("PATH", "")   # so `node` resolves for pi
        # L3 egress hard gate: the web tools refuse to network unless the
        # runner opted this ticket in. Guidance is not the gate — this is.
        env["WEB_RESEARCH_ALLOWED"] = "1" if web_research else "0"
        logger.info(f"PI task started (ticket {ticket_id}, provider={provider}/{model}, label={label}, timeout {timeout}s)")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, env=env)
        except Exception as e:
            return {"success": False, "error": f"pi could not start: {e}"}

        # Live progress: poll the session transcript while pi works.
        posted = set()
        pending = []   # (message_id, snippet) awaiting the rate-limit gap
        last_ts = 0.0
        cap = 15
        tone = _ProgressTone(ticket_id)
        deadline = time.time() + timeout
        timed_out = False
        while time.time() < deadline and proc.poll() is None:
            now = time.time()
            try:
                files = sorted(f for f in os.listdir(sdir) if f.endswith(".jsonl"))
                if files:
                    newest = os.path.join(sdir, files[-1])
                    with open(newest) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                m = json.loads(line)
                            except Exception:
                                continue
                            if m.get("type") != "message":
                                continue
                            msg = m.get("message") or {}
                            if msg.get("role") != "assistant":
                                continue
                            mid = m.get("id")
                            if mid in posted or any(p[0] == mid for p in pending):
                                continue
                            # Only post COMPLETE messages (terminal stopReason);
                            # a still-streaming message must never appear as a
                            # mid-word fragment. The full text is kept flat (all
                            # lines) and capped at PROGRESS_NOTE_MAX_CHARS with
                            # an ellipsis when truncated.
                            snippet = _ellipsize(_assistant_complete_text(msg) or "")
                            if snippet:
                                pending.append((mid, snippet))
                # Post at most one note per poll cycle, min 8s apart.
                if pending and now - last_ts >= 8 and len(posted) < cap:
                    mid, snippet = pending.pop(0)
                    _post_progress(ticket_id, snippet, tone)
                    posted.add(mid)
                    last_ts = now
                    tone.last_note_at = now
                if len(posted) >= cap:
                    pending = []
                # Elapsed-time heartbeat: a long task with no distinct activity
                # must not look hung. Heartbeats don't consume the real-note cap.
                if len(posted) < cap and now - last_ts >= 8:
                    hb = tone.heartbeat(now)
                    if hb:
                        _post_progress(ticket_id, hb)
                        last_ts = now
            except Exception:
                pass
            time.sleep(4)

        # A still-running proc once the deadline has passed = a mid-flight
        # timeout (the 08-19 incident). The checkpoint + rollback state must
        # surface instead of a half-applied mystery.
        if proc.poll() is None and time.time() >= deadline:
            timed_out = True
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            timed_out = True
        out = (out or "").strip()
        err = (err or "").strip()
        session_files = [f for f in os.listdir(sdir)
                         if f.endswith(".jsonl") and f not in pre_files]
        usage = _pi_usage_block(sdir, task, out, files=session_files)
        if proc.returncode == 0 and out:
            return {"success": True, "output": {"response": out[:20000],
                                                "usage": usage,
                                                "model": model,
                                                "provider": provider}}
        if timed_out:
            tr = _timeout_result(ticket_id, timeout)
            tr.setdefault("output", {})["usage"] = usage
            tr["output"]["model"] = model
            tr["output"]["provider"] = provider
            return tr
        return {"success": False,
                "error": f"pi exited {proc.returncode}: {err[:500] or out[:500] or 'no output'}",
                "output": {"usage": usage, "model": model, "provider": provider}}

    result = run_once(cfg["provider"], cfg["model"], cfg["api_key"], ticket_id, "primary")
    if result["success"]:
        return result
    fb = _pi_fallback_config()
    if not fb:
        return result
    logger.warning(f"Ticket {ticket_id}: primary pi provider {cfg['provider']} failed "
                   f"({result.get('error', '')[:120]}) — failing over to {fb['provider']}/{fb['model']}")
    _post_progress(ticket_id, "Hang tight — switching to a backup helper, one moment…")
    _ensure_pi_models_json(fb)
    fb_result = run_once(fb["provider"], fb["model"], fb.get("api_key", "ollama"),
                         f"{ticket_id}-fb", "fallback")
    if fb_result["success"]:
        resp = (fb_result.get("output", {}) or {}).get("response", "")
        fb_result["output"] = {
            "response": resp,
            "note": (f"answered via on-LAN fallback {fb['provider']}/{fb['model']} "
                     f"(primary {cfg['provider']} failed: {result.get('error', '')[:200]})"),
        }
    return fb_result


def _run_job_thread(filename: str, job: dict) -> None:
    """Execute one job + write its result + clean up. Runs in a worker thread
    so up to MAX_CONCURRENT jobs execute in parallel (a long pi task no longer
    blocks verify/claim/discover)."""
    try:
        result = execute_job(job)
        if result.get("merged"):
            # A pi session for this ticket was already running — this job is a
            # duplicate; drop it silently (the active run owns the result).
            logger.info(f"Job {filename} merged with an active pi session — no result posted")
        else:
            write_result(job, result)
        try:
            os.remove(os.path.join(JOBS_RUNNING, filename))
        except OSError:
            pass
        logger.info(f"Job {filename} completed (success={result['success']})")
    except Exception:
        logger.exception(f"Job {filename} thread failed")
        try:
            os.remove(os.path.join(JOBS_RUNNING, filename))
        except OSError:
            pass


def execute_job(job: dict) -> dict:
    """Execute a job (or a batch of sub-jobs) and return the result."""
    action = job["action"]
    raw_target = job.get("target", "")
    params = job.get("params", {})
    ticket_id = job.get("ticket_id", "unknown")
    if action == "pi_task":
        task = str(params.get("task", ""))
        context = str(params.get("context", ""))
        timeout = int(params.get("timeout_s", 600))
        if not task:
            return {"success": False, "error": "pi_task requires a task"}
        # SELF-PROTECTION: never dispatch anything that could harm the appliance
        # or take it offline — the one clause with no override.
        bad = _self_blocked(task) or _self_blocked(context)
        if bad:
            logger.warning(f"Self-protection blocked pi task (ticket {ticket_id}): {bad}")
            return {"success": False, "error": "blocked",
                    "output": {"blocked": "self-protection", "reason": bad,
                                "note": "The appliance may never harm itself or take itself offline — no profile overrides this."}}
        # L3 research: opt-in egress only — per-ticket flag AND the deployment
        # toggle (both re-checked here so a hand-crafted job can't widen it).
        web_research = bool(params.get("web_research")) and _web_research_enabled()
        return _run_pi_task(task, context, ticket_id, timeout,
                            web_research=web_research)
    # UniFi MAC-target actions (port/restart) keep the RAW target so the
    # name->MAC/uplink resolution happens in _build_cmd — pre-resolving to an
    # IP here would defeat it.
    if action in _MAC_TARGET_ACTIONS:
        target = raw_target
    else:
        target = resolve_target(raw_target)

    # SELF-PROTECTION: never act ON the appliance itself, even if the worker
    # or pi resolved a name to its own IP (SSH actions would reach its shell).
    if action in ("reboot_device", "apply_patch", "collect_logs", "snmp_poll",
                  "ping_test", "fingerprint_device", "windows_diag",
                  "windows_cleanup") and _self_target(target):
        logger.warning(f"Self-protection blocked target {target} (ticket {ticket_id})")
        return {"success": False, "error": "blocked",
                "output": {"blocked": "self-protection", "reason": "target is the appliance itself",
                            "note": "The appliance may never be the target of its own actions."}}

    # batch: run each sub-job through the same pipeline, report per-item results
    if action == "batch":
        jobs = params.get("jobs") if isinstance(params.get("jobs"), list) else []
        results = []
        for sj in jobs:
            sub = dict(sj)
            sub_target = resolve_target(sub.get("target", ""))
            res = _run_single(str(sub.get("action", "")), sub_target,
                              sub.get("params") or {}, ticket_id,
                              env_tz=job.get("tz", ""))
            res["action"] = sub.get("action")
            res["target"] = sub_target
            results.append(res)
        ok = sum(1 for r in results if r.get("success"))
        return {
            "success": ok == len(results) and len(results) > 0,
            "output": {
                "results": results,
                "total": len(results),
                "succeeded": ok,
                "failed": len(results) - ok,
            },
            "target": target,
            "action": "batch",
        }

    return _run_single(action, target, params, ticket_id, env_tz=job.get("tz", ""))


def _post_jobs_result(ticket_id: str, payload: dict) -> bool:
    """POST the job result to /jobs/result, the one call that must not be
    lost (a dropped result -> ticket stuck in_progress -> watchdog false
    escalation). Retries 429/5xx with backoff; on 401 re-logs-in ONCE then
    surfaces the error. Returns True on HTTP 200, False otherwise."""
    body = json.dumps(payload).encode()
    token = _api_login()
    relogged = False
    attempt = 0
    while True:
        if not token:
            return False
        try:
            req = urllib.request.Request(
                "https://localhost/api/v1/jobs/result",
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5, context=SSL_CTX)
            return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 401 and not relogged:
                # Token expired/revoked — re-login once (bypassing the cache)
                # and retry. A second 401 surfaces as a failure.
                relogged = True
                token = _api_login(force=True)
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                attempt += 1
                _sleep(_retry_backoff(attempt - 1))
                continue
            logger.error(f"Could not update ticket {ticket_id}: HTTP {e.code}")
            return False
        except Exception as e:
            if attempt < 3:
                attempt += 1
                _sleep(_retry_backoff(attempt - 1))
                continue
            logger.error(f"Could not update ticket {ticket_id}: {e}")
            return False


def write_result(job: dict, result: dict):
    """Write execution result to completed/ and update ticket."""
    ticket_id = job.get("ticket_id", "unknown")
    timestamp = datetime.datetime.utcnow().isoformat()

    result_file = {
        "ticket_id": ticket_id,
        "action": job.get("action"),
        "target": job.get("target"),
        "executed_at": timestamp,
        "result": result,
    }

    # Write to completed
    filename = f"{ticket_id}-result.json"
    filepath = os.path.join(JOBS_COMPLETED, filename)
    with open(filepath, "w") as f:
        json.dump(result_file, f, indent=2)

    logger.info(f"Result written: {filepath}")

    # Update ticket via API (using stdlib, no external deps). The result POST
    # is the one call that must not be lost: a 429/401 here silently orphans a
    # finished job and the watchdog falsely escalates it, so it gets its own
    # retry/backoff + re-login-once path.
    payload = {
        "ticket_id": ticket_id,
        "action": job.get("action"),
        "target": job.get("target"),
        "success": result["success"],
        "output": result.get("output", {}),
        "error": result.get("error"),
    }
    if _post_jobs_result(ticket_id, payload):
        logger.info(f"Ticket {ticket_id} updated via API")

    # Handle callback (e.g., device verification)
    callback = job.get("_callback")
    if callback:
        _handle_callback(callback, result)


def _handle_callback(callback: dict, result: dict):
    """Handle post-execution callbacks."""
    ctype = callback.get("type")
    if ctype == "verify_device":
        device_id = callback.get("device_id")
        if device_id:
            is_online = result.get("success", False) and result.get("output", {}).get("reachable", False)
            status = "online" if is_online else "unreachable"
            try:
                # Get agent service token
                token = _api_login()
                if not token:
                    raise RuntimeError("agent login failed")

                # Update device status + poll data
                patch_body = {"status": status}
                if result.get("output"):
                    patch_body["last_poll_data"] = {"ping": result["output"]}
                patch_data = json.dumps(patch_body).encode()
                patch_req = urllib.request.Request(
                    f"https://localhost/api/v1/devices/{device_id}",
                    data=patch_data,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                    method="PATCH",
                )
                urllib.request.urlopen(patch_req, timeout=5, context=SSL_CTX)
                logger.info(f"Device {device_id} status updated to {status}")
            except Exception as e:
                logger.error(f"Device status update failed: {e}")
    elif ctype == "fingerprint_store":
        device_id = callback.get("device_id")
        if device_id and result.get("success"):
            output = result.get("output", {}) or {}
            patch_body = {"fingerprint": output}
            if output.get("vendor"):
                patch_body["vendor"] = output["vendor"]
            try:
                token = _api_login()
                patch_data = json.dumps(patch_body).encode()
                patch_req = urllib.request.Request(
                    f"https://localhost/api/v1/devices/{device_id}", data=patch_data,
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {token}"},
                    method="PATCH",
                )
                urllib.request.urlopen(patch_req, timeout=5, context=SSL_CTX)
                logger.info(f"Fingerprint stored for device {device_id}")
            except Exception as e:
                logger.error(f"Fingerprint store failed for {device_id}: {e}")
        elif not result.get("success"):
            logger.warning(f"Fingerprint job failed for device {device_id}: "
                           f"{result.get('error') or result.get('output', {}).get('error')}")
    elif ctype == "discover_add":
        ip = callback.get("ip")
        is_online = result.get("success", False) and result.get("output", {}).get("reachable", False)
        if is_online and ip:
            try:
                token = _api_login()

                # Match-before-insert + self-exclusion live on the API side
                # (discover-results) so repeated scans UPDATE the same record
                # instead of INSERTing duplicates, and the appliance's own IP
                # is never recorded.
                add_data = json.dumps({"found": [{"ip": ip}]}).encode()
                add_req = urllib.request.Request(
                    "https://localhost/api/v1/devices/discover-results", data=add_data,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                    method="POST",
                )
                urllib.request.urlopen(add_req, timeout=5, context=SSL_CTX)
                logger.info(f"Discovery recorded device {ip}")
            except Exception as e:
                logger.error(f"Discovery add failed for {ip}: {e}")
    elif ctype == "snmp_store":
        found = (result.get("output") or {}).get("found") or []
        if found:
            try:
                token = _api_login()
                req = urllib.request.Request(
                    "https://localhost/api/v1/devices/snmp-sweep-results",
                    data=json.dumps({"found": found}).encode(),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=8, context=SSL_CTX)
                logger.info(f"SNMP sweep stored {len(found)} devices")
            except Exception as e:
                logger.error(f"SNMP sweep store failed: {e}")


def _ticket_status(ticket_id: str) -> str:
    """Fetch a ticket's status via the API (approval gate). "" on error."""
    token = _api_login()
    if not token:
        return ""
    try:
        req = urllib.request.Request(
            f"https://localhost/api/v1/tickets/{ticket_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = urllib.request.urlopen(req, timeout=5, context=SSL_CTX)
        return json.loads(resp.read().decode()).get("status", "")
    except Exception:
        return ""


def _approval_allowed(status: str) -> bool:
    """Approval gate: held jobs run only after the human approves the ticket
    (status -> in_progress). Closed/rejected/failed = never run."""
    if status in ("closed", "rejected", "failed"):
        return False
    return status == "in_progress"


def run():
    """Main agent loop."""
    logger.info("Pi Agent Runner starting...")

    # Ensure directories exist
    for d in [JOBS_INCOMING, JOBS_RUNNING, JOBS_COMPLETED, LOGS_DIR]:
        os.makedirs(d, exist_ok=True)

    # Startup recovery: jobs stranded in running/ (e.g. by a restart mid-job)
    # are re-queued so they finish instead of orphaning their ticket forever.
    for f in sorted(os.listdir(JOBS_RUNNING)):
        if f.endswith(".json"):
            src = os.path.join(JOBS_RUNNING, f)
            dst = os.path.join(JOBS_INCOMING, f)
            try:
                shutil.move(src, dst)
                logger.info(f"Recovered stranded job {f} -> incoming")
            except Exception as e:
                logger.error(f"Could not recover job {f}: {e}")

    load_managed_devices()
    active_jobs = {}   # filename -> {"job": dict, "future": Future | None}
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT,
                                  thread_name_prefix="job")

    while True:
        try:
            # Reload devices periodically
            if len(active_jobs) < MAX_CONCURRENT:
                load_managed_devices()

            # Check incoming
            # Priority queue: verify/claim jobs first, then discovery, then rest
            all_files = os.listdir(JOBS_INCOMING)
            priority = sorted(f for f in all_files if f.startswith(('verify-', 'claim-'))) + \
                       sorted(f for f in all_files if f.startswith('discover-')) + \
                       sorted(f for f in all_files if not f.startswith(('verify-', 'claim-', 'discover-')))
            incoming = priority

            for filename in incoming:
                if len(active_jobs) >= MAX_CONCURRENT:
                    break

                if not filename.endswith(".json"):
                    continue

                src = os.path.join(JOBS_INCOMING, filename)

                try:
                    with open(src) as f:
                        job = json.load(f)
                except (json.JSONDecodeError, IOError) as e:
                    logger.error(f"Invalid job file {filename}: {e}")
                    os.rename(src, os.path.join(JOBS_COMPLETED, f"error-{filename}"))
                    continue

                # Approval gate: jobs marked requires_approval run only after
                # the human approves the ticket (status -> in_progress).
                if job.get("requires_approval"):
                    status = _ticket_status(job.get("ticket_id", ""))
                    if not _approval_allowed(status):
                        if status in ("closed", "rejected", "failed"):
                            logger.info(f"Job {filename} not approved (ticket {status}) — dropping")
                            write_result(job, {"success": False,
                                               "error": f"Not approved (ticket {status})"})
                            os.remove(src)
                        else:
                            logger.info(f"Job {filename} awaiting approval — holding")
                        continue
                    logger.info(f"Job {filename} approved — executing")

                # Validate
                valid, msg = validate_job(job)
                if not valid:
                    logger.warning(f"Job {filename} rejected: {msg}")
                    result = {"success": False, "error": msg}
                    write_result(job, result)
                    os.remove(src)
                    continue

                # Per-ticket pi-task dedup: never start a second pi session for
                # a ticket that already has one running (the 08-17 double-spawn
                # lesson). Drop the duplicate — the active run owns the ticket.
                if job.get("action") == "pi_task":
                    tid = job.get("ticket_id", "")
                    with _ACTIVE_PI_LOCK:
                        if tid in _ACTIVE_PI_TICKETS:
                            logger.warning(f"Job {filename}: pi session for ticket {tid} already active — merging (skipping)")
                            os.remove(src)
                            continue

                # Move to running
                dst = os.path.join(JOBS_RUNNING, filename)
                shutil.move(src, dst)

                active_jobs[filename] = {"job": job, "future": None}
                logger.info(f"Job {filename} started (active: {len(active_jobs)})")

            # Process active jobs — worker-thread pool, up to MAX_CONCURRENT
            # running in parallel. Submit once, reap when done.
            for filename, entry in list(active_jobs.items()):
                if entry["future"] is None:
                    entry["future"] = executor.submit(
                        _run_job_thread, filename, entry["job"])
                    logger.info(f"Job {filename} running (active: {len(active_jobs)})")
                elif entry["future"].done():
                    try:
                        entry["future"].result()
                    except Exception:
                        pass  # already logged in the worker thread
                    active_jobs.pop(filename, None)

        except Exception as e:
            logger.exception(f"Agent loop error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
