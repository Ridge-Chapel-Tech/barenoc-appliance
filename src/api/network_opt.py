"""Network Optimization — scan orchestrator + collectors (P1, read-only).

Runs the audit pipeline entirely IN-PROCESS in the API container (never via
the pi/LLM agent job loop — the self-protection invariant): UniFi (one
long-lived controller session), SNMP (snmpget/snmpwalk subprocesses), and
nmap/ping (-T3/-T2 profile, capped, no intrusive scripts).

Cost knobs are first-class (.env, hot-reloaded):
  NETOPT_ENABLED           "true" (default) — master switch
  NETOPT_MAX_HOSTS         25            — cap on gear hosts scanned per run
  NETOPT_SCAN_PROFILE      standard      — "standard" (-T3, top-100) | "light" (-T2, top-50)
  NETOPT_CONCURRENCY       5             — per-host collector concurrency
  NETOPT_DEFAULT_SCHEDULE  (JSON)        — override the default weekly Sun 03:00 schedule

The scan writes one ScanRun + N Finding rows; findings are advisory only.
"""

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from database import SessionLocal
from models import Device, ScanRun, Finding
from network_opt_rules import (
    SCHEMA_VERSION, GEAR_TYPES, evaluate, score, count_rules,
    build_port_discovery,
    build_vlan_map, vlan_story,

)

logger = logging.getLogger("barenoc.netopt")

ENV_PATH = "/opt/barenoc/.env"
DEFAULT_MAX_HOSTS = 25
DEFAULT_CONCURRENCY = 5
DEFAULT_PROFILE = "standard"
PROFILES = {
    "standard": {"timing": "3", "top_ports": 100},
    "light": {"timing": "2", "top_ports": 50},
}

# In-process registry of active runs (progress + cancellation). A scan is
# advisory and short-lived; restart-safety is handled by reconciling stale
# "running" rows on the next start (not by persisting progress).
_ACTIVE = {}   # run_id -> {"cancel": threading.Event, "progress": dict}


# ── env / config ───────────────────────────────────────────────────────────

def _read_env() -> dict:
    """Read /opt/barenoc/.env, falling back to process env for NETOPT_* keys."""
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
    for k, v in os.environ.items():
        if k.startswith("NETOPT_"):
            env.setdefault(k, v)
    return env


