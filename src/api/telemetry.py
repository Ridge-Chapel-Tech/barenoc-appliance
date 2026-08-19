"""Telemetry collectors + background engine (time-series backbone, P0).

Runs IN-PROCESS in the API container (same principle as NetOpt — never the
pi/LLM agent loop, and never writes back to gear): a long-lived UniFi
controller session, SNMP subprocesses (snmpget/snmpwalk), and light ping.

Cadences are modest + configurable (.env, hot-reloaded each cycle):
  TELEMETRY_ENABLED            "true" (default)  — master switch
  TELEMETRY_UNIFI_INTERVAL_S   60    — one long-lived controller session
  TELEMETRY_SNMP_INTERVAL_S    300   — ifOperStatus/bytes -> rate, CPU/RAM/uptime
  TELEMETRY_PING_INTERVAL_S    60    — latency + packet loss, capped
  TELEMETRY_PING_MAX_DEVICES   50    — light ping cap (never hammer a big fleet)
  STARLINK_INTERVAL_S          60    — Starlink dish gRPC poll (see starlink.py)
  TELEMETRY_RETENTION_DAYS     30    — store retention (pruned by the scheduler)
  TELEMETRY_DISK_MIN_FREE_PCT  10    — disk-aware prune floor

Samples are written via metrics_store.write_samples (batched); bandwidth is
stored as a RATE (bytes/sec) computed here from consecutive counter reads, so
the store never has to know a channel's raw units. The scheduler owns
retention (see scheduler/main.py prune_telemetry).
"""

import datetime
import logging
import os
import re
import threading
import time

from database import SessionLocal
from models import Device
import metrics_store
import network_opt
import starlink

logger = logging.getLogger("barenoc.telemetry")

ENV_PATH = "/opt/barenoc/.env"

DEFAULTS = {
    "enabled": True,
    "unifi_interval": 60,
    "snmp_interval": 300,
    "ping_interval": 60,
    "ping_max_devices": 50,
    "starlink_interval": 60,
    "retention_days": metrics_store.DEFAULT_RETENTION_DAYS,
    "disk_min_free_pct": metrics_store.DEFAULT_MIN_FREE_PCT,
}

_SNMP_BASE = "1.3.6.1.2.1"
_IFTABLE = f"{_SNMP_BASE}.2.2.1"
# ifTable columns we record -> their column sub-identifier under 1.3.6.1.2.1.2.2.1
_TELEM_IF_COLS = {"ifdescr": "2", "ifoper": "8", "ifinoctets": "10", "ifoutoctets": "16"}
_UCD_CPU_1MIN = "1.3.6.1.4.1.2021.10.1.3.1"
_UCD_MEM_TOTAL = "1.3.6.1.4.1.2021.4.5.0"
_UCD_MEM_AVAIL = "1.3.6.1.4.1.2021.4.6.0"

# Long-lived UniFi session (the login-rate-limit lesson): one client + one
# login, reused across polls. Rebuilt lazily on first use; re-login on failure.
_UNIFI = {"client": None, "logged_in": False, "env_url": None}

# Counter -> rate state: (device_id, metric) -> (prev_ts, prev_raw_value).
# In-memory only; a restart simply skips the first rate interval (no bogus
# spikes), which is exactly the behaviour we want.
_COUNTER_PREV = {}


# ── config ─────────────────────────────────────────────────────────────────

def _read_env() -> dict:
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
        if k.startswith("TELEMETRY_"):
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


