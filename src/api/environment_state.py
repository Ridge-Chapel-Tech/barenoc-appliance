"""Knowledge-layer L1 — the managed-environment live state model.

A normalized, read-only view of what the appliance manages, consumed by the
agent's sysctx (via ``summarize_environment``) and the optimizer/report
(via ``device_state`` + the thin capability/control accessors below).

Design (briefs/knowledge-layer.md + managed-environment-intelligence.md):

* **identity/inventory** — name/type/vendor/model/firmware from the devices
  table + the collectors that already run (UniFi / SNMP / NOC_Agent facts).
* **channels** — which control channels exist (agent/ssh/snmp/unifi/vendor_api/
  monitor), reusing the device-add-model taxonomy in ``action_validator``.
* **capabilities** — resolved via ``catalog -> probe -> conservative floor``:
  1. a bundled baseline **capability catalog** (vendor/model -> feature set);
  2. **probe** hooks (agent capabilities, UniFi-managed flag, SSH key, agent
     facts) that infer support where the catalog lacks an entry;
  3. a **conservative floor** (unknown -> minimal set + ``unknown-floor``).
  Every capability carries a confidence basis (``catalog-verified`` /
  ``probed`` / ``unknown-floor``).
* **controls -> action channel** — computed from channel x permission:
  ``bareNOC_fix`` (we hold a direct control channel + the device is claimed),
  ``tech_action`` (managed gear, no direct control -> recommend a tech),
  ``manual_review`` (unmanaged / unknown).
* **config snapshot** — best-effort normalization of what the collectors already
  return (agent facts, SNMP/ping poll data, nmap fingerprint, device firmware);
  never re-collected.

Unknowns are FIRST-CLASS: a device outside the catalog (and with no probe
signal) is flagged (``unknown=True``) and carries a ``catalog_contribution``
hint (MAC OUI / fingerprint / agent facts) for the L3/L4 + shared-catalog lanes
to build on. Nothing here guesses an identity — it reports what exists.

No secrets ever leave this module: the digest and records contain no SNMP
communities, SSH keys, or credentials.
"""

import json
import re

from models import Device, DeviceFirmware
from action_validator import (
    effective_channels,
    canonical_device_type,
)

# ── capability vocabulary (stable names — the optimizer/rules key off these) ─
# catalog-verified / probed / unknown-floor are the ONLY allowed confidence
# bases (briefs/knowledge-layer.md). Capabilities are FEATURES the device class
# supports; whether BareNOC can DRIVE them is the action-channel's job.
CAPABILITY_BASES = ("catalog-verified", "probed", "unknown-floor")

# Channels that let BareNOC act directly on a device (bareNOC_fix). snmp is
# read-only polling in BareNOC (snmp_poll), so it is NOT a control channel.
CONTROL_CHANNELS = {"agent", "vendor_api", "ssh", "unifi"}

# The conservative floor: every unknown device gets only this, basis
# unknown-floor. "monitor" reachability is the one thing even an unclaimed
# record proves (it was discovered by ping/SNMP sweep).
FLOOR_CAPABILITIES = {
    "reachability": "ping / online-status monitor (minimal, unverified)",
}

# Agent-reported capability names -> canonical capability names (NOC_Agent
# self-report). Unknown agent names are kept verbatim as ``agent:<name>`` —
# never mapped onto a feature we cannot verify.
AGENT_ACTION_CAPABILITIES = {
    "check_updates": "update_check",
    "apply_updates": "apply_updates",
    "collect_logs": "collect_logs",
    "reboot": "reboot_control",
}


