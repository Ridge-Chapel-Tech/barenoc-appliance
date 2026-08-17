import enum
import os
import re
from typing import Optional


class AllowedAction(str, enum.Enum):
    PING_TEST = "ping_test"
    SNMP_POLL = "snmp_poll"
    DEVICE_STATUS = "device_status"
    APPLY_PATCH = "apply_patch"
    REBOOT_DEVICE = "reboot_device"
    COLLECT_LOGS = "collect_logs"
    NETWORK_DISCOVERY = "network_discovery"
    NETWORK_INFO = "network_info"  # logical: AI Tech answers network/VLAN queries from UniFi
    SYSTEM_TIME = "system_time"  # read-only: appliance current time + timezone
    TICKET_STATUS = "ticket_status"  # read-only: live status of a ticket by TKT-… id
    UNIFI_CLIENTS = "unifi_clients"  # read-only: who is online (UniFi)
    UNIFI_DEVICES = "unifi_devices"  # read-only: device health/uptime (UniFi)
    UNIFI_PORTS = "unifi_ports"      # read-only: switch port table (UniFi)
    UNIFI_PORT_CONFIG = "unifi_port_config"  # WRITE: assign native/tagged VLANs to a UniFi switch port
    UNIFI_CLIENT_PORT = "unifi_client_port"  # read-only: which switch port a client is on
    UNIFI_FIREWALL_RULES = "unifi_firewall_rules"  # read-only: custom firewall rules (UniFi)
    UNIFI_RESTART = "unifi_restart"    # WRITE: reboot a UniFi-managed device via the controller
    UNIFI_PORT_BOUNCE = "unifi_port_bounce"  # WRITE: cycle a switch port (disable->enable)
    UNIFI_PORT_RENAME = "unifi_port_rename"  # WRITE: rename a switch port
    UNIFI_ENSURE_WIRELESS_UPLINKS = "unifi_ensure_wireless_uplinks"  # WRITE: tag all enabled wireless VLANs on all AP uplinks
    UNIFI_SET_SSID_PASSWORD = "unifi_set_ssid_password"  # WRITE: change a Wi-Fi SSID passphrase
    UNIFI_NETWORK_CREATE = "unifi_network_create"  # WRITE: create a new VLAN/subnet on the controller
    PI_TASK = "pi_task"                # WRITE: run the local Pi Coding Agent on an open-ended task
    BATCH = "batch"                    # WRITE: run multiple sub-jobs (params.jobs list)
    FINGERPRINT_DEVICE = "fingerprint_device"
    INSTALL_CHAT_CLIENT = "install_chat_client"
    ENROLL_DEVICE = "enroll_device"  # WRITE: adopt a Linux device with a step-ca cert (SSH transport)
    SNMP_SWEEP = "snmp_sweep"          # WRITE: discover + identify SNMP gear across subnets
    COMPLETE_TICKET = "complete_ticket"  # logical action: close on customer confirmation
    REQUEST_CUSTOMER_INPUT = "request_customer_input"  # logical: ask customer for info → customer_action
    ESCALATE_HUMAN = "escalate_human"


# Predefined scripts that map to each action
ACTION_SCRIPTS = {
    AllowedAction.PING_TEST: "scripts/ping_check.sh",
    AllowedAction.SNMP_POLL: "scripts/snmp_poll.sh",
    AllowedAction.DEVICE_STATUS: "scripts/ping_check.sh",  # status = ping reachability
    AllowedAction.APPLY_PATCH: "scripts/apply_patch.sh",
    AllowedAction.REBOOT_DEVICE: "scripts/reboot_device.sh",
    AllowedAction.COLLECT_LOGS: "scripts/collect_logs.sh",
    AllowedAction.NETWORK_DISCOVERY: "scripts/discover.sh",
    AllowedAction.NETWORK_INFO: "scripts/network_info.sh",
    AllowedAction.SYSTEM_TIME: "scripts/system_time.sh",
    AllowedAction.TICKET_STATUS: "scripts/ticket_status.sh",
    AllowedAction.UNIFI_CLIENTS: "scripts/unifi_clients.sh",
    AllowedAction.UNIFI_DEVICES: "scripts/unifi_devices.sh",
    AllowedAction.UNIFI_PORTS: "scripts/unifi_ports.sh",
    AllowedAction.UNIFI_PORT_CONFIG: "scripts/unifi_port.sh",
    AllowedAction.UNIFI_CLIENT_PORT: "scripts/unifi_client_port.sh",
    AllowedAction.UNIFI_FIREWALL_RULES: "scripts/unifi_firewall_rules.sh",
    AllowedAction.UNIFI_RESTART: "scripts/unifi_restart.sh",
    AllowedAction.UNIFI_PORT_BOUNCE: "scripts/unifi_port_bounce.sh",
    AllowedAction.UNIFI_PORT_RENAME: "scripts/unifi_port_rename.sh",
    AllowedAction.UNIFI_ENSURE_WIRELESS_UPLINKS: "scripts/unifi_ensure_wireless_uplinks.sh",
    AllowedAction.UNIFI_SET_SSID_PASSWORD: "scripts/unifi_set_ssid_password.sh",
    AllowedAction.UNIFI_NETWORK_CREATE: "scripts/unifi_network_create.sh",
    AllowedAction.FINGERPRINT_DEVICE: "scripts/fingerprint.sh",
    AllowedAction.INSTALL_CHAT_CLIENT: "scripts/install_chat_client.sh",
    AllowedAction.ENROLL_DEVICE: "scripts/enroll_device.sh",
    AllowedAction.SNMP_SWEEP: "scripts/snmp_sweep.sh",
    # ESCALATE_HUMAN has no script — it's a logical action
}