def telemetry_config(env: dict = None) -> dict:
    env = env if env is not None else _read_env()
    return {
        "enabled": _env_bool(env.get("TELEMETRY_ENABLED"), DEFAULTS["enabled"]),
        "unifi_interval": max(15, _env_int(env.get("TELEMETRY_UNIFI_INTERVAL_S"),
                                           DEFAULTS["unifi_interval"])),
        "snmp_interval": max(30, _env_int(env.get("TELEMETRY_SNMP_INTERVAL_S"),
                                          DEFAULTS["snmp_interval"])),
        "ping_interval": max(15, _env_int(env.get("TELEMETRY_PING_INTERVAL_S"),
                                          DEFAULTS["ping_interval"])),
        "ping_max_devices": max(1, min(_env_int(env.get("TELEMETRY_PING_MAX_DEVICES"),
                                                DEFAULTS["ping_max_devices"]), 500)),
        "starlink_interval": max(15, _env_int(env.get("STARLINK_INTERVAL_S"),
                                              DEFAULTS["starlink_interval"])),
        "retention_days": max(1, _env_int(env.get("TELEMETRY_RETENTION_DAYS"),
                                          DEFAULTS["retention_days"])),
        "disk_min_free_pct": max(1, min(_env_int(env.get("TELEMETRY_DISK_MIN_FREE_PCT"),
                                                 DEFAULTS["disk_min_free_pct"]), 99)),
    }


# ── sample helpers ─────────────────────────────────────────────────────────

def _gauge(device_id, metric, value, ts) -> dict:
    return {"device_id": device_id, "metric": metric, "value": float(value),
            "ts": ts, "kind": "gauge"}


def _counter(device_id, metric, raw_value, ts) -> dict:
    """``metric`` is the RATE metric name; ``value`` is the raw cumulative
    counter that compute_counter_rates turns into bytes/sec."""
    return {"device_id": device_id, "metric": metric, "value": float(raw_value),
            "ts": ts, "kind": "counter"}


def compute_counter_rates(prev: dict, curr: dict) -> dict:
    """Pure + testable: turn two consecutive counter reads into rates.

    prev/curr: {(device_id, metric): (ts_datetime, raw_value)}. Returns
    {(device_id, metric): rate} for entries seen in BOTH with a positive time
    delta and a monotonic (non-rolled-over) counter. Counter resets, zero
    deltas and first-seen counters are skipped (no bogus spike)."""
    out = {}
    for key, (cts, cval) in curr.items():
        p = prev.get(key)
        if not p:
            continue
        pts, pval = p
        dt = (cts - pts).total_seconds()
        if dt <= 0:
            continue
        delta = cval - pval
        if delta < 0:
            continue  # counter reset / rollover
        out[key] = delta / dt
    return out


# ── UniFi collector ────────────────────────────────────────────────────────

def _unifi_client():
    """Long-lived UniFi session (built + logged-in once)."""
    cfg_url = network_opt._unifi_client_from_env()
    if cfg_url is None:
        _UNIFI["client"] = None
        _UNIFI["logged_in"] = False
        return None
    url = cfg_url.base_url
    if _UNIFI["client"] is None or _UNIFI["env_url"] != url:
        _UNIFI["client"] = cfg_url
        _UNIFI["env_url"] = url
        _UNIFI["logged_in"] = False
    if not _UNIFI["logged_in"]:
        if _UNIFI["client"].login():
            _UNIFI["logged_in"] = True
        else:
            return None
    return _UNIFI["client"]