# ── the bundled capability catalog (small + honest) ────────────────────────
# Matcher = BOTH vendor and model must hit a keyword. Order matters only for
# entries whose keyword sets overlap; UniFi AP is checked before UniFi switch
# so a "U6-Enterprise" resolves to AP, while "USW-…" still hits switch via
# "usw"/"us-". Deliberately NOT exhaustive — the probe + floor + contribution
# paths handle everything else.
CAPABILITY_CATALOG = [
    {
        "id": "unifi-ap",
        "label": "UniFi access point",
        "vendor_kw": ("ubiquiti", "unifi", "ubnt"),
        "model_kw": ("uap", "u6", "u7", "ac-pro", "ac-lite", "ac-lr", "ac-m",
                     "nano", "mesh", "in-wall", "access point"),
        "capabilities": ("wifi_ssids", "wifi_wpa3", "vlan_8021q",
                         "unifi_controller_api", "firmware_feed"),
    },
    {
        "id": "unifi-gateway",
        "label": "UniFi gateway/console",
        "vendor_kw": ("ubiquiti", "unifi", "ubnt"),
        "model_kw": ("ucg", "udm", "uxg", "udr", "usg", "dream machine",
                     "dream router", "cloud gateway", "cloud key", "cloudkey",
                     "express", "gateway"),
        "capabilities": ("vlan_8021q", "dhcp_server", "firewall_rules",
                         "port_profiles", "unifi_controller_api",
                         "firmware_feed"),
    },
    {
        "id": "unifi-switch",
        "label": "UniFi switch",
        "vendor_kw": ("ubiquiti", "unifi", "ubnt"),
        "model_kw": ("usw", "us-", "us8", "us16", "us24", "us48", "switch",
                     "aggregation"),
        "capabilities": ("vlan_8021q", "port_profiles",
                         "unifi_controller_api", "firmware_feed"),
    },
    {
        "id": "linux-endpoint",
        "label": "Linux endpoint/server",
        "vendor_kw": ("linux", "ubuntu", "debian", "fedora", "centos",
                      "red hat", "redhat", "rhel", "rocky", "almalinux",
                      "alma", "qemu", "kvm", "oracle"),
        "model_kw": ("linux", "ubuntu", "debian", "fedora", "centos", "rhel",
                     "vm", "server", "workstation", "desktop", "virtual"),
        "capabilities": ("ssh_control", "collect_logs"),
    },
]

# Human-readable labels for the digest highlights (testable, no secrets).
CAPABILITY_LABELS = {
    "vlan_8021q": "802.1Q VLAN",
    "wifi_wpa3": "WPA3/SAE",
    "wifi_ssids": "multi-SSID",
    "dhcp_server": "DHCP",
    "dhcp_snooping": "DHCP snooping",
    "firewall_rules": "firewall rules",
    "port_profiles": "port profiles",
    "poe": "PoE",
    "unifi_controller_api": "UniFi control",
    "firmware_feed": "firmware feed",
    "agent_control": "agent control",
    "ssh_control": "SSH control",
    "vendor_api_control": "vendor API control",
    "update_check": "update check",
    "apply_updates": "apply updates",
    "collect_logs": "collect logs",
    "reboot_control": "reboot",
    "reachability": "monitor",
}


# ── small helpers (dict- and ORM-friendly) ─────────────────────────────────

