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
import urllib.request
import urllib.error
import ssl

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


def _api_login() -> str:
    """Log in as the agent service account; returns an access token (or "")."""
    creds = _api_credentials()
    if not creds:
        return ""
    try:
        login_data = json.dumps({"username": creds["username"],
                                 "password": creds["password"]}).encode()
        login_req = urllib.request.Request(
            "https://localhost/api/v1/auth/login",
            data=login_data, headers={"Content-Type": "application/json"},
            method="POST",
        )
        login_resp = urllib.request.urlopen(login_req, timeout=5, context=SSL_CTX)
        return json.loads(login_resp.read().decode()).get("access_token", "")
    except Exception as e:
        logger.error(f"Agent API login failed: {e}")
        return ""

# Config
POLL_INTERVAL = 3  # seconds
MAX_CONCURRENT = 2
JOB_TIMEOUT = 300  # seconds (install_chat_client needs time for remote package installs)
MAX_RETRIES = 3

# Allowed actions mapped to their scripts
ACTION_SCRIPTS = {
    "ping_test": "ping_check.sh",
    "snmp_poll": "snmp_poll.sh",
    "device_status": "ping_check.sh",  # Use ping as basic status check
    "reboot_device": "reboot_device.sh",
    "apply_patch": "patch_debian.sh",
    "collect_logs": "collect_logs.sh",
    "network_discovery": "discover.sh",
    "network_info": "network_info.sh",
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
        MANAGED_DEVICES.clear()
        MANAGED_MACS.clear()
        DEVICE_BY_IP.clear()
        for d in devices:
            MANAGED_DEVICES[d["name"]] = d["ip_address"]
            MANAGED_DEVICES[d["ip_address"]] = d["ip_address"]
            if d.get("hostname"):
                MANAGED_DEVICES[d["hostname"]] = d["ip_address"]
            DEVICE_BY_IP[d["ip_address"]] = d["id"]
            # MAC passthrough — UniFi port actions target the switch MAC
            if d.get("mac_address"):
                MANAGED_DEVICES[d["mac_address"]] = d["mac_address"]
                MANAGED_MACS[d["name"]] = d["mac_address"]
                MANAGED_MACS[d["ip_address"]] = d["mac_address"]  # IP targets resolve too
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
                with open(key_path, "w") as f:
                    f.write(creds["ssh_key"])
                os.chmod(key_path, 0o600)
                ssh_key = key_path
    return ssh_user or "root", ssh_key or DEFAULT_SSH_KEY


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
    the topology endpoint — so 'tag the uplink of U7 Outdoor' targets the right
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
    of U7 Outdoor' targets the right switch port. Returns (switch_mac, port_idx)."""
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
    if action == "unifi_set_ssid_password":
        return ["bash", script_path, str(params.get("ssid", "")),
                str(params.get("password", ""))]
    if action == "unifi_network_create":
        # name + vlan required; subnet/dhcp optional (script defaults them)
        return ["bash", script_path, str(params.get("name", "")),
                str(params.get("vlan", "")),
                str(params.get("subnet", "")),
                str(params.get("dhcp", "true"))]
    if action in ("network_info", "unifi_firewall_rules", "unifi_ensure_wireless_uplinks"):
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
    return ["bash", script_path, target]


def _run_single(action: str, target: str, params: dict, ticket_id: str) -> dict:
    """Run ONE action script and parse its JSON output."""
    script = ACTION_SCRIPTS.get(action)
    if not script:
        return {"success": False, "error": f"No script for action: {action}"}
    script_path = os.path.join(SCRIPTS_DIR, script)
    if not os.path.exists(script_path):
        return {"success": False, "error": f"Script not found: {script_path}"}
    cmd = _build_cmd(action, target, params)
    logger.info(f"Executing: {' '.join(cmd[:4])}... (ticket {ticket_id})")
    try:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=JOB_TIMEOUT)
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


def _pi_provider_config() -> dict:
    """Provider/model/api-key for pi, straight from BareNOC's Settings.
    Prefers the API-written secrets file (/opt/barenoc/volumes/secrets/
    llm_provider.json — kept in sync on every settings save), falls back to
    reading .env. Maps the BareNOC provider block to a provider pi recognizes."""
    try:
        with open("/opt/barenoc/volumes/secrets/llm_provider.json") as f:
            d = json.load(f)
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