def collect_unifi_metrics(client, device_map: dict) -> list:
    """Per-device counters from stat/device over one long-lived session:
    state, uptime, optional device latency/health, and per-port rx/tx bytes
    (as raw counters -> engine computes bandwidth rate). ``device_map`` maps
    lowercased MAC -> BareNOC device id."""
    raw = client.get_raw_devices()
    if raw is None:
        # Controller unreachable / auth lost — force a fresh login next cycle.
        _UNIFI["logged_in"] = False
        return []
    samples = []
    now = datetime.datetime.utcnow()
    for d in raw or []:
        mac = (d.get("mac") or "").lower()
        device_id = device_map.get(mac)
        if not device_id:
            continue
        state = d.get("state")
        if isinstance(state, (int, float)):
            samples.append(_gauge(device_id, "unifi.state", 1 if state == 1 else 0, now))
        up = d.get("uptime")
        if isinstance(up, (int, float)):
            samples.append(_gauge(device_id, "unifi.uptime_seconds", float(up), now))
        lat = d.get("latency")
        if isinstance(lat, (int, float)):
            samples.append(_gauge(device_id, "unifi.latency_ms", float(lat), now))
        # device-level throughput counters (some gateways/APs expose these)
        stat = d.get("stat") or {}
        if isinstance(stat, dict):
            for dirn in ("rx", "tx"):
                v = stat.get(f"{dirn}_bytes")
                if isinstance(v, (int, float)):
                    samples.append(_counter(device_id, f"unifi.{dirn}_bps", float(v), now))
        for pt in (d.get("port_table") or []):
            idx = pt.get("port_idx")
            if idx is None:
                continue
            if isinstance(pt.get("up"), bool):
                samples.append(_gauge(device_id, f"unifi.port.{idx}.up",
                                      1 if pt["up"] else 0, now))
            for dirn in ("rx", "tx"):
                v = pt.get(f"{dirn}_bytes")
                if isinstance(v, (int, float)):
                    samples.append(_counter(device_id, f"unifi.port.{idx}.{dirn}_bps",
                                            float(v), now))
    return samples


# ── ping collector ─────────────────────────────────────────────────────────

_PING_LATENCY = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")
_PING_LOSS = re.compile(r"(\d+)% packet loss")


def ping_sample(ip: str, run=None) -> dict:
    """One ping probe: {reachable, latency_ms, loss_pct}. ``run`` is an
    injectable subprocess runner (tests pass a fake)."""
    run = run or network_opt._run
    if not network_opt._binary("ping"):
        return {"reachable": None, "latency_ms": None, "loss_pct": None}
    r = run(["ping", "-c", "3", "-W", "2", ip], timeout=10)
    out = {"reachable": False, "latency_ms": None, "loss_pct": None}
    m = _PING_LATENCY.search(r.get("stdout", ""))
    if m:
        out["latency_ms"] = float(m.group(2))
    m = _PING_LOSS.search(r.get("stdout", ""))
    if m:
        out["loss_pct"] = float(m.group(1))
    if r.get("ok"):
        out["reachable"] = True
    elif out["loss_pct"] is None:
        out["loss_pct"] = 100.0
    return out


def collect_ping_metrics(device_id: int, ip: str) -> list:
    """Gauge samples for one device's ping probe."""
    p = ping_sample(ip)
    if p.get("reachable") is None:
        return []
    now = datetime.datetime.utcnow()
    samples = [_gauge(device_id, "ping.reachable", 1 if p["reachable"] else 0, now)]
    if p.get("latency_ms") is not None:
        samples.append(_gauge(device_id, "ping.latency_ms", p["latency_ms"], now))
    if p.get("loss_pct") is not None:
        samples.append(_gauge(device_id, "ping.loss_pct", p["loss_pct"], now))
    return samples


# ── SNMP collector ─────────────────────────────────────────────────────────

def _snmp_get(ip, community, version, oid):
    if not network_opt._binary("snmpget"):
        return None
    args = network_opt._snmp_args("snmpget", ip, community, version)
    args += ["-Onqv", "-t", "3", "-r", "1", oid]
    r = network_opt._run(args, timeout=8)
    return network_opt._parse_snmp_value(r["stdout"].strip()) if r["ok"] else None


def _snmp_walk_iftable(ip, community, version) -> dict:
    """Walk ifTable -> {ifindex: {ifdescr, ifoper, ifinoctets, ifoutoctets}}."""
    if not network_opt._binary("snmpwalk"):
        return {}
    args = network_opt._snmp_args("snmpwalk", ip, community, version)
    args += ["-On", "-t", "3", "-r", "1", _IFTABLE]
    r = network_opt._run(args, timeout=30)
    table = {}
    for line in r["stdout"].splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        oid, _, value = line.partition("=")
        parts = [p for p in oid.strip().split(".") if p]
        if len(parts) < 2:
            continue
        col = _TELEM_IF_COLS.get(parts[-2])
        if not col:
            continue
        row = table.setdefault(parts[-1], {})
        row[col] = network_opt._parse_snmp_value(value.strip())
    return table


