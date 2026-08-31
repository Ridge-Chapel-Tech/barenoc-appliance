"""Discovery-side dedupe + appliance self-exclusion.

Two invariants every discovery WRITE path must honor (locked brief
`briefs/discovery-dedupe.md`):

1. **Self-exclusion** — the appliance never appears in its own inventory.
   Its identity is APPLIANCE_IP (from .env) + the host's own interface IPs
   and MACs + loopback, and is overridable via ``SELF_EXCLUDE_IPS`` /
   ``SELF_EXCLUDE_MACS``.

2. **Match-before-insert** — a discovered identity is matched against an
   existing record by ``mac_address`` (case/separator-insensitive) first,
   then ``ip_address``, and UPDATEs that record instead of INSERTing a
   duplicate. A ``claimed=1`` record's identity is never stolen: a
   conflicting discovery is logged and skipped.

NOTE: the adoption-time IP/MAC fallback + merge helper (routes/devices.py,
device_certs.py, device_agent.py) belong to the OTHER worker
(feat/device-dedupe). This module only serves the DISCOVERY writers (UniFi
sync + ping/discover sweep + SNMP sweep).
"""

import logging
import os
import socket

from datetime import datetime

from sqlalchemy import func

from models import Device

logger = logging.getLogger("discovery")

ENV_PATH = "/opt/barenoc/.env"

_LOOPBACK_IPS = frozenset({"127.0.0.1", "::1", "0.0.0.0"})
_SELF_NAMES = frozenset({
    "bareNOC", "bareNOC.local", "app.barenoc.com", "localhost",
})

# Host MACs are 12 hex chars; separators vary (":" / "-" / "."). Normalize by
# stripping every non-alphanumeric char so "AA:BB:CC:00:00:01" == "aa-bb-cc-00-00-01".
_MAC_NORMALIZED = func.lower(
    func.replace(
        func.replace(
            func.replace(Device.mac_address, ":", ""),
            "-", ""),
        ".", ""))


def _read_env() -> dict:
    """Read /opt/barenoc/.env (file first), with process-env fallback for the
    self-exclusion keys so tests can inject the appliance identity."""
    env = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception:
        pass
    for key in ("APPLIANCE_IP", "SELF_EXCLUDE_IPS", "SELF_EXCLUDE_MACS"):
        if key not in env and key in os.environ:
            env[key] = os.environ[key]
    return env


def _csv(value) -> set:
    return {x.strip() for x in (value or "").split(",") if x.strip()}


def normalize_mac(mac) -> str:
    """Lowercase a MAC and drop separators (so stored/discovered forms match)."""
    if not mac:
        return ""
    return "".join(ch for ch in str(mac).lower() if ch.isalnum())


def normalize_ip(ip) -> str:
    return str(ip or "").strip().lower()


def _host_interface_ids() -> "tuple[set, set]":
    """Best-effort read of the host's own interface IPs + MACs.

    On the pi-agent runner (host-side) this is the appliance's real NIC and VM
    bridge interfaces; inside the api container it reflects the container's own
    netns (a defensive second net — APPLIANCE_IP stays authoritative). Never
    raises: a failed read just means fewer self IDs.
    """
    ips, macs = set(), set()
    # MACs from /sys/class/net/<if>/address (stdlib, no subprocess).
    try:
        for name in os.listdir("/sys/class/net"):
            try:
                with open(f"/sys/class/net/{name}/address") as f:
                    m = f.read().strip().lower()
                if m and m != "00:00:00:00:00:00":
                    macs.add(m)
            except Exception:
                pass
    except Exception:
        pass
    # IPv4s via hostname resolution + SIOCGIFADDR (Linux).
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except Exception:
        pass
    try:
        import fcntl
        import struct
        SIOCGIFADDR = 0x8915
        for _idx, name in socket.if_nameindex():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                packed = fcntl.ioctl(
                    s.fileno(), SIOCGIFADDR,
                    struct.pack("256s", name.encode()[:15]))[20:24]
                addr = socket.inet_ntoa(packed)
                if addr and addr != "0.0.0.0":
                    ips.add(addr)
            except Exception:
                pass
            finally:
                s.close()
    except Exception:
        pass
    return ips, macs


def self_exclusion(env: dict = None) -> dict:
    """The appliance's own IPs/MACs/names that discovery must never record.

    Returns ``{"ips": set, "macs": set, "names": set}`` (all lowercased /
    separator-stripped where appropriate). Identity sources: loopback,
    APPLIANCE_IP, the host's own interfaces, then the SELF_EXCLUDE_* overrides.
    """
    env = env if env is not None else _read_env()
    ips = set(_LOOPBACK_IPS) | _csv(env.get("APPLIANCE_IP")) | _csv(env.get("SELF_EXCLUDE_IPS"))
    ips = {normalize_ip(i) for i in ips if i}
    macs = _csv(env.get("SELF_EXCLUDE_MACS"))
    host_ips, host_macs = _host_interface_ids()
    ips |= {normalize_ip(i) for i in host_ips if i}
    macs |= host_macs
    macs = {normalize_mac(m) for m in macs if normalize_mac(m)}
    names = {n.lower() for n in _SELF_NAMES}
    return {"ips": ips, "macs": macs, "names": names}