def _get(obj, key, default=None):
    """Read a field from an ORM object OR a plain dict (the optimizer's scan
    snapshot devices are dicts)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _norm(s) -> str:
    return " ".join((s or "").lower().split())


def _parse_json(value):
    """Parse a JSON column (text or already-decoded) -> dict/list/None."""
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def _iso(dt) -> "str | None":
    return dt.isoformat() if dt is not None else None


def _oui(mac) -> "str | None":
    """The first 3 octets of a MAC (lowercased 'aa:bb:cc') — the OUI hint for
    the shared capability catalog. Never a vendor guess."""
    m = (mac or "").strip()
    if not m:
        return None
    parts = [p for p in re.split(r"[:-]", m) if p]
    if len(parts) < 3:
        return None
    return ":".join(p.lower() for p in parts[:3])


# ── channels ────────────────────────────────────────────────────────────────

def device_channels(device) -> list:
    """The device's effective control-channel set (derived ∪ explicit),
    deterministic order by security tier. Mirrors routes/devices._device_channels
    so the knowledge layer and the device API never disagree."""
    agent_connected = (_get(device, "adoption_method") == "agent"
                       or bool(_get(device, "agent_version")))
    return effective_channels(
        ssh_configured=bool(_get(device, "ssh_key_fingerprint")),
        snmp_configured=bool(_get(device, "snmp_community")),
        unifi_managed=bool(_get(device, "unifi_managed")),
        agent_connected=agent_connected,
        explicit=_get(device, "channels") or [],
    )


# ── capability resolution: catalog -> probe -> conservative floor ───────────

def _catalog_match(device) -> "dict | None":
    """Return the catalog entry whose vendor AND model keywords both match.
    Both must be present — a model-only or vendor-only hit is NOT catalog
    verification (that's the probe's job, and it stays 'probed')."""
    vendor = _norm(_get(device, "vendor"))
    model = _norm(_get(device, "model"))
    if not vendor or not model:
        return None
    for entry in CAPABILITY_CATALOG:
        if any(k in vendor for k in entry["vendor_kw"]):
            if any(k in model for k in entry["model_kw"]):
                return entry
    return None


def _probe_capabilities(device) -> dict:
    """Infer capabilities from live signals where the catalog lacks an entry.

    Returns {capability: {"basis": "probed", "source": …}}. Only signals the
    collectors already persisted are used — nothing is re-collected here.
    """
    out = {}

    # NOC_Agent self-report: the agent IS a control path.
    if _get(device, "adoption_method") == "agent" or _get(device, "agent_version"):
        out["agent_control"] = {"basis": "probed", "source": "agent:report"}
    agent_caps = _parse_json(_get(device, "agent_capabilities"))
    if isinstance(agent_caps, list):
        out["agent_control"] = {"basis": "probed", "source": "agent:report"}
        for c in agent_caps:
            name = str(c).strip().lower()
            if not name:
                continue
            if name in AGENT_ACTION_CAPABILITIES:
                out[AGENT_ACTION_CAPABILITIES[name]] = {
                    "basis": "probed", "source": f"agent:capability:{name}"}
            else:
                out[f"agent:{name}"] = {
                    "basis": "probed", "source": f"agent:capability:{name}"}

    # Stored SSH key -> SSH control + log collection.
    if _get(device, "ssh_key_fingerprint"):
        out["ssh_control"] = {"basis": "probed", "source": "ssh:key"}
        out["collect_logs"] = {"basis": "probed", "source": "ssh:key"}

    # UniFi-managed -> controller control + firmware feed (+ VLAN on network
    # gear — the controller assigns per-port/per-SSID VLANs there).
    if _get(device, "unifi_managed"):
        out["unifi_controller_api"] = {"basis": "probed", "source": "unifi:managed"}
        out["firmware_feed"] = {"basis": "probed", "source": "unifi:managed"}
        dtype = str(_get(device, "device_type") or "").lower()
        if dtype in ("switch", "gateway", "router", "ap"):
            out["vlan_8021q"] = {"basis": "probed", "source": "unifi:managed"}

    return out


def resolve_capabilities(device) -> dict:
    """catalog -> probe -> conservative floor, with per-capability confidence.

    Returns {"capabilities": {name: {"basis", "source"}},
             "confidence": "catalog-verified"|"probed"|"unknown-floor",
             "catalog_id": str|None}.
    ``confidence`` is the resolution tier: catalog when a known class matches,
    probe when only live signals inferred support, floor otherwise.
    """
    entry = _catalog_match(device)
    if entry is not None:
        caps = {c: {"basis": "catalog-verified", "source": f"catalog:{entry['id']}"}
                for c in entry["capabilities"]}
        return {"capabilities": caps, "confidence": "catalog-verified",
                "catalog_id": entry["id"]}

    caps = _probe_capabilities(device)
    if caps:
        return {"capabilities": caps, "confidence": "probed", "catalog_id": None}

    return {
        "capabilities": {c: {"basis": "unknown-floor", "source": "floor"}
                         for c in FLOOR_CAPABILITIES},
        "confidence": "unknown-floor",
        "catalog_id": None,
    }


# ── controls -> action channel (channel x permission) ──────────────────────

def compute_action_channel(device, channels) -> dict:
    """The action channel for a recommendation on this device — computed from
    channel x permission (brief §2):

    * ``bareNOC_fix``  — claimed AND a direct control channel is present.
    * ``tech_action``  — claimed, no direct control (snmp/monitor only) ->
                         "recommend the tech apply X on device Y".
    * ``manual_review`` — unclaimed (unmanaged).

    "Unknown" is orthogonal and first-class: an unknown device (outside the
    catalog, ``capability_confidence: unknown-floor``) is flagged on the record
    and in the digest, but its action channel still follows permission/control
    (a claimed unknown device routes to ``tech_action`` — a human acts, never a
    guess).
    """
    if not _get(device, "claimed", True):
        return {"channel": "manual_review",
                "reason": "unmanaged — device is not claimed"}
    control = set(channels or []) & CONTROL_CHANNELS
    if control:
        return {"channel": "bareNOC_fix",
                "reason": "direct control channel present",
                "via": sorted(control)}
    return {"channel": "tech_action",
            "reason": "managed but no direct control channel — recommend a technician"}


# ── config snapshot (best-effort normalization, never re-collected) ────────

def _firmware_row(device, db):
    """The device's firmware state from the collectors (device_firmware), by
    device_id first, then by MAC."""
    if db is None:
        return None
    q = db.query(DeviceFirmware).filter(DeviceFirmware.device_id == _get(device, "id"))
    row = q.first()
    if row is None and _get(device, "mac_address"):
        row = (db.query(DeviceFirmware)
               .filter(DeviceFirmware.mac_address == _get(device, "mac_address"))
               .first())
    return row


def _config_snapshot(device, db) -> dict:
    """Normalize what the collectors already return — agent facts, poll data
    (ping/SNMP), nmap fingerprint, and firmware — into one config block."""
    cfg = {}
    facts = _parse_json(_get(device, "facts_json"))
    if isinstance(facts, dict) and facts:
        cfg["agent"] = {k: v for k, v in facts.items() if v not in (None, "")}
    poll = _get(device, "last_poll_data")
    if isinstance(poll, dict) and poll:
        cfg["collector"] = poll
    fp = _get(device, "fingerprint")
    if isinstance(fp, dict) and fp:
        cfg["fingerprint"] = {k: fp.get(k)
                              for k in ("os", "vendor", "device_type", "open_ports")
                              if fp.get(k)}
    fw = _firmware_row(device, db)
    if fw is not None:
        cfg["firmware"] = {
            "current": fw.current_version or None,
            "available": fw.available_version or None,
            "upgradeable": bool(fw.upgradeable),
            "model": fw.model,
        }
    return cfg


def _catalog_contribution(device) -> "dict | None":
    """What we already know that could teach the shared catalog about an
    unknown device (fingerprint / OUI / agent facts). Advisory only."""
    c = {}
    oui = _oui(_get(device, "mac_address"))
    if oui:
        c["mac_oui"] = oui
    fp = _get(device, "fingerprint")
    if isinstance(fp, dict):
        hint = {k: fp.get(k) for k in ("os", "vendor", "device_type") if fp.get(k)}
        ports = fp.get("open_ports")
        if ports:
            hint["open_ports"] = [
                (p.get("port") if isinstance(p, dict) else p) for p in ports][:20]
        if hint:
            c["fingerprint"] = hint
    facts = _parse_json(_get(device, "facts_json"))
    if isinstance(facts, dict):
        hint = {k: facts.get(k) for k in ("os", "kernel", "hostname") if facts.get(k)}
        if hint:
            c["agent_facts"] = hint
    return c or None


# ── the normalized record + query surface ──────────────────────────────────

def _build_record(device, db=None) -> dict:
    channels = device_channels(device)
    resolution = resolve_capabilities(device)
    confidence = resolution["confidence"]
    action = compute_action_channel(device, channels)
    fw = _firmware_row(device, db)
    return {
        "id": _get(device, "id"),
        "identity": {
            "name": _get(device, "name"),
            "hostname": _get(device, "hostname"),
            "ip_address": _get(device, "ip_address"),
            "mac_address": _get(device, "mac_address"),
            "device_type": canonical_device_type(_get(device, "device_type")),
            "vendor": _get(device, "vendor"),
            "model": _get(device, "model"),
            "firmware": (fw.current_version or None) if fw is not None else None,
        },
        "status": {
            "state": _get(device, "status"),
            "claimed": bool(_get(device, "claimed", True)),
            "last_seen": _iso(_get(device, "last_seen")),
        },
        "channels": channels,
        "capabilities": resolution["capabilities"],
        "capability_confidence": confidence,
        "catalog": {
            "id": resolution["catalog_id"],
            "matched": resolution["catalog_id"] is not None,
        },
        "controls": action,
        "config": _config_snapshot(device, db),
        "unknown": confidence == "unknown-floor",
        "catalog_contribution": _catalog_contribution(device),
    }


def device_state(db, device_id: int) -> "dict | None":
    """The full normalized record for one managed device (optimizer + report
    consume). None when the device does not exist."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if device is None:
        return None
    return _build_record(device, db)