# Managed devices (populated from DB at runtime)
# DEVICES dict is populated by the worker before validation
MANAGED_DEVICES: dict = {}

# Patch allowlist — only patches on this list can be applied.
# Per-deployment override via PATCH_ALLOWLIST env (comma-separated; "*" = any
# patch id allowed, e.g. home deployments). Defaults below when unset.
DEFAULT_PATCH_ALLOWLIST = [
    "FW-6.6.55",
    "FW-6.6.52",
    "FW-7.0.1",
    "UBI-OS-5.1.20",
]


def patch_allowlist() -> list:
    """Effective patch allowlist: env file -> process env -> defaults. Hot."""
    raw = ""
    try:
        from llm_providers import read_env_file
        raw = (read_env_file().get("PATCH_ALLOWLIST") or "").strip()
    except Exception:
        raw = os.getenv("PATCH_ALLOWLIST", "").strip()
    if not raw:
        raw = os.getenv("PATCH_ALLOWLIST", "").strip()
    if not raw:
        return list(DEFAULT_PATCH_ALLOWLIST)
    if raw.strip() == "*":
        return ["*"]
    return [p.strip() for p in raw.split(",") if p.strip()]


def validate_action(action: str) -> tuple[bool, str]:
    """Check if an action string is a valid AllowedAction."""
    try:
        AllowedAction(action)
        return True, ""
    except ValueError:
        return False, f"Unknown action: '{action}'. Allowed: {[a.value for a in AllowedAction]}"


def validate_target(target: str) -> tuple[bool, str]:
    """Check if a target device is in managed inventory."""
    if not MANAGED_DEVICES:
        # Not loaded yet — skip validation (will be caught at agent level)
        return True, ""
    if target in MANAGED_DEVICES:
        return True, ""
    # Allow IP addresses to pass through (validated at agent level)
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", target):
        return True, ""
    # Allow subnet CIDRs (network_discovery targets)
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$", target):
        return True, ""
    return False, f"Unknown target: '{target}'. Device not in managed inventory."


