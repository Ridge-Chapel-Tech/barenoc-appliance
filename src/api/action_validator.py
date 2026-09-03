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
    CHECK_UPDATES = "check_updates"  # agent device: read-only multi-source update check
    APPLY_UPDATES = "apply_updates"  # agent device: confirm-gated OS update apply
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
    WINDOWS_DIAG = "windows_diag"          # read-only: Windows PC health report over SSH
    WINDOWS_CLEANUP = "windows_cleanup"    # safe cleanup: autostart offenders + TEMP + recycle (no uninstalls/partition ops)


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
    AllowedAction.WINDOWS_DIAG: "scripts/windows_diag.sh",
    AllowedAction.WINDOWS_CLEANUP: "scripts/windows_cleanup.sh",
    # ESCALATE_HUMAN has no script — it's a logical action
}

# Managed devices (populated from DB at runtime)
# DEVICES dict is populated by the worker before validation
MANAGED_DEVICES: dict = {}


# ── Control-channel capability model (device_adoption_model.md §3-§5) ──
# Capability = config (the device's channels); authority = code (this catalog).
# Channels are ranked security-first; the recommendation tier in §4 of the doc.
CHANNEL_AGENT = "agent"
CHANNEL_VENDOR_API = "vendor_api"
CHANNEL_SSH = "ssh"
CHANNEL_UNIFI = "unifi"
CHANNEL_SNMP = "snmp"
CHANNEL_MONITOR = "monitor"

# Canonical order = security tier (highest first) — used for deterministic
# channel lists and the recommendation ranking.
CHANNELS = (
    CHANNEL_AGENT, CHANNEL_VENDOR_API, CHANNEL_SSH, CHANNEL_UNIFI,
    CHANNEL_SNMP, CHANNEL_MONITOR,
)

DEVICE_TYPES = ("server", "switch", "ap", "router", "camera", "iot", "other")

# Legacy device_type spellings (kept working; display/suggestion map to canon).
LEGACY_DEVICE_TYPES = {
    "gateway": "router", "workstation": "server", "printer": "iot", "nas": "iot",
}

# Security tier: lower = prefer. snmpv2c / plaintext HTTP are last-resort and
# are never auto-recommended (they surface as warnings instead).
CHANNEL_SECURITY_RANK = {
    CHANNEL_AGENT: 0,
    CHANNEL_VENDOR_API: 1,
    CHANNEL_SSH: 2,
    CHANNEL_UNIFI: 3,
    CHANNEL_SNMP: 4,
    CHANNEL_MONITOR: 5,
}

CHANNEL_WHY = {
    CHANNEL_AGENT: "mTLS cert, poll-only (no exposed service), least-privilege",
    CHANNEL_VENDOR_API: "vendor RESTCONF/NETCONF/HTTP over TLS (token/cert auth)",
    CHANNEL_SSH: "SSH key-only + scoped sudo",
    CHANNEL_UNIFI: "delegated controller trust (no per-device creds)",
    CHANNEL_SNMP: "SNMPv3 auth+priv (v2c plaintext is last-resort only)",
    CHANNEL_MONITOR: "ping/status only — recommended over weak control",
}


def canonical_device_type(device_type: str) -> str:
    """Map legacy spellings to the canonical taxonomy (idempotent)."""
    return LEGACY_DEVICE_TYPES.get((device_type or "").lower(), (device_type or "other"))


def effective_channels(ssh_configured: bool = False, snmp_configured: bool = False,
                       unifi_managed: bool = False, agent_connected: bool = False,
                       explicit=None) -> list:
    """The device's effective control-channel set = auto-derived (from
    credential/adoption columns) ∪ explicit declarations (vendor_api, monitor,
    future channels). 'monitor' is always present. Deterministic order (by
    security tier). device_adoption_model.md §8."""
    ch = {CHANNEL_MONITOR}
    if ssh_configured:
        ch.add(CHANNEL_SSH)
    if snmp_configured:
        ch.add(CHANNEL_SNMP)
    if unifi_managed:
        ch.add(CHANNEL_UNIFI)
    if agent_connected:
        ch.add(CHANNEL_AGENT)
    for c in (explicit or []):
        if c in CHANNELS:
            ch.add(c)
    return [c for c in CHANNELS if c in ch]