def _unknown_brief(device) -> dict:
    return {
        "id": _get(device, "id"),
        "name": _get(device, "name"),
        "ip": _get(device, "ip_address"),
        "device_type": canonical_device_type(_get(device, "device_type")),
        "vendor": _get(device, "vendor"),
        "model": _get(device, "model"),
        "hint": _catalog_contribution(device),
    }


def _digest_text(s: dict) -> str:
    """The compact sysctx digest — inventory counts, capability/control
    highlights, unknown-device flags. No secrets, bounded length."""
    inv = s.get("inventory", {})
    classes = ", ".join(f"{k} {v}" for k, v in sorted(inv.get("by_class", {}).items()))
    controls = s.get("controls", {})
    conf = s.get("capabilities", {}).get("confidence", {})
    lines = [
        f"ENVIRONMENT: {inv.get('total', 0)} managed devices"
        f" ({classes or 'none'}). Claimed {inv.get('claimed', 0)},"
        f" unclaimed {inv.get('unclaimed', 0)}.",
        f"Control channels: {controls.get('bareNOC_fix', 0)} bareNOC-fix, "
        f"{controls.get('tech_action', 0)} tech-action, "
        f"{controls.get('manual_review', 0)} manual-review.",
        f"Capability confidence: catalog-verified "
        f"{conf.get('catalog-verified', 0)}, probed {conf.get('probed', 0)}, "
        f"unknown-floor {conf.get('unknown-floor', 0)}.",
    ]
    unknown = s.get("unknown_devices") or []
    if unknown:
        names = ", ".join(f"{u['name']} {u['ip']}" for u in unknown[:8])
        lines.append(f"Unknown devices (verify before acting): {names}.")
    return "\n".join(lines)