def validate_params(action: str, params: dict) -> tuple[bool, str]:
    """Validate action-specific parameters."""
    if action == AllowedAction.REBOOT_DEVICE.value:
        # reboot_device.sh reboots immediately over SSH (maintenance window is
        # enforced by the operator/AI judgement, not by a scheduled_at param).
        return True, ""

    if action == AllowedAction.TICKET_STATUS.value:
        ticket_id = str(params.get("ticket_id", "")).strip().upper()
        if not re.match(r"^TKT-\d{8}-\d{4}$", ticket_id):
            return False, ("ticket_status requires 'ticket_id' in the form "
                           "TKT-YYYYMMDD-NNNN")
        return True, ""

    if action == AllowedAction.APPLY_PATCH.value:
        allow = patch_allowlist()
        if "*" in allow:
            return True, ""
        patch_id = params.get("patch_id", "")
        if patch_id not in allow:
            return False, f"Patch '{patch_id}' is not in the approved allowlist"
        return True, ""

    if action == AllowedAction.UNIFI_PORT_CONFIG.value:
        if "port_idx" not in params:
            return False, "unifi_port_config requires 'port_idx' parameter"
        if "tagged" not in params or not params.get("tagged"):
            return False, "unifi_port_config requires 'tagged' (network names/IDs)"
        return True, ""

    if action == AllowedAction.UNIFI_RESTART.value:
        # target must be the MAC of a managed UniFi device (validated upstream)
        return True, ""

    if action == AllowedAction.UNIFI_DEVICES.value:
        # optional filters: device_type (ap|switch|gateway), status (online|offline)
        dt = params.get("device_type")
        if dt and dt not in ("gateway", "switch", "ap"):
            return False, "device_type must be gateway, switch, or ap"
        st = params.get("status")
        if st and st not in ("online", "offline"):
            return False, "status must be online or offline"
        return True, ""

    if action == AllowedAction.UNIFI_CLIENTS.value:
        # optional filters: online (true|false), wired (true|false)
        for key in ("online", "wired"):
            v = params.get(key)
            if v is not None and str(v).lower() not in ("true", "false"):
                return False, f"{key} must be true or false"
        return True, ""

    if action == AllowedAction.UNIFI_PORT_BOUNCE.value:
        if "port_idx" not in params:
            return False, "unifi_port_bounce requires 'port_idx' parameter"
        return True, ""

    if action == AllowedAction.UNIFI_PORT_RENAME.value:
        if "port_idx" not in params:
            return False, "unifi_port_rename requires 'port_idx' parameter"
        if not params.get("name"):
            return False, "unifi_port_rename requires 'name' parameter"
        return True, ""

    if action == AllowedAction.UNIFI_SET_SSID_PASSWORD.value:
        if not params.get("ssid"):
            return False, "unifi_set_ssid_password requires an 'ssid' parameter"
        pw = str(params.get("password", ""))
        if not (8 <= len(pw) <= 63):
            return False, "unifi_set_ssid_password requires a 'password' of 8-63 characters"
        return True, ""

    if action == AllowedAction.UNIFI_NETWORK_CREATE.value:
        if not str(params.get("name", "")).strip():
            return False, "unifi_network_create requires a 'name' parameter"
        vlan = params.get("vlan")
        try:
            vlan = int(vlan)
        except (TypeError, ValueError):
            return False, "unifi_network_create requires a numeric 'vlan' (1-4094)"
        if not 1 <= vlan <= 4094:
            return False, "unifi_network_create vlan must be 1-4094"
        subnet = params.get("subnet")
        if subnet is not None and not re.match(
                r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$", str(subnet)):
            return False, "unifi_network_create subnet must be a CIDR like 192.168.<vlan>.1/24"
        return True, ""

    if action == AllowedAction.PI_TASK.value:
        if not params.get("task"):
            return False, "pi_task requires a 'task' parameter"
        return True, ""

    if action == AllowedAction.SNMP_SWEEP.value:
        return True, ""

    if action == AllowedAction.ENROLL_DEVICE.value:
        # target = the device; optional ttl for the enrollment token
        ttl = params.get("ttl")
        if ttl is not None:
            try:
                ttl = int(ttl)
            except (TypeError, ValueError):
                return False, "enroll_device ttl must be seconds (60-3600)"
            if not 60 <= ttl <= 3600:
                return False, "enroll_device ttl must be 60-3600 seconds"
        return True, ""

    if action == AllowedAction.BATCH.value:
        # Batch: every sub-job must itself pass the full validation pipeline
        jobs = params.get("jobs") if isinstance(params.get("jobs"), list) else []
        if not jobs:
            return False, "batch requires a non-empty 'jobs' list"
        if len(jobs) > 50:
            return False, "batch too large (max 50 sub-jobs)"
        for i, job in enumerate(jobs):
            if not isinstance(job, dict):
                return False, f"batch job {i} is not an object"
            ok, msg = validate_action(str(job.get("action", "")))
            if not ok:
                return False, f"batch job {i}: {msg}"
            if str(job.get("action")) == AllowedAction.BATCH.value:
                return False, "batch cannot nest batches"
            tgt = job.get("target") or ""
            if tgt:
                ok, msg = validate_target(tgt)
                if not ok:
                    return False, f"batch job {i}: {msg}"
            ok, msg = validate_params(str(job.get("action", "")), job.get("params") or {})
            if not ok:
                return False, f"batch job {i}: {msg}"
        return True, ""

    return True, ""


def validate_job(job: dict) -> tuple[bool, str]:
    """
    Full job validation pipeline.
    Returns (is_valid, reason_if_invalid).
    """
    # 1. Schema check
    if "action" not in job:
        return False, "Job missing 'action' field"

    # 2. Action must be allowed
    valid, msg = validate_action(job["action"])
    if not valid:
        return False, msg

    # 3. Target must exist
    if "target" in job and job["target"]:
        valid, msg = validate_target(job["target"])
        if not valid:
            return False, msg

    # 4. Parameters validation
    params = job.get("params", {})
    valid, msg = validate_params(job["action"], params)
    if not valid:
        return False, msg

    return True, ""