def snmp_sample(ip: str, community: str = "public", version: str = "2c") -> dict:
    """One SNMP poll: system identity + CPU/RAM/uptime + ifTable octets/status.

    Returns None when the device doesn't answer SNMP (no sysDescr)."""
    descr = _snmp_get(ip, community, version, f"{_SNMP_BASE}.1.1.0")
    if descr is None:
        return None
    out = {"sysdescr": str(descr)}
    up = _snmp_get(ip, community, version, f"{_SNMP_BASE}.1.3.0")
    out["uptime_seconds"] = int(up / 100) if isinstance(up, int) and up > 0 else None
    load = _snmp_get(ip, community, version, _UCD_CPU_1MIN)
    out["cpu_pct"] = float(load) if isinstance(load, (int, float)) else None
    mem_total = _snmp_get(ip, community, version, _UCD_MEM_TOTAL)
    mem_avail = _snmp_get(ip, community, version, _UCD_MEM_AVAIL)
    if isinstance(mem_total, int) and isinstance(mem_avail, int) and mem_total > 0:
        out["mem_used_pct"] = round((mem_total - mem_avail) / mem_total * 100, 1)
    out["interfaces"] = [
        {"ifindex": ifindex,
         "ifdescr": row.get("ifdescr") or f"if{ifindex}",
         "oper_status": row.get("ifoper"),
         "rx_octets": row.get("ifinoctets"),
         "tx_octets": row.get("ifoutoctets")}
        for ifindex, row in sorted(_snmp_walk_iftable(ip, community, version).items())
    ]
    return out


def collect_snmp_metrics(device_id: int, ip: str, community: str,
                         version: str = "2c") -> list:
    """Gauge + counter samples for one SNMP device."""
    snap = snmp_sample(ip, community, version)
    if not snap:
        return []
    now = datetime.datetime.utcnow()
    samples = []
    if snap.get("uptime_seconds") is not None:
        samples.append(_gauge(device_id, "snmp.uptime_seconds", snap["uptime_seconds"], now))
    if snap.get("cpu_pct") is not None:
        samples.append(_gauge(device_id, "snmp.cpu_pct", snap["cpu_pct"], now))
    if snap.get("mem_used_pct") is not None:
        samples.append(_gauge(device_id, "snmp.mem_used_pct", snap["mem_used_pct"], now))
    for itf in snap.get("interfaces", []):
        key = re.sub(r"[^A-Za-z0-9]+", "_", str(itf.get("ifdescr") or "")).strip("_") \
            or str(itf.get("ifindex"))
        oper = itf.get("oper_status")
        if oper in (1, 2):
            samples.append(_gauge(device_id, f"snmp.if.{key}.oper_status",
                                  1 if oper == 1 else 0, now))
        if isinstance(itf.get("rx_octets"), (int, float)):
            samples.append(_counter(device_id, f"snmp.if.{key}.rx_bps",
                                    float(itf["rx_octets"]), now))
        if isinstance(itf.get("tx_octets"), (int, float)):
            samples.append(_counter(device_id, f"snmp.if.{key}.tx_bps",
                                    float(itf["tx_octets"]), now))
    return samples


# ── engine ─────────────────────────────────────────────────────────────────

def _device_maps(db):
    devices = db.query(Device).filter(Device.claimed.is_(True)).all()
    self_ids = network_opt.self_identifiers()
    mac_map = {(d.mac_address or "").lower(): d.id for d in devices if d.mac_address}
    pingable = [d for d in devices if d.ip_address and not network_opt.is_self(d, self_ids)]
    pingable.sort(key=lambda d: (d.ip_address or "", d.name or ""))
    snmp_devs = [d for d in devices if d.ip_address and d.snmp_community
                 and not network_opt.is_self(d, self_ids)]
    return mac_map, pingable, snmp_devs