def _run_pi_task(task: str, context: str, ticket_id: str, timeout: int = 600) -> dict:
    """Run the Pi Coding Agent headlessly on a ticket task.

    Uses `pi -p` (non-interactive): the task + context are passed as system
    context and the final response is captured on stdout. Runs as the pi-agent
    user with the provider/model/api-key read live from BareNOC's .env.
    """
    pi_bin = os.getenv("PI_AGENT_BIN", "/home/pi-agent/.local/share/pi-node/current/bin/pi")
    pi_dir = os.path.dirname(os.path.dirname(pi_bin))
    workdir = os.getenv("PI_AGENT_WORKDIR", "/opt/barenoc/pi-work")
    os.makedirs(workdir, exist_ok=True)
    session_dir = os.path.join(workdir, "sessions", ticket_id)
    os.makedirs(session_dir, exist_ok=True)
    cfg = _pi_provider_config()
    sysctx = (
        "You are Lily, the BareNOC network operations assistant, working an autonomous "
        "ticket session with FULL tool access (bash, file reads, the UniFi controller "
        "API, and the bundled UniFi CLI scripts under /opt/barenoc/scripts). "
        "Work autonomously and answer the ticket request directly.\n\n"
        "RULES:\n"
        "- Answer the request directly and concisely: a few sentences plus any concrete "
        "findings (values, names, IPs). Do NOT narrate your process and never end with "
        "'let me compile a summary' — your final message IS the customer answer and is "
        "posted to the ticket automatically by the runner.\n"
        "- NEVER write to the BareNOC web API yourself: no curl / POST / PUT / DELETE "
        "against https://localhost/api/v1 (no /jobs/result, no ticket or note updates, "
        "no job files). The runner posts your result to the ticket when you finish. "
        "Reading the UniFi controller, reading local files, and running the scripts are fine.\n"
        "- If you truly cannot complete the request, say exactly what is missing or "
        "blocking you so a human can help.\n"
        "- Do not invent data: report only what your tools actually returned."
    )
    if context:
        sysctx += "\n\nTicket context:\n" + context[:6000]
    cmd = [pi_bin, "-p", "--provider", cfg["provider"], "--model", cfg["model"],
           "--api-key", cfg["api_key"], "--thinking", "off",
           "--session-dir", session_dir,
           "--append-system-prompt", sysctx, task[:8000]]
    env = dict(os.environ)
    env["PATH"] = f"{pi_dir}/bin:" + env.get("PATH", "")   # so `node` resolves for pi
    logger.info(f"PI task started (ticket {ticket_id}, provider={cfg['provider']}/{cfg['model']}, timeout {timeout}s)")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 0 and out:
            return {"success": True, "output": {"response": out[:20000]}}
        return {"success": False,
                "error": f"pi exited {proc.returncode}: {err[:500] or out[:500] or 'no output'}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"pi timed out after {timeout}s"}


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
        return _run_pi_task(task, context, ticket_id, timeout)
    # UniFi MAC-target actions (port/restart) keep the RAW target so the
    # name->MAC/uplink resolution happens in _build_cmd — pre-resolving to an
    # IP here would defeat it.
    if action in _MAC_TARGET_ACTIONS:
        target = raw_target
    else:
        target = resolve_target(raw_target)

    # batch: run each sub-job through the same pipeline, report per-item results
    if action == "batch":
        jobs = params.get("jobs") if isinstance(params.get("jobs"), list) else []
        results = []
        for sj in jobs:
            sub = dict(sj)
            sub_target = resolve_target(sub.get("target", ""))
            res = _run_single(str(sub.get("action", "")), sub_target,
                              sub.get("params") or {}, ticket_id)
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

    return _run_single(action, target, params, ticket_id)


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

    # Update ticket via API (using stdlib, no external deps)
    try:
        token = _api_login()
        payload_bytes = json.dumps({
            "ticket_id": ticket_id,
            "action": job.get("action"),
            "target": job.get("target"),
            "success": result["success"],
            "output": result.get("output", {}),
            "error": result.get("error"),
        }).encode()
        req = urllib.request.Request(
            "https://localhost/api/v1/jobs/result",
            data=payload_bytes,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5, context=SSL_CTX)
        if resp.status == 200:
            logger.info(f"Ticket {ticket_id} updated via API")
        else:
            logger.error(f"API update failed: {resp.status}")
    except Exception as e:
        logger.error(f"Could not update ticket {ticket_id}: {e}")

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

                add_data = json.dumps({"name": f"discovered-{ip.replace('.', '-')}", "ip_address": ip, "device_type": "unknown", "claimed": False}).encode()
                add_req = urllib.request.Request(
                    "https://localhost/api/v1/devices", data=add_data,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                    method="POST",
                )
                urllib.request.urlopen(add_req, timeout=5, context=SSL_CTX)
                logger.info(f"Discovery added device {ip}")
            except Exception as e:
                logger.error(f"Discovery add failed for {ip}: {e}")


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

    load_managed_devices()
    active_jobs = {}

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

                # Move to running
                dst = os.path.join(JOBS_RUNNING, filename)
                shutil.move(src, dst)

                active_jobs[filename] = job
                logger.info(f"Job {filename} started (active: {len(active_jobs)})")

            # Process active jobs
            completed = []
            for filename, job in list(active_jobs.items()):
                src = os.path.join(JOBS_RUNNING, filename)
                if not os.path.exists(src):
                    completed.append(filename)
                    continue

                result = execute_job(job)
                write_result(job, result)

                # Remove from running
                os.remove(src)
                completed.append(filename)
                logger.info(f"Job {filename} completed (success={result['success']})")

            for f in completed:
                active_jobs.pop(f, None)

        except Exception as e:
            logger.exception(f"Agent loop error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
