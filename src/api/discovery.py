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


def _is_hex_mac(m) -> bool:
    """A normalized MAC is a hex string of at least 12 chars (EUI-48)."""
    return bool(m) and len(m) >= 12 and all(c in "0123456789abcdef" for c in m)


def is_randomized_mac(mac) -> bool:
    """True when a MAC has the locally-administered bit set in its first octet
    (02/06/0A/0E/12/16/… patterns — the signature of a private/randomized
    address). Real, globally-assigned MACs (e.g. bc:24:11:… Proxmox VMs share a
    prefix but are distinct boxes) never carry this bit, so matching by prefix
    alone is never a fold."""
    m = normalize_mac(mac)
    if not _is_hex_mac(m):
        return False
    try:
        first_octet = int(m[:2], 16)
    except ValueError:
        return False
    return bool(first_octet & 0x02)


def _fold_eligible(device) -> bool:
    """A record may be folded INTO only when it is a plain unclaimed discovery
    row: not claimed, not linked/revoked, and holding no control channel.
    A claimed/linked identity or an SSH/SNMP/UniFi/agent channel must never be
    silently folded into (channels/control never auto-fold)."""
    if device is None:
        return False
    if device.claimed:
        return False
    if (device.adoption_status or "none") in ("linked", "revoked"):
        return False
    if device.adoption_method and device.adoption_method != "none":
        return False
    if device.ssh_key_fingerprint or device.snmp_community:
        return False
    if device.unifi_managed:
        return False
    for ch in (device.channels or []):
        if ch != "monitor":
            return False
    return True


def _identity_match(device, *, name=None, hostname=None, vendor=None) -> bool:
    """Strict identity match for a fold: the sighting and the record describe
    the SAME device (same name case-insensitively, or same hostname). Different
    names NEVER fold (even when a hostname happens to match), and vendor/OUI
    fields must agree or be absent."""
    def _n(v):
        return (v or "").strip().lower()
    name = _n(name)
    hostname = _n(hostname)
    vendor = _n(vendor)
    dev_name = _n(device.name)
    dev_host = _n(device.hostname)
    dev_vendor = _n(device.vendor)

    name_match = (not _is_generic_name(name) and not _is_generic_name(dev_name)
                  and name == dev_name)
    host_match = (not _is_generic_name(hostname) and not _is_generic_name(dev_host)
                  and hostname == dev_host)
    # "different names never fold" — a conflicting non-generic name wins even
    # when a hostname happens to match.
    names_conflict = (not _is_generic_name(name) and not _is_generic_name(dev_name)
                      and name != dev_name)
    if names_conflict:
        return False
    if not (name_match or host_match):
        return False
    if vendor and dev_vendor and vendor != dev_vendor:
        return False
    return True


def find_fold_target(db, *, mac=None, name=None, hostname=None, vendor=None):
    """Find an existing UNCLAIMED discovery record to fold a randomized-MAC
    sighting into, by strict identity (name/hostname) — never by prefix alone.
    Returns None when the sighting is not a randomized MAC or nothing matches."""
    if not is_randomized_mac(mac):
        return None
    candidates = db.query(Device).filter(Device.claimed.is_(False)).all()
    matches = [d for d in candidates
               if _fold_eligible(d)
               and _identity_match(d, name=name, hostname=hostname, vendor=vendor)]
    if not matches:
        return None
    # Deterministic winner: most recently seen first, then highest id.
    matches.sort(key=lambda d: (d.last_seen is not None,
                                d.last_seen or datetime.min, d.id), reverse=True)
    return matches[0]


def record_mac_sighting(device, mac, *, ip=None, source="discovery", when=None):
    """Record a MAC sighting on the canonical record. The FIRST sighting fills
    ``mac_address``; subsequent distinct MACs append to ``mac_history`` (a
    randomized device presents a fresh MAC per network — each is retained, but
    only one row exists). Returns the normalized MAC when something changed,
    else None (duplicate or empty)."""
    m = normalize_mac(mac)
    if not _is_hex_mac(m):
        return None
    if not normalize_mac(device.mac_address):
        device.mac_address = mac
        return m
    if m == normalize_mac(device.mac_address):
        return None
    history = list(device.mac_history or [])
    for entry in history:
        if isinstance(entry, dict) and normalize_mac(entry.get("mac")) == m:
            return None
    history.append({
        "mac": mac,
        "ip": normalize_ip(ip) or "",
        "source": source or "discovery",
        "seen": (when or datetime.utcnow()).isoformat(),
    })
    device.mac_history = history
    return m


def fold_sighting(db, target, *, mac=None, ip=None, name=None, hostname=None,
                  vendor=None, status=None, source="discovery"):
    """Fold a randomized-MAC sighting into an existing unclaimed record.

    The canonical record is kept (name/MAC identity intact); the sighting
    refreshes last_seen/status/IP and its MAC is appended to ``mac_history``
    (no data loss) instead of INSERTing a duplicate row. Audited via the audit
    + change-log paths (event_type ``device_sighting_folded``, actor = the
    discovery source).
    """
    now = datetime.utcnow()
    target.last_seen = now
    if ip:
        target.ip_address = normalize_ip(ip)
    if status and (status != "unknown" or not target.status):
        target.status = status
    if name and (not target.name or _is_generic_name(target.name)):
        target.name = name
    if hostname and (not target.hostname or _is_generic_name(target.hostname)):
        target.hostname = hostname
    target.vendor = target.vendor or vendor
    record_mac_sighting(target, mac, ip=ip, source=source, when=now)
    db.flush()
    from audit import log_event
    from change_log import record
    record(db, event_type="device_sighting_folded", actor=source or "discovery",
           asset=target.name,
           summary=f"Folded a randomized-MAC sighting into {target.name}",
           detail=(f"New MAC {mac} (ip {ip or '-'}) folded into device "
                   f"#{target.id} '{target.name}'; "
                   f"{len(target.mac_history or [])} sighting(s) retained"),
           links={"device_id": target.id, "mac": mac, "ip": ip or ""},
           customer_visible=False)
    log_event(db, "device_sighting_folded", source or "discovery", {
        "device_id": target.id, "mac": mac, "ip": ip or ""})
    return target


def upsert_discovered(db, *, mac=None, ip=None, name=None, hostname=None,
                      device_type=None, vendor=None, model=None,
                      status=None, claimed=False, unifi_managed=False,
                      tags=None, source="discovery", env=None):
    """Match-before-insert for discovery writers.

    Returns ``(outcome, device)`` where outcome is one of:
      ``"added"``            — new record created
      ``"updated"``          — existing record refreshed in place
      ``"folded"``           — randomized-MAC same-identity sighting folded
                               into an existing unclaimed record
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
        # Randomized-MAC fold (phone-mac-fold): a private/randomized address is
        # a NEW MAC every network join, so MAC/IP matching can never collapse
        # it. When the sighting matches an existing unclaimed record by strict
        # identity (name/hostname), fold it in instead of inserting a row.
        # find_existing stays MAC/IP-first for real devices.
        if is_randomized_mac(mac):
            target = find_fold_target(db, mac=mac, name=name, hostname=hostname,
                                      vendor=vendor)
            if target is not None:
                fold_sighting(db, target, mac=mac, ip=ip, name=name,
                              hostname=hostname, vendor=vendor, status=status,
                              source=source)
                return "folded", target
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