def _persist(samples: list, db) -> int:
    gauges = [s for s in samples if s["kind"] == "gauge"]
    counters = [s for s in samples if s["kind"] == "counter"]
    curr = {(s["device_id"], s["metric"]): (s["ts"], s["value"]) for s in counters}
    rates = compute_counter_rates(_COUNTER_PREV, curr)
    _COUNTER_PREV.update(curr)

    out = [{"device_id": s["device_id"], "metric": s["metric"], "ts": s["ts"],
            "value": s["value"]} for s in gauges]
    now = datetime.datetime.utcnow()
    for (device_id, metric), rate in rates.items():
        out.append({"device_id": device_id, "metric": metric, "ts": now, "value": rate})
    return metrics_store.write_samples(db, out)


def collect_once(cfg: dict = None, db=None, channels=None) -> dict:
    """One collection pass for the requested channels (all by default).
    Uses its own DB session when ``db`` is None. Testable via injectable
    collectors (tests patch telemetry.collect_unifi_metrics etc.)."""
    cfg = cfg or telemetry_config()
    channels = set(channels) if channels else {"unifi", "snmp", "ping", "starlink"}
    own = db is None
    if own:
        db = SessionLocal()
    try:
        mac_map, pingable, snmp_devs = _device_maps(db)
        summary = {"unifi": 0, "snmp": 0, "ping": 0, "starlink": 0, "samples": 0}
        unifi_samples = []
        snmp_samples = []
        ping_samples = []
        starlink_samples = []

        if "unifi" in channels:
            client = _unifi_client()
            if client is not None:
                unifi_samples = collect_unifi_metrics(client, mac_map)
                summary["unifi"] = len(unifi_samples)

        if "snmp" in channels:
            from crypto import decrypt
            for d in snmp_devs:
                try:
                    community = decrypt(d.snmp_community)
                except Exception:
                    community = None
                if not community:
                    continue
                try:
                    snmp_samples += collect_snmp_metrics(d.id, d.ip_address, community)
                except Exception as e:
                    logger.warning("SNMP telemetry failed for %s: %s", d.name, e)
            summary["snmp"] = len(snmp_samples)

        if "ping" in channels:
            for d in pingable[: cfg["ping_max_devices"]]:
                try:
                    ping_samples += collect_ping_metrics(d.id, d.ip_address)
                except Exception as e:
                    logger.warning("ping telemetry failed for %s: %s", d.name, e)
            summary["ping"] = len(ping_samples)

        if "starlink" in channels:
            try:
                starlink_samples += starlink.collect_starlink_telemetry(cfg, db)
            except Exception as e:
                logger.warning("starlink telemetry failed: %s", e)
            summary["starlink"] = len(starlink_samples)

        summary["samples"] = _persist(
            unifi_samples + snmp_samples + ping_samples + starlink_samples, db)
        return summary
    finally:
        if own:
            db.close()


def _engine_loop():
    logger.info("Telemetry engine starting")
    last = {"unifi": 0, "snmp": 0, "ping": 0, "starlink": 0}
    while True:
        try:
            cfg = telemetry_config()
            if not cfg["enabled"]:
                time.sleep(30)
                continue
            now = time.time()
            if now - last["unifi"] >= cfg["unifi_interval"]:
                collect_once(cfg, channels={"unifi"})
                last["unifi"] = now
            if now - last["snmp"] >= cfg["snmp_interval"]:
                collect_once(cfg, channels={"snmp"})
                last["snmp"] = now
            if now - last["ping"] >= cfg["ping_interval"]:
                collect_once(cfg, channels={"ping"})
                last["ping"] = now
            if now - last["starlink"] >= cfg["starlink_interval"]:
                collect_once(cfg, channels={"starlink"})
                last["starlink"] = now
        except Exception:
            logger.exception("telemetry engine cycle error")
        time.sleep(10)


_engine = None


def start_telemetry_engine():
    """Idempotently start the background telemetry engine."""
    global _engine
    if _engine is None:
        _engine = threading.Thread(target=_engine_loop, daemon=True,
                                   name="telemetry-engine")
        _engine.start()
    return _engine