# Actions that require a control channel on the TARGET device. An action not
# listed here is channel-agnostic (appliance-side, controller-side, or logical)
# and keeps today's behavior. device_adoption_model.md §5.
ACTION_REQUIRED_CHANNELS = {
    AllowedAction.REBOOT_DEVICE: {CHANNEL_SSH, CHANNEL_AGENT, CHANNEL_SNMP, CHANNEL_VENDOR_API},
    AllowedAction.COLLECT_LOGS: {CHANNEL_SSH, CHANNEL_AGENT},
    AllowedAction.APPLY_PATCH: {CHANNEL_SSH, CHANNEL_AGENT},
    AllowedAction.INSTALL_CHAT_CLIENT: {CHANNEL_SSH, CHANNEL_AGENT},
    AllowedAction.ENROLL_DEVICE: {CHANNEL_SSH},
    AllowedAction.SNMP_POLL: {CHANNEL_SNMP},
    # Agent-channel ONLY: check_updates/apply_updates run on the endpoint via
    # the NOC_Agent device_jobs transport (not the SSH-script path). SSH
    # devices use apply_patch/check via scripts/apply_patch.sh instead.
    AllowedAction.CHECK_UPDATES: {CHANNEL_AGENT},
    AllowedAction.APPLY_UPDATES: {CHANNEL_AGENT},
    # Windows PCs are SSH-only for now (device_adoption_model.md §2: server
    # type + ssh channel; the Windows agent ships in a later milestone).
    AllowedAction.WINDOWS_DIAG: {CHANNEL_SSH},
    AllowedAction.WINDOWS_CLEANUP: {CHANNEL_SSH},
}


def validate_channels(action, device_type: str = None, channels=None) -> tuple[bool, str]:
    """Capability gate: an action whose catalog entry declares required
    channels must intersect the device's channels. Channel-less actions and
    unknown channels (None) pass through — the agent-level gate stays the
    fallback. device_adoption_model.md §5."""
    try:
        action = AllowedAction(action)
    except ValueError:
        return True, ""  # unknown action is caught by validate_action
    required = ACTION_REQUIRED_CHANNELS.get(action)
    if not required:
        return True, ""
    if channels is None:
        return True, ""  # channel info unknown — don't block
    have = set(channels or [])
    if have & required:
        return True, ""
    return False, (
        f"Action '{action.value}' requires one of these control channels: "
        f"{', '.join(sorted(required))}; the device has "
        f"{', '.join(sorted(have)) if have else 'none'}"
    )


def _camera_vendor(vendor: str) -> bool:
    v = (vendor or "").lower()
    return any(k in v for k in ("hikvision", "dahua", "onvif", "axis", "amcrest",
                                "reolink", "uniview", "camera"))