def summarize_environment(db) -> dict:
    """The compact environment digest for the sysctx builder (+ the optimizer
    report). Structured summary + a pre-rendered ``text`` block."""
    devices = db.query(Device).all()
    by_class = {}
    confidence_counts = {"catalog-verified": 0, "probed": 0, "unknown-floor": 0}
    control_counts = {"bareNOC_fix": 0, "tech_action": 0, "manual_review": 0}
    highlights = {}
    claimed = unclaimed = 0
    unknown_devices = []

    for d in devices:
        ctype = canonical_device_type(d.device_type)
        by_class[ctype] = by_class.get(ctype, 0) + 1
        if d.claimed:
            claimed += 1
        else:
            unclaimed += 1
        channels = device_channels(d)
        resolution = resolve_capabilities(d)
        confidence = resolution["confidence"]
        confidence_counts[confidence] += 1
        action = compute_action_channel(d, channels)
        control_counts[action["channel"]] += 1
        for cap in resolution["capabilities"]:
            highlights[cap] = highlights.get(cap, 0) + 1
        if confidence == "unknown-floor":
            unknown_devices.append(_unknown_brief(d))

    summary = {
        "inventory": {
            "total": len(devices),
            "claimed": claimed,
            "unclaimed": unclaimed,
            "by_class": by_class,
        },
        "capabilities": {
            "highlights": highlights,
            "confidence": confidence_counts,
        },
        "controls": control_counts,
        "unknown_devices": unknown_devices,
        "unknown_count": len(unknown_devices),
    }
    summary["text"] = _digest_text(summary)
    return summary


# ── the optimizer's thin capability/control accessor ────────────────────────
# netopt-netsec-rules owns network_opt_rules.py; these are the import points
# its capability/permission-aware findings (and the report lane) use. They are
# deliberately thin — resolution lives above, not in the rules.

def capabilities_for(db, device_id: int) -> dict:
    """Resolved capabilities for one device: {capability: {"basis", "source"}}.
    Empty dict for an unknown device id (the optimizer must then treat the
    device as unknown-floor, never guess)."""
    rec = device_state(db, device_id)
    return rec["capabilities"] if rec else {}


def has_capability(db, device_id: int, capability: str) -> bool:
    """True when a capability has been VERIFIED (catalog or probe) for the
    device. The optimizer must not fire a rule on an unverified capability."""
    return capability in capabilities_for(db, device_id)


def action_channel_for(db, device_id: int) -> str:
    """The action channel for a device's recommendations: one of
    ``bareNOC_fix`` / ``tech_action`` / ``manual_review``."""
    rec = device_state(db, device_id)
    return rec["controls"]["channel"] if rec else "manual_review"


def capability_label(capability: str) -> str:
    """Human label for a capability name (digest/report rendering)."""
    return CAPABILITY_LABELS.get(capability, capability)
