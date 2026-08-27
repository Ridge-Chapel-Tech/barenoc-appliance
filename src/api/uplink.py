"""Uplink / ISP status — vendor-agnostic uplink health for the Devices page.

One read surface for the "Uplink / ISP" card. Fallback order (locked design):

  1. Starlink dish metrics (the existing ``starlink.*`` store) when a live
     dish exists — Starlink owners get the dish stats they had on the System
     page, now on Devices.
  2. UniFi gateway WAN (``wan1``/``wan2`` + ``stat/health``) — ISP name + WAN
     IP + link health + any latency/throughput/uptime the controller exposes.
     Reuses link_monitor's WAN-health path (same controller session + cache),
     so no duplicate logins.
  3. The appliance's own egress probe (gateway + a well-known internet host)
     as the fallback when no dish and no UniFi gateway data — reachability +
     latency to 8.8.8.8/1.1.1.1-style targets.

Pure, testable helpers + one builder (``uplink_status(db)``) consumed by the
read-only route. No new deps; the UniFi read and the probe both reuse config
the appliance already has.
"""

import datetime
import logging
import os
import re
import subprocess
import threading
import time

from models import Device, LinkEpisode
import starlink

logger = logging.getLogger("barenoc.uplink")

PROBE_HOST_DEFAULT = "8.8.8.8"
PROBE_GATEWAY_DEFAULT = ""     # empty = derive from the gateway device record
PROBE_CACHE_TTL = 30           # seconds between egress probes (shared cache)

# A doc-IP / empty gateway must never be probed (mirrors alerting's guard —
# probing 192.0.2.1 forever produces a permanent fake "link down").
_DOC_IPS = ("192.0.2.", "203.0.113.", "198.51.100.")


# ── config (hot-read from .env, os.environ fallback) ────────────────────────

def _read_env() -> dict:
    env = {}
    try:
        with open("/opt/barenoc/.env") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def uplink_config(env: dict = None) -> dict:
    env = env if env is not None else _read_env()

    def s(key, default):
        return (env.get(key) or os.getenv(key, default) or default).strip()

    return {
        "probe_enabled": s("UPLINK_PROBE_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        # Reuse the internet-probe targets when configured; otherwise probe the
        # well-known host directly.
        "gateway": s("INTERNET_PROBE_GATEWAY", s("UPLINK_PROBE_GATEWAY", PROBE_GATEWAY_DEFAULT)),
        "host": s("INTERNET_PROBE_HOST", s("UPLINK_PROBE_HOST", PROBE_HOST_DEFAULT)),
    }


# ── egress probe (pure-ish, testable) ───────────────────────────────────────

def _ping(host: str, timeout_s: float = 2.0) -> tuple:
    """(reachable, latency_ms). Latency parsed from ``time=… ms``; None on gap."""
    if not host or host.lower() in ("none", "null"):
        return False, None
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            capture_output=True, text=True, timeout=timeout_s + 2)
    except Exception:
        return False, None
    if r.returncode != 0:
        return False, None
    m = re.search(r"time[=<]([\d.]+)\s*ms", r.stdout or "")
    latency = float(m.group(1)) if m else None
    return True, latency


def probe_egress(cfg: dict) -> dict:
    """One egress probe: LAN gateway + internet host from the appliance."""
    gw = cfg.get("gateway") or ""
    gw_ok, gw_ms = _ping(gw)
    inet_ok, inet_ms = _ping(cfg.get("host") or PROBE_HOST_DEFAULT)
    if gw:
        if gw_ok and inet_ok:
            state = "up"
        elif gw_ok:
            state = "isp_down"   # gateway reachable, internet not — ISP-side
        else:
            state = "link_down"  # gateway unreachable — LAN/physical side
    else:
        state = "up" if inet_ok else "down"
    return {
        "gateway": gw,
        "host": cfg.get("host") or PROBE_HOST_DEFAULT,
        "gateway_reachable": gw_ok,
        "internet_reachable": inet_ok,
        "gateway_ms": gw_ms,
        "internet_ms": inet_ms,
        "state": state,
    }


_PROBE_CACHE = {"value": None, "at": 0.0}
_PROBE_LOCK = threading.Lock()