def suggest_from_fingerprint(fp: dict) -> dict:
    """Map a fingerprint.sh JSON result to a suggested type + candidate
    channels + a SECURITY-FIRST recommendation. Pure function — no DB.
    device_adoption_model.md §2 and §4.

    Returns {"device_type", "candidate_channels" (ranked), "recommendation"
             (one channel), "why" (one line, security), "warnings": [...]}.
    """
    fp = fp or {}
    ports = {p.get("port") for p in (fp.get("open_ports") or [])}
    vendor = str(fp.get("vendor") or "").lower()
    os_ = str(fp.get("os") or "").lower()
    ssh_banner = str(fp.get("ssh_banner") or "").lower()
    sysdescr = str(fp.get("sysdescr") or "").lower()

    warnings = []
    candidate = []
    device_type = "other"
    insecure = set()  # channels flagged as plaintext/weak for THIS device

    has_ssh = 22 in ports
    has_snmp = 161 in ports or "snmp" in sysdescr
    has_http = 80 in ports or 443 in ports
    has_rtsp = 554 in ports
    net_gear = any(k in sysdescr or k in vendor for k in
                   ("cisco", "juniper", "hp", "aruba", "mikrotik", "huawei",
                    "switch", "router", "fortinet", "brocade", "zyxel"))
    camera = _camera_vendor(vendor) or has_rtsp or ("onvif" in sysdescr)

    if net_gear and ("switch" in sysdescr or "router" in sysdescr or
                     "gateway" in sysdescr or "ios" in sysdescr or
                     "junos" in sysdescr or has_snmp):
        device_type = "router" if ("router" in sysdescr or "gateway" in sysdescr
                                   or "junos" in sysdescr) else "switch"
        candidate = [CHANNEL_SNMP, CHANNEL_VENDOR_API]
        if has_ssh:
            candidate.insert(0, CHANNEL_SSH)
        if not has_ssh and has_http and not has_snmp:
            warnings.append("plaintext HTTP management — prefer vendor API over TLS")
            insecure.add(CHANNEL_VENDOR_API)
    elif camera:
        device_type = "camera"
        candidate = [CHANNEL_MONITOR, CHANNEL_VENDOR_API]
        if has_http and has_rtsp:
            warnings.append("camera exposes plaintext HTTP/RTSP — monitor-only recommended")
            insecure.add(CHANNEL_VENDOR_API)
        if has_snmp:
            candidate.append(CHANNEL_SNMP)
    elif has_ssh:
        device_type = "server"
        candidate = [CHANNEL_AGENT, CHANNEL_SSH]
        if "windows" in os_ or "windows" in ssh_banner:
            warnings.append("Windows endpoint — agent channel ships in a later milestone; SSH key-only for now")
        if has_snmp:
            candidate.append(CHANNEL_SNMP)
    elif 445 in ports or 139 in ports:
        device_type = "server"
        candidate = [CHANNEL_MONITOR]
        warnings.append("SMB ports open, no SSH — no secure control channel; monitor-only")
    elif 9100 in ports:
        device_type = "iot"
        candidate = [CHANNEL_MONITOR, CHANNEL_SNMP]
    elif has_http:
        device_type = "iot"
        candidate = [CHANNEL_MONITOR, CHANNEL_VENDOR_API]
        if 443 not in ports:
            warnings.append("HTTP (not HTTPS) only — plaintext; monitor-only recommended")
            insecure.add(CHANNEL_VENDOR_API)
    else:
        device_type = "other"
        candidate = [CHANNEL_MONITOR]

    if CHANNEL_MONITOR not in candidate:
        candidate.append(CHANNEL_MONITOR)
    # Default-credential warning (can't be proven from a passive scan — flag
    # the class of risk when a weak channel is the only control option).
    snmp_v3 = bool(str(fp.get("snmp_version") or "").lower().startswith("3")) \
        or bool((fp.get("snmp") or {}).get("v3"))
    if CHANNEL_SNMP in candidate and not snmp_v3:
        warnings.append("SNMP likely v2c plaintext community — use v3 auth+priv or monitor-only")
        insecure.add(CHANNEL_SNMP)

    # Security-first recommendation (device_adoption_model.md §4): the
    # highest-ranked candidate that is secure for THIS device; 'monitor' is
    # the recommendation when every control channel is weak/plaintext.
    ranked = sorted(set(candidate), key=lambda c: CHANNEL_SECURITY_RANK.get(c, 99))
    secure = [c for c in ranked if c not in insecure and c != CHANNEL_MONITOR]
    recommendation = secure[0] if secure else CHANNEL_MONITOR
    return {
        "device_type": device_type,
        "candidate_channels": ranked,
        "recommendation": recommendation,
        "why": CHANNEL_WHY.get(recommendation, ""),
        "warnings": warnings,
    }

# Patch allowlist — only patches on this list can be applied.
# Per-deployment override via PATCH_ALLOWLIST env (comma-separated; "*" = any
# patch id allowed, e.g. home deployments). Defaults below when unset.
DEFAULT_PATCH_ALLOWLIST = [
    "FW-6.6.55",
    "FW-6.6.52",
    "FW-7.0.1",
    "UBI-OS-5.1.20",
]


# Windows cleanup: known autostart offenders (configurable per deployment via
# WINDOWS_CLEANUP_OFFENDERS env — comma-separated). The cleanup pass stops the
# matching process AND removes its autostart entry (Run key / Startup folder).
# Safe by construction: no uninstalls, no partition ops — see
# scripts/windows_cleanup.sh and the windows_cleanup validate_params branch.
DEFAULT_WINDOWS_CLEANUP_OFFENDERS = [
    "Adobe CollabSync",
    "Copilot",
]


def windows_cleanup_offenders() -> list:
    """Effective Windows cleanup offender list: env file -> process env ->
    defaults. Hot (matches the patch allowlist pattern)."""
    raw = ""
    try:
        from llm_providers import read_env_file
        raw = (read_env_file().get("WINDOWS_CLEANUP_OFFENDERS") or "").strip()
    except Exception:
        raw = os.getenv("WINDOWS_CLEANUP_OFFENDERS", "").strip()
    if not raw:
        raw = os.getenv("WINDOWS_CLEANUP_OFFENDERS", "").strip()
    if not raw:
        return list(DEFAULT_WINDOWS_CLEANUP_OFFENDERS)
    return [o.strip() for o in raw.split(",") if o.strip()]


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