def is_self_ip(ip, excl: dict = None) -> bool:
    ip = normalize_ip(ip)
    if not ip:
        return False
    excl = excl if excl is not None else self_exclusion()
    return ip in excl["ips"]


def is_self_mac(mac, excl: dict = None) -> bool:
    m = normalize_mac(mac)
    if not m:
        return False
    excl = excl if excl is not None else self_exclusion()
    return m in excl["macs"]


def is_self_identity(ip=None, mac=None, name=None, hostname=None,
                     excl: dict = None) -> bool:
    """True when a discovered identity is the appliance itself: an exact IP/MAC
    match, or a name/hostname that embeds one of the self identifiers."""
    excl = excl if excl is not None else self_exclusion()
    if is_self_ip(ip, excl) or is_self_mac(mac, excl):
        return True
    hay = " ".join(str(x or "") for x in (name, hostname)).lower()
    if not hay:
        return False
    for n in excl["names"]:
        if n and n in hay:
            return True
    for i in excl["ips"]:
        if i and i in hay:
            return True
    return False


def find_existing(db, mac=None, ip=None):
    """Match an existing record by MAC (case/separator-insensitive) first, then
    by exact IP. None when nothing matches."""
    m = normalize_mac(mac)
    if m:
        row = db.query(Device).filter(_MAC_NORMALIZED == m).first()
        if row:
            return row
    ipn = normalize_ip(ip)
    if ipn:
        row = db.query(Device).filter(Device.ip_address == ipn).first()
        if row:
            return row
    return None


def _is_generic_name(value) -> bool:
    v = (value or "").strip()
    if not v:
        return True
    low = v.lower()
    return low in ("unknown", "") or low.startswith("discovered-") \
        or low.startswith("unifi-")


def upsert_discovered(db, *, mac=None, ip=None, name=None, hostname=None,
                      device_type=None, vendor=None, model=None,
                      status=None, claimed=False, unifi_managed=False,
                      tags=None, source="discovery", env=None):
    """Match-before-insert for discovery writers.

    Returns ``(outcome, device)`` where outcome is one of:
      ``"added"``            — new record created
      ``"updated"``          — existing record refreshed in place
      ``"skipped_self"``     — identity is the appliance itself (never recorded)
      ``"skipped_claimed"``  — existing record is claimed and the discovery
                               conflicts with its identity (logged + skipped)
    """
    excl = self_exclusion(env)
    if is_self_identity(ip=ip, mac=mac, name=name, hostname=hostname, excl=excl):
        logger.info("discovery: self-excluded %s/%s (%s)", ip, mac, source)
        return "skipped_self", None

    existing = find_existing(db, mac, ip)
    now = datetime.utcnow()

    if existing is None:
        if not ip and not mac:
            return "skipped_self", None   # nothing to key a record on
        fallback = f"discovered-{normalize_ip(ip).replace('.', '-')}" if ip \
            else f"mac-{normalize_mac(mac)}"
        d = Device(
            name=name or fallback,
            hostname=hostname,
            ip_address=normalize_ip(ip) or "",
            mac_address=mac or None,
            device_type=device_type or "unknown",
            vendor=vendor,
            model=model,
            status=status or "unknown",
            claimed=claimed,
            unifi_managed=unifi_managed,
            device_group="default",
            tags=list(tags or []),
            last_seen=now,
        )
        db.add(d)
        db.flush()
        return "added", d

    # A claimed record's identity is the user's, not the scanner's.
    if existing.claimed:
        conflict = False
        if mac and existing.mac_address \
                and normalize_mac(mac) != normalize_mac(existing.mac_address):
            conflict = True
        if (name and not _is_generic_name(name) and existing.name
                and not _is_generic_name(existing.name)
                and name.strip().lower() != (existing.name or "").strip().lower()):
            conflict = True
        if conflict:
            logger.warning(
                "discovery: collision — %s (%s/%s) conflicts with claimed "
                "record #%s %s (%s/%s); leaving the claimed record untouched",
                source, ip, mac, existing.id, existing.name,
                existing.ip_address, existing.mac_address)
            return "skipped_claimed", existing
        # Safe refresh only — reachability + last_seen, never identity fields.
        # "unknown" carries no reachability signal, so it never degrades a
        # claimed record's live status.
        if status and status != "unknown":
            existing.status = status
        existing.last_seen = now
        return "updated", existing

    # Unclaimed: refresh identity + metadata in place (one record per box).
    if mac:
        existing.mac_address = existing.mac_address or mac
    if ip and not existing.ip_address:
        existing.ip_address = normalize_ip(ip)
    if name and (not existing.name or _is_generic_name(existing.name)):
        existing.name = name
    if hostname and (not existing.hostname or _is_generic_name(existing.hostname)):
        existing.hostname = hostname
    if device_type and (not existing.device_type
                        or existing.device_type in ("unknown", "")):
        existing.device_type = device_type
    existing.vendor = existing.vendor or vendor
    existing.model = existing.model or model
    # "unknown" carries no reachability signal — never let it degrade a real
    # status an earlier sweep/link-monitor already learned.
    if status and (status != "unknown" or not existing.status):
        existing.status = status
    if unifi_managed:
        existing.unifi_managed = True
    if tags:
        merged = list(existing.tags or [])
        for t in tags:
            if t not in merged:
                merged.append(t)
        existing.tags = merged
    existing.last_seen = now
    return "updated", existing