def cached_probe(cfg: dict, probe_fn=None) -> dict:
    """Shared, cached egress probe (TTL PROBE_CACHE_TTL) so concurrent card
    refreshes don't stampede ping. ``probe_fn`` is injectable for tests."""
    probe_fn = probe_fn or probe_egress
    now = time.time()
    if (now - _PROBE_CACHE["at"]) < PROBE_CACHE_TTL and _PROBE_CACHE["value"] is not None:
        return _PROBE_CACHE["value"]
    with _PROBE_LOCK:
        if (time.time() - _PROBE_CACHE["at"]) < PROBE_CACHE_TTL and _PROBE_CACHE["value"] is not None:
            return _PROBE_CACHE["value"]
        value = probe_fn(cfg)
        _PROBE_CACHE["value"] = value
        _PROBE_CACHE["at"] = time.time()
        return value


# ── gateway + UniFi WAN ─────────────────────────────────────────────────────

def gateway_device(db) -> "Device | None":
    """The customer's gateway: prefer a claimed UniFi-managed gateway with a
    MAC (so stat/health/{mac} is queryable), then any claimed gateway."""
    q = (db.query(Device)
         .filter(Device.device_type == "gateway")
         .filter(Device.claimed.is_(True)))
    gws = q.order_by(Device.id.asc()).all()
    if not gws:
        return None
    for g in gws:
        if g.unifi_managed and g.mac_address:
            return g
    for g in gws:
        if g.mac_address:
            return g
    return gws[0]


def _wan_isp_hint(wan: dict) -> str:
    """Best-effort ISP name from the wan1/wan2 raw config. UniFi doesn't put a
    clean ISP name in stat/health, but a PPPoE service / WAN profile name is a
    useful hint. Returns '' when the build exposes nothing."""
    if not wan:
        return ""
    for key in ("wan1", "wan2"):
        w = wan.get(key)
        if not isinstance(w, dict):
            continue
        for field in ("isp_name", "isp", "pppoe_service", "name"):
            v = str(w.get(field) or "").strip()
            if v:
                return v
    return ""


def _wan_from_config(wan: dict) -> dict:
    """Normalize the wan1/wan2 config into {primary_ip, secondary_ip, isp_hint}."""
    out = {"primary_ip": "", "secondary_ip": "", "isp_hint": ""}
    if not wan:
        return out
    ips = []
    for key in ("wan1", "wan2"):
        w = wan.get(key)
        if isinstance(w, dict):
            ip = str(w.get("ip") or w.get("wan_ip") or "").strip()
            if ip and ip not in ips:
                ips.append(ip)
    out["primary_ip"] = ips[0] if ips else ""
    out["secondary_ip"] = ips[1] if len(ips) > 1 else ""
    out["isp_hint"] = _wan_isp_hint(wan)
    return out


# ── the builder ─────────────────────────────────────────────────────────────

def _gateway_info(g: "Device | None") -> "dict | None":
    if g is None:
        return None
    return {
        "name": g.name or g.hostname or f"gateway {g.id}",
        "model": g.model or "",
        "vendor": g.vendor or "",
        "ip": g.ip_address or "",
        "mac": g.mac_address or "",
        "status": g.status or "",
    }


def _link_state_from_wan(health: dict) -> str:
    if not health:
        return "unknown"
    st = str(health.get("status") or "").lower()
    if st in ("ok", "up"):
        return "up"
    if st:
        return "down"
    # status missing but up/internet_ok present
    if health.get("up") is True or health.get("internet_ok") is True:
        return "up"
    return "unknown"


def _flap_data(db, gateway_id: int) -> "dict | None":
    """Existing link-flap data for the gateway's WAN (interface == 'wan')."""
    if not gateway_id:
        return None
    ep = (db.query(LinkEpisode)
          .filter(LinkEpisode.device_id == gateway_id,
                  LinkEpisode.interface == "wan")
          .first())
    if ep is None:
        return None
    return {
        "state": ep.state,
        "flap_count": ep.flap_count or 0,
        "escalated": ep.escalated,
        "ticket_id": ep.ticket_id,
        "down_since": (ep.down_since.isoformat() + "Z"
                       if ep.down_since and ep.down_since.tzinfo is None
                       else (ep.down_since.isoformat() if ep.down_since else None)),
    }