_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_CIDR_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$")


def is_ip_or_subnet(target: str) -> bool:
    """True for a bare IPv4 address or a subnet CIDR (agent-validated later)."""
    return bool(_IP_RE.match(target or "") or _CIDR_RE.match(target or ""))


def find_subnet(text: str) -> "str | None":
    """First subnet CIDR in free text — the actionable target of a
    whole-subnet scan request ('ping sweep 192.168.1.0/24')."""
    m = re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}\b", text or "")
    return m.group(0) if m else None


# Tone-discipline: customer-facing (chat) vs technical (ticket/log) wording.
_UNKNOWN_TARGET_FRIENDLY = (
    "I couldn't find a device named '{target}'. You can ask me to check an IP "
    "address or subnet instead (for example, 'scan 192.168.1.0/24'), or adopt "
    "the device first so it shows up in my inventory."
)


def unknown_target_friendly(target: str) -> str:
    """Customer-facing message for an unresolvable device name."""
    return _UNKNOWN_TARGET_FRIENDLY.format(target=target)


def unknown_target_detail(target: str) -> str:
    """Technical ticket/log wording — keeps the internal inventory context."""
    managed = ", ".join(sorted(MANAGED_DEVICES.keys())) or "(none loaded)"
    return (f"Unknown target '{target}' — device not in managed inventory "
            f"(managed: {managed})")


def validate_target(target: str) -> tuple[bool, str]:
    """Check if a target device is in managed inventory.

    IPs and subnet CIDRs pass through (validated at the agent level). An
    unresolvable NAME returns the customer-friendly message; call
    unknown_target_detail() for the technical ticket/log text.
    """
    if not MANAGED_DEVICES:
        # Not loaded yet — skip validation (will be caught at agent level)
        return True, ""
    if target in MANAGED_DEVICES:
        return True, ""
    if is_ip_or_subnet(target):
        return True, ""
    return False, unknown_target_friendly(target)


def validate_params(action: str, params: dict) -> tuple[bool, str]:
    """Validate action-specific parameters."""
    if action == AllowedAction.REBOOT_DEVICE.value:
        # reboot_device.sh reboots immediately over SSH (maintenance window is
        # enforced by the operator/AI judgement, not by a scheduled_at param).
        return True, ""

    if action == AllowedAction.APPLY_UPDATES.value:
        # apply_updates writes to the endpoint OS — customer-requested only.
        # The SAME confirm gate as reboot (and as the agent catalog + the
        # device_agent enqueue path): never apply autonomously-unprompted.
        if not params.get("confirm"):
            return False, "apply_updates requires 'confirm' = true"
        return True, ""

    if action == AllowedAction.CHECK_UPDATES.value:
        # Read-only multi-source check — no params required.
        return True, ""

    if action == AllowedAction.WINDOWS_DIAG.value:
        # Read-only health report over SSH — no params required.
        return True, ""

    if action == AllowedAction.WINDOWS_CLEANUP.value:
        # Safe cleanup (autostart offenders + TEMP + recycle). The offender
        # list is optional + configurable; it must be a list of names when
        # provided. The script NEVER runs partition ops or uninstalls — those
        # require an explicit per-device owner confirmation outside this
        # action (there is no code path for them here at all).
        offenders = params.get("offenders")
        if offenders is not None:
            if not isinstance(offenders, list):
                return False, "windows_cleanup 'offenders' must be a list of names"
            for o in offenders:
                if not isinstance(o, str) or not o.strip():
                    return False, "windows_cleanup 'offenders' entries must be non-empty strings"
                if len(o) > 128:
                    return False, "windows_cleanup offender names must be <= 128 chars"
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

    # 5. Channel capability check (device_adoption_model.md §5): when the
    # target resolves to a managed device with known channels, the action's
    # required channels must intersect the device's channels. Unknown targets
    # or devices without channel info pass through (agent-level fallback).
    target = job.get("target") or ""
    if target and target in MANAGED_DEVICES:
        dev = MANAGED_DEVICES[target]
        if isinstance(dev, dict) and dev.get("channels") is not None:
            valid, msg = validate_channels(job["action"], dev.get("type"), dev["channels"])
            if not valid:
                return False, msg

    return True, ""