def _env_bool(value, default=False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _env_int(value, default) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def netopt_config(env: dict = None) -> dict:
    """Normalized NETOPT knobs (validated + clamped)."""
    env = env if env is not None else _read_env()
    enabled = _env_bool(env.get("NETOPT_ENABLED"), True)
    max_hosts = _env_int(env.get("NETOPT_MAX_HOSTS"), DEFAULT_MAX_HOSTS)
    max_hosts = max(1, min(max_hosts, 250))
    concurrency = _env_int(env.get("NETOPT_CONCURRENCY"), DEFAULT_CONCURRENCY)
    concurrency = max(1, min(concurrency, 16))
    profile = str(env.get("NETOPT_SCAN_PROFILE") or DEFAULT_PROFILE).strip().lower()
    if profile not in PROFILES:
        profile = DEFAULT_PROFILE
    default_schedule = _default_schedule(env.get("NETOPT_DEFAULT_SCHEDULE"))
    return {
        "enabled": enabled,
        "max_hosts": max_hosts,
        "concurrency": concurrency,
        "profile": profile,
        "default_schedule": default_schedule,
    }


def _default_schedule(raw) -> dict:
    """Parse NETOPT_DEFAULT_SCHEDULE (JSON) into {mode, day, hour, enabled}.
    Falls back to the user-approved default: recurring, weekly Sunday, 03:00
    local, DISABLED (a scan must be explicitly enabled/scheduled)."""
    d = {"mode": "recurring", "day": "0", "hour": 3, "enabled": False}
    if not raw:
        return d
    try:
        parsed = json.loads(str(raw))
        if isinstance(parsed, dict):
            mode = str(parsed.get("mode", "recurring")).lower()
            if mode in ("recurring", "onetime"):
                d["mode"] = mode
            day = str(parsed.get("day", "0"))
            if day in ("daily", "0", "1", "2", "3", "4", "5", "6"):
                d["day"] = day
            hour = _env_int(parsed.get("hour"), 3)
            if 0 <= hour <= 23:
                d["hour"] = hour
            d["enabled"] = _env_bool(parsed.get("enabled"), False)
    except (ValueError, TypeError):
        pass
    return d


# ── self-protection ────────────────────────────────────────────────────────

def self_identifiers(env: dict = None) -> list:
    """The appliance's own identity — never a scan target (hard invariant)."""
    env = env if env is not None else _read_env()
    raw = (env.get("APPLIANCE_IP") or os.environ.get("APPLIANCE_IP") or "").strip()
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    ids += ["127.0.0.1", "localhost", "bareNOC.local", "app.barenoc.com"]
    if "barenoc" not in [x.lower() for x in ids]:
        ids.append("bareNOC")
    return [x for x in ids if x]


def is_self(device, self_ids: list) -> bool:
    """True when a device (row or dict) matches the appliance's own identity."""
    hay = " ".join([
        str(getattr(device, "name", None) or (device.get("name") if isinstance(device, dict) else "") or ""),
        str(getattr(device, "ip_address", None) or (device.get("ip") if isinstance(device, dict) else "") or ""),
        str(getattr(device, "hostname", None) or (device.get("hostname") if isinstance(device, dict) else "") or ""),
    ]).lower()
    for sid in self_ids:
        s = str(sid).strip().lower()
        if not s:
            continue
        if s in hay:
            return True
    return False


# ── scope ──────────────────────────────────────────────────────────────────

def build_scope(db, config: dict = None, env: dict = None) -> dict:
    """The scan scope: discovered/onboarded NETWORK GEAR ONLY (gateway/router/
    switch/ap, or UniFi-managed), minus the appliance itself, capped by
    NETOPT_MAX_HOSTS. NO endpoints/servers are ever scanned."""
    config = config or netopt_config(env)
    self_ids = self_identifiers(env)
    rows = db.query(Device).filter(Device.claimed.is_(True)).all()
    included, excluded = [], []
    for d in rows:
        if is_self(d, self_ids):
            excluded.append({"name": d.name, "ip": d.ip_address, "reason": "self"})
            continue
        is_gear = (d.device_type or "unknown").lower() in GEAR_TYPES
        if is_gear or d.unifi_managed:
            included.append(d)
        # everything else (servers/endpoints/workstations/iot) is out of scope
    # deterministic order + host cap
    included.sort(key=lambda d: (d.ip_address or "", d.name or ""))
    if len(included) > config["max_hosts"]:
        included = included[:config["max_hosts"]]
    return {
        "devices": included,
        "excluded": excluded,
        "max_hosts": config["max_hosts"],
        "self_ids": self_ids,
    }


# ── low-level collectors (subprocess) ──────────────────────────────────────

def _run(cmd: list, timeout: int) -> dict:
    """Run a subprocess; return {ok, stdout, stderr}. Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "stdout": r.stdout or "",
                "stderr": r.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"timeout after {timeout}s"}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e)}


def _binary(name: str) -> bool:
    return shutil.which(name) is not None


def collect_ping(ip: str) -> dict:
    """ICMP reachability + latency via ping (iputils-ping in the api image)."""
    if not _binary("ping"):
        return {"reachable": None, "latency_ms": None, "error": "ping not installed"}
    r = _run(["ping", "-c", "3", "-W", "2", ip], timeout=10)
    if not r["ok"]:
        return {"reachable": False, "latency_ms": None}
    m = re.search(r"= ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms", r["stdout"])
    if m:
        return {"reachable": True, "latency_ms": float(m.group(2))}
    return {"reachable": True, "latency_ms": None}


def _guess_ttl(ip: str) -> "int | None":
    r = _run(["ping", "-c", "1", "-W", "2", ip], timeout=5)
    m = re.search(r"ttl=(\d+)", r["stdout"], re.I)
    return int(m.group(1)) if m else None


def parse_nmap_grepable(text: str) -> dict:
    """Parse nmap -oG - output into open_ports + services. Pure + testable."""
    ports = []
    services = {}
    for line in (text or "").splitlines():
        if not line.startswith("Host:") or "Ports:" not in line:
            continue
        seg = line.split("Ports:", 1)[1].split("\t", 1)[0].strip()
        for part in seg.split(", "):
            fields = part.split("/")
            if len(fields) >= 3 and fields[1] == "open":
                try:
                    port = int(fields[0])
                except ValueError:
                    continue
                ports.append(port)
                if len(fields) >= 5:
                    services[str(port)] = fields[4] or fields[3] or ""
    ports = sorted(set(ports))
    return {"open_ports": ports, "open_services": services}


def guess_os(open_ports: list, ttl: "int | None") -> str:
    """Deterministic OS guess from open ports + TTL (gear-focused, no probes
    beyond the connect scan)."""
    ports = set(open_ports or [])
    if 161 in ports and (80 in ports or 443 in ports):
        return "Managed network device (SNMP + web UI)"
    if 23 in ports:
        return "Network device (telnet)"
    if 445 in ports or 139 in ports:
        return "Windows (SMB ports open)"
    if 22 in ports:
        return "Unix/Linux (SSH open)" if (ttl and ttl <= 64) else "Unix-like (SSH open)"
    if 9100 in ports:
        return "Printer (raw port 9100)"
    if ttl:
        if ttl > 100:
            return "Windows (TTL %d)" % ttl
        if ttl <= 64:
            return "Unix/Linux (TTL %d)" % ttl
        return "Network device (TTL %d)" % ttl
    return "Unknown"


def collect_nmap(ip: str, profile: str = "standard") -> dict:
    """TCP connect scan (-sT, no root needed), no intrusive scripts, capped
    ports. -T3 (standard) / -T2 (light)."""
    if not _binary("nmap"):
        return {"open_ports": [], "open_services": {}, "os": "",
                "error": "nmap not installed"}
    p = PROFILES.get(profile, PROFILES["standard"])
    r = _run(["nmap", "-sT", "-Pn", f"-T{p['timing']}",
              "--top-ports", str(p["top_ports"]), "--open",
              "--host-timeout", "45s", "-oG", "-", ip], timeout=70)
    if not r["ok"] and "Ports:" not in r["stdout"]:
        return {"open_ports": [], "open_services": {}, "os": "",
                "error": (r["stderr"] or "nmap failed").strip()[:200]}
    parsed = parse_nmap_grepable(r["stdout"])
    parsed["os"] = guess_os(parsed["open_ports"], _guess_ttl(ip))
    return parsed


# ── SNMP collector ─────────────────────────────────────────────────────────

_SNMP_BASE = "1.3.6.1.2.1"
_IFTABLE = f"{_SNMP_BASE}.2.2.1"
_DOT3_DUPLEX = f"{_SNMP_BASE}.10.7.2.1.19"
_OIDS = {
    "ifindex": f"{_IFTABLE}.1",
    "ifdescr": f"{_IFTABLE}.2",
    "iftype": f"{_IFTABLE}.3",
    "ifmtu": f"{_IFTABLE}.4",
    "ifspeed": f"{_IFTABLE}.5",
    "ifadmin": f"{_IFTABLE}.7",
    "ifoper": f"{_IFTABLE}.8",
    "ifinucast": f"{_IFTABLE}.11",
    "ifindiscards": f"{_IFTABLE}.13",
    "ifinerrors": f"{_IFTABLE}.14",
    "ifoutucast": f"{_IFTABLE}.17",
    "ifoutdiscards": f"{_IFTABLE}.19",
    "ifouterrors": f"{_IFTABLE}.20",
    "ifhighspeed": "1.3.6.1.2.1.31.1.1.1.15",
}
_DUPLEX = {
    1: "unknown",
    2: "half",
    3: "full",
}


def _parse_snmp_value(tagged: str):
    """Parse 'TYPE: value' (or bare value) into a python scalar."""
    t = (tagged or "").strip()
    if not t:
        return None
    if ": " in t:
        typ, _, val = t.partition(": ")
        typ = typ.strip().lower()
    else:
        typ, val = "", t
    if typ == "string":
        return val.strip().strip('"')
    if typ in ("integer", "gauge32", "counter32", "counter64", "unsigned32", "gauge"):
        try:
            return int(val.split()[0])
        except (ValueError, IndexError):
            return None
    if typ == "timeticks":
        m = re.search(r"\((\d+)\)", val)
        if m:
            return int(m.group(1))
        return None
    # numeric OID get -Onqv returns a bare value
    try:
        return int(val)
    except ValueError:
        return val


def _snmp_walk_table(ip, community, version) -> dict:
    """Walk the ifTable columns; return {ifindex: {col: value}}."""
    if not _binary("snmpwalk"):
        return {}
    args = _snmp_args("snmpwalk", ip, community, version)
    args += ["-On", "-t", "3", "-r", "1", _IFTABLE]
    r = _run(args, timeout=30)
    table = {}
    for line in r["stdout"].splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        oid, _, value = line.partition("=")
        oid = oid.strip()
        # column = the digit just before the interface index
        parts = [p for p in oid.split(".") if p != ""]
        if len(parts) < 2:
            continue
        col = parts[-2]
        ifindex = parts[-1]
        # map the column OID back to a name
        col_name = _oid_to_col(oid)
        if not col_name:
            continue
        row = table.setdefault(ifindex, {})
        row[col_name] = _parse_snmp_value(value.strip())
    return table


def _oid_to_col(oid: str) -> "str | None":
    # find which known OID prefix this line belongs to (longest match)
    best, best_len = None, -1
    for name, base in _OIDS.items():
        if oid.startswith(base + "."):
            if len(base) > best_len:
                best, best_len = name, len(base)
    return best


def _snmp_args(bin_name, ip, community, version):
    args = [bin_name, f"-v{version}"]
    if version == "3":
        args += ["-u", community.get("user", "noc"),
                 "-a", community.get("auth_proto", "SHA"),
                 "-A", community.get("auth_pass", ""),
                 "-x", community.get("priv_proto", "AES"),
                 "-X", community.get("priv_pass", ""),
                 "-l", "authPriv"]
    else:
        args += ["-c", community if isinstance(community, str) else "public"]
    return args


def collect_snmp(ip: str, community: str = "public", version: str = "2c") -> dict:
    """Poll sysDescr/sysName/sysUpTime + ifTable + UCD-SNMP CPU/mem. Returns
    the normalized ``snmp`` snapshot block or None when the device doesn't
    answer SNMP."""
    if not _binary("snmpget"):
        return {"error": "snmpget not installed", "interfaces": []}
    comm = community if isinstance(community, str) else "public"
    out = {"version": version, "community": comm, "interfaces": []}

    def get(oid):
        args = _snmp_args("snmpget", ip, community, version)
        args += ["-Onqv", "-t", "3", "-r", "1", oid]
        r = _run(args, timeout=8)
        return _parse_snmp_value(r["stdout"].strip()) if r["ok"] else None

    # system group (1.3.6.1.2.1.1) — the trailing `.0` instance, NOT an extra
    # `.1` before it (the 2026-08-18 wrong-OID bug). Aligned with telemetry.
    descr = get(f"{_SNMP_BASE}.1.1.0")           # sysDescr   1.3.6.1.2.1.1.1.0
    if descr is None:
        return None  # no SNMP response
    out["sysdescr"] = str(descr)
    out["sysname"] = get(f"{_SNMP_BASE}.1.5.0")  # sysName    1.3.6.1.2.1.1.5.0
    up = get(f"{_SNMP_BASE}.1.3.0")              # sysUpTime  1.3.6.1.2.1.1.3.0
    out["uptime_seconds"] = int(up / 100) if isinstance(up, int) and up > 0 else None
    # UCD-SNMP (Linux/Net-SNMP hosts only — Cisco/Juniper ignore these)
    load = get("1.3.6.1.4.1.2021.10.1.3.1")
    out["cpu_load"] = float(load) if isinstance(load, (int, float)) else None
    mem_total = get("1.3.6.1.4.1.2021.4.5.0")
    mem_avail = get("1.3.6.1.4.1.2021.4.6.0")
    if isinstance(mem_total, int) and isinstance(mem_avail, int) and mem_total > 0:
        out["mem_used_pct"] = round((mem_total - mem_avail) / mem_total * 100, 1)

    table = _snmp_walk_table(ip, community, version)
    # ifHighSpeed (Mbps) — needed for links >2.4 Gbps where the Gauge32
    # ifSpeed saturates at 4294967295.
    for ifindex, mbps in _snmp_walk_highspeed(ip, community, version).items():
        table.setdefault(ifindex, {})["ifhighspeed"] = mbps
    # dot3StatsDuplexStatus is a separate table (per-ifIndex under 10.7.2.1.19)
    duplex_by_index = _snmp_walk_duplex(ip, community, version)
    for ifindex, row in table.items():
        speed = row.get("ifspeed")
        highspeed = row.get("ifhighspeed")
        if isinstance(speed, int) and 0 < speed < 4294967295:
            speed_mbps = int(speed // 1_000_000)
        elif isinstance(highspeed, int):
            speed_mbps = int(highspeed)
        else:
            speed_mbps = None
        oper = row.get("ifoper")
        admin = row.get("ifadmin")
        out["interfaces"].append({
            "ifindex": ifindex,
            "ifdescr": row.get("ifdescr") or f"if{ifindex}",
            "iftype": row.get("iftype"),
            "oper_status": {1: "up", 2: "down", 3: "testing"}.get(oper, "unknown"),
            "admin_status": {1: "up", 2: "down", 3: "testing"}.get(admin, "unknown"),
            "speed_mbps": speed_mbps,
            "duplex": _DUPLEX.get(duplex_by_index.get(ifindex), "unknown"),
            "mtu": row.get("ifmtu"),
            "in_errors": row.get("ifinerrors"),
            "out_errors": row.get("ifouterrors"),
            "in_discards": row.get("ifindiscards"),
            "out_discards": row.get("ifoutdiscards"),
            "in_pkts": row.get("ifinucast"),
            "out_pkts": row.get("ifoutucast"),
        })
    return out


def _snmp_walk_duplex(ip, community, version) -> dict:
    """Walk dot3StatsDuplexStatus (1.3.6.1.2.1.10.7.2.1.19) -> {ifindex: 1|2|3}."""
    if not _binary("snmpwalk"):
        return {}
    args = _snmp_args("snmpwalk", ip, community, version)
    args += ["-On", "-t", "3", "-r", "1", _DOT3_DUPLEX]
    r = _run(args, timeout=20)
    out = {}
    for line in r["stdout"].splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        oid, _, value = line.partition("=")
        oid = oid.strip()  # no trailing space — '1 ' would create a phantom ifindex
        ifindex = oid.rsplit(".", 1)[-1]
        v = _parse_snmp_value(value.strip())
        if isinstance(v, int):
            out[ifindex] = v
    return out


def _snmp_walk_highspeed(ip, community, version) -> dict:
    """Walk ifHighSpeed (1.3.6.1.2.1.31.1.1.1.15, Mbps) -> {ifindex: mbps}."""
    if not _binary("snmpwalk"):
        return {}
    args = _snmp_args("snmpwalk", ip, community, version)
    args += ["-On", "-t", "3", "-r", "1", "1.3.6.1.2.1.31.1.1.1.15"]
    r = _run(args, timeout=20)
    out = {}
    for line in r["stdout"].splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        oid, _, value = line.partition("=")
        oid = oid.strip()  # no trailing space — '1 ' would create a phantom ifindex
        ifindex = oid.rsplit(".", 1)[-1]
        v = _parse_snmp_value(value.strip())
        if isinstance(v, int):
            out[ifindex] = v
    return out


# ── UniFi collector (ONE long-lived session for the whole run) ────────────

def _as_int(value) -> int:
    """Coerce a port_table counter/value to int, tolerating None/str forms."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _port_mac_table_count(pt: dict) -> int:
    """Learned-MAC count for a port: ``mac_table_count`` when present, else
    the length of the ``mac_table`` list (older firmware spells)."""
    count = pt.get("mac_table_count")
    if count is not None:
        try:
            return int(count)
        except (TypeError, ValueError):
            pass
    mac_table = pt.get("mac_table")
    if isinstance(mac_table, list):
        return len(mac_table)
    return 0


def _mac_string(v) -> str:
    """Normalize a MAC-ish value (string or {mac:...} dict) to lowercase colon
    form — '' when not a usable MAC. Best-effort, never raises."""
    if isinstance(v, dict):
        v = v.get("mac") or v.get("mac_address") or ""
    h = re.sub(r"[^0-9a-fA-F]", "", str(v or ""))
    if len(h) < 12:
        return ""
    h = h[:12].lower()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def _port_macs(pt: dict) -> list:
    """The learned MAC list for a port (``mac_table`` list entries) — ''-safe,
    deduped, best-effort. The firmware hides port→MAC on some builds, so this
    may be empty even when a device is connected."""
    out = []
    mac_table = (pt or {}).get("mac_table")
    if isinstance(mac_table, list):
        for entry in mac_table:
            m = _mac_string(entry)
            if m and m not in out:
                out.append(m)
    return out


def _effective_port_profile(pt: dict, ov: dict) -> dict:
    """The port's EFFECTIVE name/native/tagged — the port_overrides entry wins
    when it sets the field (the 08-19 HouseSwitch port 7 fix: the override set
    native=Default while the port_table had no native, so the 'no profile'
    rule fired on a CONFIGURED port)."""
    pt = pt or {}
    ov = ov or {}
    return {
        "name": ov.get("name") or pt.get("name") or f"Port {pt.get('port_idx')}",
        "native_id": ov.get("native_networkconf_id") or pt.get("native_networkconf_id"),
        "tagged_ids": ov.get("tagged_networkconf_id") or pt.get("tagged_networkconf_id") or "",
    }


def _clients_by_port(clients: list) -> dict:
    """Map (switch_mac, port_idx) -> list of client records (hostname/mac/oui)
    for the client-list correlation (OUI + DHCP hostname best-effort)."""
    out = {}
    for c in (clients or []):
        if not isinstance(c, dict):
            continue
        sw_mac = (c.get("sw_mac") or "").strip().lower()
        port = c.get("sw_port")
        if not sw_mac or port is None:
            continue
        try:
            port = int(port)
        except (TypeError, ValueError):
            continue
        out.setdefault((sw_mac, port), []).append(c)
    return out


def _uplinks_by_port(raw_devices) -> dict:
    """Map (switch_mac, port_idx) -> list of KNOWN managed device names that
    report their uplink on that switch port (the controller's uplink data)."""
    out = {}
    for d in raw_devices or []:
        up = d.get("uplink") or {}
        sw_mac = (up.get("uplink_mac") or "").strip().lower()
        if not sw_mac:
            continue
        port = up.get("uplink_remote_port")
        if port is None:
            continue
        try:
            port = int(port)
        except (TypeError, ValueError):
            continue
        name = d.get("name") or d.get("mac") or "unknown"
        out.setdefault((sw_mac, port), [])
        if name not in out[(sw_mac, port)]:
            out[(sw_mac, port)].append(name)
    return out


def collect_unifi(config: dict, client=None) -> dict:
    """Collect networks/wlans/device health + ports over one controller
    session. ``client`` is an already-logged-in UniFiClient (or None to
    build+login one from UNIFI_* env). Returns
    {"devices_by_mac": {...}, "networks": [...], "wlans": [...]}."""
    if client is None:
        client = _unifi_client_from_env()
        if client is None:
            return {"devices_by_mac": {}, "networks": [], "wlans": [],
                    "error": "UniFi not configured"}
        if not client.login():
            return {"devices_by_mac": {}, "networks": [], "wlans": [],
                    "error": f"UniFi login failed: {client.last_error or 'unknown'}"}

    raw = client.get_raw_devices() or []
    networks = client.get_networks()
    # raw wlanconf for wpa_enc + networkconf_id (get_wlans omits wpa_enc)
    wlans_raw = (client._stat("rest/wlanconf") or {}).get("data", []) or []
    nets_by_id = _networks_by_id(client)
    # port -> KNOWN managed devices that uplink on it (the controller's uplink data)
    uplinks = _uplinks_by_port(raw)
    # client-list correlation (OUI + DHCP hostname) — fail-soft. The
    # stat/dhcpd_lease endpoint 404s on this build, so the client list is the
    # primary path; leases are a best-effort supplement.
    clients = []
    try:
        clients = client.get_clients() or []
    except Exception:
        clients = []
    clients_by_port = _clients_by_port(clients)
    leases = []
    try:
        leases = client.get_dhcp_leases() or []
    except Exception:
        leases = []
    lease_hostnames = {}
    for lease in leases:
        m = (lease.get("mac") or "").strip().lower()
        host = (lease.get("hostname") or "").strip()
        if m and host:
            lease_hostnames.setdefault(m, host)

    def _vlan(network_id):
        return nets_by_id.get(network_id, {}).get("vlan")

    def _name(network_id):
        rec = nets_by_id.get(network_id, {})
        return rec.get("name") or None

    devices_by_mac = {}
    for d in raw:
        mac = (d.get("mac") or "").lower()
        if not mac:
            continue
        wan_count = len([k for k in ("wan1", "wan2") if d.get(k)])
        wan = None
        if d.get("type") in ("udm", "ugw", "ucg", "usg"):
            health = client.get_wan_health(d.get("mac"))
            wan = {"status": (health or {}).get("status") or "",
                   "wan_count": wan_count or 1}
        overrides = {_as_int(o.get("port_idx")): o for o in (d.get("port_overrides") or [])}
        ports = []
        for pt in (d.get("port_table") or []):
            port_idx = pt.get("port_idx")
            ov = overrides.get(_as_int(port_idx)) or {}
            prof = _effective_port_profile(pt, ov)
            native_id = prof["native_id"]
            tagged_ids = prof["tagged_ids"]
            learned_macs = _port_macs(pt)
            port_clients = clients_by_port.get((mac, _as_int(port_idx)), [])
            client_macs = [m for m in (_mac_string(c.get("mac")) for c in port_clients) if m]
            connected_mac = (client_macs[0] if client_macs else
                             (learned_macs[0] if learned_macs else ""))
            client_hostnames = [str(c.get("hostname") or c.get("name") or "").strip()
                                for c in port_clients]
            client_hostnames = [h for h in client_hostnames if h]
            dhcp_hostname = ""
            if client_hostnames:
                dhcp_hostname = client_hostnames[0]
            elif connected_mac:
                dhcp_hostname = lease_hostnames.get(connected_mac.lower(), "")

            ports.append({
                "port_idx": port_idx,
                "name": prof["name"],
                "up": bool(pt.get("up")),
                "disabled": bool(ov.get("disabled", False)),
                "speed_mbps": pt.get("speed"),
                "max_speed_mbps": pt.get("max_speed"),
                "native_vlan": _vlan(native_id),
                "native_network": _name(native_id),
                "tagged_vlans": [_vlan(x) for x in tagged_ids.split(",") if x],
                "tagged_networks": [n for n in (
                    _name(x) for x in tagged_ids.split(",") if x) if n],
                "link_down_count": pt.get("link_down_count") or 0,
                "tx_errors": pt.get("tx_errors") or 0,
                "rx_errors": pt.get("rx_errors") or 0,
                "is_uplink": bool(pt.get("is_uplink")),
                "mac_table_count": _port_mac_table_count(pt),
                "rx_packets": _as_int(pt.get("rx_packets")),
                "tx_packets": _as_int(pt.get("tx_packets")),
                "tx_multicast": _as_int(pt.get("tx_multicast")),
                "stp_state": pt.get("stp_state"),
                "uplink_devices": uplinks.get((mac, _as_int(port_idx)), []),
                "learned_macs": learned_macs,
                "client_macs": client_macs,
                "connected_mac": connected_mac or None,
                "dhcp_hostname": dhcp_hostname or None,
            })
        devices_by_mac[mac] = {
            "name": d.get("name") or mac,
            "ip": _live_ip(d),
            "status": "online" if d.get("state", 0) == 1 else "offline",
            "model": d.get("model", ""),
            "version": d.get("version", ""),
            "upgradable": bool(d.get("upgradable")),
            "uptime_seconds": d.get("uptime"),
            "uplink_mac": (d.get("uplink") or {}).get("uplink_mac") or "",
            "fixed_ip": None,   # resolved below against rest/user if present
            "wan": wan,
            "ports": ports,
        }

    # DHCP reservation status for gear that also appears as a client (rare)
    fixed_by_mac = {}
    try:
        users = (client._stat("rest/user") or {}).get("data", []) or []
        for u in users:
            mac = (u.get("mac") or "").lower()
            if mac:
                fixed_by_mac[mac] = bool(u.get("use_fixedip"))
    except Exception:
        pass
    for mac, rec in devices_by_mac.items():
        if mac in fixed_by_mac:
            rec["fixed_ip"] = fixed_by_mac[mac]

    wlans = []
    for w in wlans_raw:
        if not isinstance(w, dict):
            continue
        wlans.append({
            "name": w.get("name", ""),
            "enabled": bool(w.get("enabled", False)),
            "security": w.get("security") or "open",
            "wpa_mode": w.get("wpa_mode") or "",
            "wpa_enc": w.get("wpa_enc") or "",
            "vlan": _vlan(w.get("networkconf_id")),
        })

    return {"devices_by_mac": devices_by_mac, "networks": networks,
            "wlans": wlans}


def _networks_by_id(client) -> dict:
    nets = {}
    try:
        raw = (client._stat("rest/networkconf") or {}).get("data", []) or []
        for n in raw:
            if not isinstance(n, dict):
                continue
            nets[n.get("_id", "")] = {
                "name": n.get("name", ""),
                "vlan": (n.get("vlan") if n.get("vlan_enabled") else None),
                "subnet": n.get("ip_subnet", ""),
                "purpose": n.get("purpose", ""),
                "enabled": bool(n.get("enabled", True)),
            }
    except Exception:
        pass
    return nets


def _unifi_client_from_env():
    from unifi import UniFiClient
    env = _read_env()
    url = (env.get("UNIFI_URL") or "").strip()
    if not url or url.startswith("https://192.0.2.1"):
        return None
    return UniFiClient(
        url,
        env.get("UNIFI_USER", "admin"),
        env.get("UNIFI_PASSWORD", ""),
        api_key=env.get("UNIFI_API_KEY") or None,
    )


def _live_ip(d: dict) -> str:
    """The device's current management IP from a raw stat/device entry:
    ``config_network.ip`` (static/configured) preferred, falling back to the
    last-known ``ip``. This is the authority IP for UniFi-managed gear."""
    cn = d.get("config_network") or {}
    return (cn.get("ip") or d.get("ip") or "").strip()


# ── per-device snapshot builder ────────────────────────────────────────────

def collect_device(device: Device, config: dict, unifi_rec: dict = None) -> dict:
    """Run ping/nmap/snmp for ONE device. UniFi data is merged in later (it's
    collected once at the run level via a shared session).

    ``unifi_rec`` is the controller's live snapshot for this device's MAC (when
    it is UniFi-managed). For UniFi-managed gear the CONTROLLER is the
    authority for reachability + the live IP — a stale DB ``ip_address`` (the
    08-18 incident) must never produce a false ``offline_gear`` critical while
    the controller shows the device UP."""
    ip = device.ip_address
    unifi_managed = bool(device.unifi_managed)
    if unifi_managed and unifi_rec:
        ip = unifi_rec.get("ip") or ip
    snap = {
        "device_id": device.id,
        "name": device.name,
        "ip": ip,
        "mac": device.mac_address,
        "device_type": (device.device_type or "unknown").lower(),
        "vendor": device.vendor,
        "model": device.model,
        "unifi_managed": unifi_managed,
        "ping": None,
        "nmap": None,
        "snmp": None,
        "unifi": None,
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
    }
    if not ip:
        return snap
    if unifi_managed and unifi_rec:
        # Controller-live authority: reachability comes from the controller's
        # snapshot, never an ICMP probe of a possibly-stale record IP. (Many
        # managed devices also drop ICMP while being perfectly online.)
        online = unifi_rec.get("status") == "online"
        snap["ping"] = {"reachable": online, "latency_ms": None, "source": "unifi"}
        if online:
            # Still port-scan/SNMP the LIVE ip for security/performance
            # findings (nmap -sT -Pn does not depend on ICMP).
            snap["nmap"] = collect_nmap(ip, config["profile"])
            snap["snmp"] = _collect_snmp(device, ip)
        return snap
    # Non-UniFi (or UniFi-managed with no controller record): record/scan path.
    snap["ping"] = collect_ping(ip)
    snap["nmap"] = collect_nmap(ip, config["profile"])
    snap["snmp"] = _collect_snmp(device, ip)
    return snap


def _collect_snmp(device: Device, ip: str) -> dict:
    """Decrypt + poll SNMP for a device (None when SNMP is not configured)."""
    if not device.snmp_community:
        return None
    from crypto import decrypt
    try:
        community = decrypt(device.snmp_community)
    except Exception:
        community = None
    if community:
        return collect_snmp(ip, community, "2c")
    return None


# ── run execution ──────────────────────────────────────────────────────────

def reconcile_stale_runs(db):
    """Mark any 'running' scan from a previous process as cancelled (a scan is
    advisory and never resumed across restarts)."""
    stale = db.query(ScanRun).filter(ScanRun.status == "running").all()
    for r in stale:
        r.status = "cancelled"
        r.finished_at = datetime.utcnow()
    if stale:
        db.commit()


def execute_scan(db, run_id: int, config: dict = None, cancel_event=None,
                 triggered_by: str = "manual", collect=None) -> dict:
    """Full pipeline: scope → collectors → rules → score → persist.

    ``collect`` is an optional override: collect(device, config) -> device
    snapshot, and ``collect_unifi`` is imported at module level (tests patch
    ``network_opt.collect_device`` / ``network_opt.collect_unifi``)."""
    config = config or netopt_config()
    run = db.query(ScanRun).get(run_id)
    if not run:
        raise ValueError(f"no ScanRun {run_id}")
    run.status = "running"
    run.started_at = datetime.utcnow()
    run.triggered_by = triggered_by
    db.commit()

    scope = build_scope(db, config)
    scope_summary = {
        "devices": [d.id for d in scope["devices"]],
        "excluded": scope["excluded"],
        "max_hosts": scope["max_hosts"],
    }
    run.scope = scope_summary
    db.commit()

    progress = _active_progress(run_id, cancel_event)
    progress.update({"stage": "collecting", "done": 0,
                     "total": len(scope["devices"]), "current": ""})

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "scope": scope_summary,
        "devices": [],
        "networks": [],
        "wlans": [],
        "vlan_map": {},
        "meta": {"collector_errors": [], "hosts_scanned": 0,
                 "profile": config["profile"]},
    }

    cancelled = False
    try:
        # UniFi once (one long-lived session), then per-device in parallel.
        unifi_data = collect_unifi(config)
        if unifi_data.get("error"):
            snapshot["meta"]["collector_errors"].append(
                {"channel": "unifi", "error": unifi_data["error"]})
        snapshot["networks"] = unifi_data.get("networks") or []
        snapshot["wlans"] = unifi_data.get("wlans") or []
        snapshot["vlan_map"] = build_vlan_map(snapshot["networks"])
        by_mac = unifi_data.get("devices_by_mac") or {}

        device_snaps = []
        workers = max(1, config["concurrency"])
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {}
            for d in scope["devices"]:
                if cancel_event is not None and cancel_event.is_set():
                    break
                unifi_rec = by_mac.get((d.mac_address or "").lower())
                futs[pool.submit(collect if collect else collect_device, d, config, unifi_rec)] = d
            for fut in as_completed(futs):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    for f in futs:
                        f.cancel()
                    break
                d = futs[fut]
                try:
                    dev_snap = fut.result()
                except Exception as e:
                    dev_snap = {"device_id": d.id, "name": d.name, "ip": d.ip_address,
                                "error": str(e)}
                    snapshot["meta"]["collector_errors"].append(
                        {"device": d.name, "error": str(e)})
                if dev_snap:
                    # merge the once-collected UniFi record for this device
                    if d.mac_address and (d.mac_address.lower() in by_mac):
                        dev_snap["unifi"] = by_mac[d.mac_address.lower()]
                    device_snaps.append(dev_snap)
                    progress["done"] = len(device_snaps)
                    progress["current"] = d.name or d.ip_address

        if cancelled:
            progress["stage"] = "cancelled"
            run.status = "cancelled"
            run.finished_at = datetime.utcnow()
            db.commit()
            return {"run_id": run.id, "status": "cancelled"}

        snapshot["devices"] = device_snaps
        snapshot["meta"]["hosts_scanned"] = len(device_snaps)
        findings = evaluate(snapshot)
        return _finalize(db, run, snapshot, findings)
    except Exception as e:
        logger.exception("netopt scan %s failed", run_id)
        run.status = "failed"
        run.finished_at = datetime.utcnow()
        run.summary = json.dumps({"error": str(e)})
        db.commit()
        return {"run_id": run.id, "status": "failed", "error": str(e)}
    finally:
        progress["stage"] = "done" if progress.get("stage") != "cancelled" else "cancelled"