def uplink_status(db, unifi_channel=None, probe_fn=None, env: dict = None) -> dict:
    """The single uplink-card payload. Never raises — every source degrades to
    an honest gap and the UI renders whatever is present."""
    cfg = uplink_config(env)
    sl = starlink.status_payload(db, cfg=starlink.starlink_config(env))
    has_dish = starlink.has_live_dish(db)

    gw = gateway_device(db)
    unifi = None
    if gw is not None and gw.mac_address:
        try:
            from link_monitor import UniFiChannel
            ch = unifi_channel if unifi_channel is not None else UniFiChannel()
            picture = ch.wan_picture(gw.mac_address)
            if picture:
                health = picture.get("health") or {}
                wan_cfg = _wan_from_config(picture.get("wan") or {})
                unifi = {
                    "status": health.get("status") or "",
                    "wan_ip": health.get("wan_ip") or wan_cfg["primary_ip"] or "",
                    "wan_ip_secondary": wan_cfg["secondary_ip"] or "",
                    "gateway_ip": health.get("gateway_ip") or "",
                    "latency_ms": health.get("latency"),
                    "uptime_seconds": health.get("uptime"),
                    "down_mbps": health.get("speedtest_download"),
                    "up_mbps": health.get("speedtest_upload"),
                    "speedtest_latency_ms": health.get("speedtest_latency"),
                    "internet_ok": health.get("internet_ok"),
                    "dns_ok": health.get("dns_ok"),
                    "isp_hint": wan_cfg["isp_hint"] or health.get("isp_name") or "",
                    "link_state": _link_state_from_wan(health),
                }
        except Exception as e:
            logger.warning("uplink: UniFi WAN read failed: %s", e)
            unifi = None

    # Fallback probe only when no dish and no usable UniFi WAN data.
    probe = None
    if not has_dish and (unifi is None or not unifi.get("wan_ip") and not unifi.get("status")):
        gw_probe = cfg.get("gateway") or (gw.ip_address if gw is not None else "")
        if cfg["probe_enabled"] and not gw_probe.lower().startswith(_DOC_IPS):
            try:
                pcfg = dict(cfg, gateway=gw_probe)
                probe = cached_probe(pcfg, probe_fn=probe_fn)
            except Exception as e:
                logger.warning("uplink: egress probe failed: %s", e)
                probe = None

    # Source precedence for the card's headline.
    if has_dish:
        source = "starlink"
        lu = sl.get("latest", {}).get("starlink.link_up")
        link_state = "up" if lu is None or lu >= 1 else "down"
    elif unifi is not None:
        source = "unifi"
        link_state = unifi["link_state"]
    elif probe is not None:
        source = "probe"
        link_state = "up" if probe.get("internet_reachable") else (probe.get("state") or "down")
    else:
        source = "none"
        link_state = "unknown"

    isp = {"name": "", "wan_ip": ""}
    if has_dish:
        isp = {"name": "Starlink", "wan_ip": (sl.get("address") or "").split(":")[0]}
    elif unifi is not None:
        isp = {"name": unifi.get("isp_hint") or "",
               "wan_ip": unifi.get("wan_ip") or "",
               "wan_ip_secondary": unifi.get("wan_ip_secondary") or ""}

    stats = {}
    if has_dish:
        L = sl.get("latest", {})
        stats = {
            "latency_ms": L.get("starlink.ping_ms"),
            "down_mbps": L.get("starlink.down_mbps"),
            "up_mbps": L.get("starlink.up_mbps"),
            "uptime_seconds": L.get("starlink.uptime_seconds"),
            "drop_rate": L.get("starlink.ping_drop_rate"),
        }
    elif unifi is not None:
        stats = {
            "latency_ms": unifi.get("latency_ms"),
            "down_mbps": unifi.get("down_mbps"),
            "up_mbps": unifi.get("up_mbps"),
            "uptime_seconds": unifi.get("uptime_seconds"),
            "drop_rate": None,
        }
    elif probe is not None:
        stats = {"latency_ms": probe.get("internet_ms"),
                 "down_mbps": None, "up_mbps": None,
                 "uptime_seconds": None, "drop_rate": None}

    return {
        "source": source,
        "isp": isp,
        "gateway": _gateway_info(gw),
        "link": {
            "state": link_state,
            "flap": _flap_data(db, gw.id) if gw is not None else None,
        },
        "stats": stats,
        "starlink": sl if has_dish else None,
        "probe": probe,
        "unifi": unifi,
        "latest_ts": sl.get("latest_ts") if has_dish else (
            datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            if source in ("unifi", "probe") else None),
    }