def build_vlan_context(snapshot: dict) -> list:
    """Per-device, per-port VLAN context for the run detail — the STORY
    ('native WiFi vlan5 (.5.1/24), tagged Kids(9)/RCTF(10)') beside the
    discovery + findings so the optimize flow is visibly subnet-aware."""
    vlan_map = snapshot.get("vlan_map") or build_vlan_map(snapshot.get("networks") or [])
    out = []
    for d in snapshot.get("devices") or []:
        ports = (d.get("unifi") or {}).get("ports") or []
        if not ports:
            continue
        entries = []
        for p in ports:
            entries.append({
                "port_idx": p.get("port_idx"),
                "name": p.get("name"),
                "up": bool(p.get("up")),
                "is_uplink": bool(p.get("is_uplink")),
                "native_vlan": p.get("native_vlan"),
                "native_network": p.get("native_network"),
                "tagged_vlans": p.get("tagged_vlans"),
                "tagged_networks": p.get("tagged_networks"),
                "story": vlan_story(p, vlan_map),
            })
        out.append({
            "device_id": d.get("device_id"),
            "name": d.get("name"),
            "ip": d.get("ip"),
            "ports": entries,
        })
    return out


def probe_capture_capability() -> dict:
    """Declared tool state for the future SOC-appliance packet-capture
    integration (light). This scan identifies devices WITHOUT packet capture;
    the probe reports whether a mirror/native capture capability exists. On
    current gear (UCG-Max 4.x UniFi OS) there is no SPAN/mirror/tcpdump/PBR
    channel exposed to the appliance, so it is ABSENT by construction."""
    return {
        "packet_capture": {
            "available": False,
            "channels": ["mirror", "native_tcpdump", "pbr"],
            "note": ("capture/mirror capability not present on this gear — "
                     "device identification uses controller-exposed signals "
                     "(traffic archetype, OUI, DHCP hostname) instead; packet "
                     "capture lands on the future SOC appliance."),
        },
    }


def _finalize(db, run: ScanRun, snapshot: dict, findings: list) -> dict:
    """Score + persist findings + finalize the run. Exposed for tests."""
    sc = score(findings)
    run.score = sc["overall"]
    run.finished_at = datetime.utcnow()
    run.status = "completed"
    run.schema_version = SCHEMA_VERSION
    run.summary = json.dumps({
        "schema_version": SCHEMA_VERSION,
        "score": sc["overall"],
        "categories": sc["categories"],
        "counts": sc["counts"],
        "category_counts": sc["category_counts"],
        "total": sc["total"],
        "rules_evaluated": count_rules(),
        "meta": snapshot.get("meta") or {},
        "port_discovery": build_port_discovery(snapshot),
        "network_map": snapshot.get("vlan_map") or {},
        "vlan_context": build_vlan_context(snapshot),
        "capabilities": probe_capture_capability(),

    })
    for f in findings:
        db.add(Finding(
            run_id=run.id,
            finding_key=f["finding_key"],
            category=f["category"],
            severity=f["severity"],
            device_id=f.get("device_id"),
            interface=f.get("interface"),
            title=f["title"],
            detail=f.get("detail", ""),
            evidence=f.get("evidence") or {},
        ))
    db.commit()
    return {"run_id": run.id, "status": "completed", "score": sc["overall"],
            "total_findings": len(findings), "summary": json.loads(run.summary)}


def _active_progress(run_id: int, cancel_event) -> dict:
    if run_id not in _ACTIVE:
        _ACTIVE[run_id] = {
            "cancel": cancel_event or threading.Event(),
            "progress": {"stage": "queued", "done": 0, "total": 0, "current": ""},
        }
    return _ACTIVE[run_id]["progress"]


def run_progress(run_id: int) -> dict:
    rec = _ACTIVE.get(run_id)
    return dict(rec["progress"]) if rec else {"stage": "unknown"}


def cancel_run(run_id: int) -> bool:
    rec = _ACTIVE.get(run_id)
    if rec:
        rec["cancel"].set()
        return True
    return False


def start_scan(run_id: int, triggered_by: str = "manual"):
    """Spawn the scan in a background thread (own DB session)."""
    def _target():
        db = SessionLocal()
        try:
            reconcile_stale_runs(db)
            cancel = threading.Event()
            execute_scan(db, run_id, cancel_event=cancel,
                         triggered_by=triggered_by)
        finally:
            db.close()
    t = threading.Thread(target=_target, daemon=True, name=f"netopt-{run_id}")
    t.start()
    return t
