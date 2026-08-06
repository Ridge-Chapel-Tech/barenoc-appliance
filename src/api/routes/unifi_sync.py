"""UniFi Controller integration — sync devices and status into BareNOC."""

import os
import re
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from urllib.parse import quote
from sqlalchemy.orm import Session
from datetime import datetime
from database import get_db
from models import Device, User
from schemas import DeviceResponse
from auth import get_current_user, require_role, require_any_role
from unifi import UniFiClient
from audit import log_event

router = APIRouter(prefix="/api/v1/unifi", tags=["unifi"])


def _read_unifi_env() -> dict:
    """Read UniFi config from the .env file (volume-mounted into the container)
    so values saved via the UI apply immediately.

    os.getenv() would be a stale snapshot from container start — writing the
    .env file does NOT update a running process's environment.
    """
    env = {}
    path = "/opt/barenoc/.env"
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    except Exception:
        pass
    # Fall back to process environment (env_file injection at container start)
    for key in ("UNIFI_URL", "UNIFI_USER", "UNIFI_PASSWORD", "UNIFI_API_KEY"):
        if key not in env and key in os.environ:
            env[key] = os.environ[key]
    return env


def _get_unifi_config() -> dict:
    """Read UniFi config from environment."""
    env = _read_unifi_env()
    return {
        "url": env.get("UNIFI_URL", "https://192.0.2.1:443"),
        "username": env.get("UNIFI_USER", "admin"),
        "password": env.get("UNIFI_PASSWORD", ""),
        "api_key": env.get("UNIFI_API_KEY", ""),
    }


@router.get("/config")
def get_config(user: User = Depends(require_role("admin"))):
    """Get UniFi config (redacted)."""
    cfg = _get_unifi_config()
    env = _read_unifi_env()
    return {
        "url": cfg["url"],
        "username": cfg["username"],
        "password_configured": bool(cfg["password"]),
        "api_key_configured": bool(cfg["api_key"]),
        "auth": "api_key" if cfg["api_key"] else ("password" if cfg["password"] else None),
        "autosync_enabled": _env_bool(env.get("UNIFI_AUTOSYNC_ENABLED", "")),
        "autosync_interval": _env_int(env.get("UNIFI_AUTOSYNC_INTERVAL_MIN", "5"), 5),
        # Auto-adopt: UniFi-managed network devices are claimed automatically
        # once the controller connection works (default ON).
        "auto_adopt": _env_bool(env.get("UNIFI_AUTO_ADOPT", "true") or "true"),
    }


def _env_bool(value: str) -> bool:
    """Parse a truthy env value ('1', 'true', 'yes', 'on'). Empty -> False."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _env_int(value: str, default: int) -> int:
    """Parse an int env value, falling back to default."""
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


@router.post("/config")
def set_config(config: dict, db: Session = Depends(get_db),
               user: User = Depends(require_role("admin"))):
    """Update UniFi config in .env (audit-logged)."""
    env_path = "/opt/barenoc/.env"
    allowed = {
        "url": "UNIFI_URL",
        "username": "UNIFI_USER",
        "password": "UNIFI_PASSWORD",
        "api_key": "UNIFI_API_KEY",
    }
    updates = {}
    for key, env_key in allowed.items():
        if key not in config:
            continue
        value = config[key]
        # empty or redacted secret = keep the stored one (same
        # convention as the LLM/email settings)
        if key in ("password", "api_key") and (not value or "••" in str(value)):
            continue
        updates[env_key] = str(value)

    # Auto-sync toggle + interval (typed + validated)
    if "autosync_enabled" in config:
        updates["UNIFI_AUTOSYNC_ENABLED"] = (
            "true" if str(config["autosync_enabled"]).strip().lower() in ("1", "true", "yes", "on") else "false"
        )
    if "autosync_interval" in config:
        try:
            iv = int(config["autosync_interval"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="autosync_interval must be minutes (number)")
        if iv not in (5, 10, 15, 30, 60):
            raise HTTPException(status_code=400, detail="autosync_interval must be 5, 10, 15, 30, or 60 minutes")
        updates["UNIFI_AUTOSYNC_INTERVAL_MIN"] = str(iv)

    # Auto-adopt toggle (typed)
    if "auto_adopt" in config:
        updates["UNIFI_AUTO_ADOPT"] = (
            "true" if str(config["auto_adopt"]).strip().lower() in ("1", "true", "yes", "on") else "false"
        )

    try:
        with open(env_path, "r") as f:
            lines = f.readlines()
        for env_key, value in updates.items():
            new_lines = []
            found = False
            for line in lines:
                if line.startswith(f"{env_key}="):
                    new_lines.append(f"{env_key}={value}\n")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"{env_key}={value}\n")
            lines = new_lines
        with open(env_path, "w") as f:
            f.writelines(lines)
        # Audit: field names + safe values (never the password / API key)
        safe = {k: v for k, v in updates.items()
                if k not in ("UNIFI_PASSWORD", "UNIFI_API_KEY")}
        log_event(db, "settings_change", user.username, {
            "section": "unifi",
            "fields": sorted(updates.keys()),
            "values": safe,
        })
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _unifi_client(cfg: dict) -> UniFiClient:
    """Build a client using the API key when present, else session login."""
    return UniFiClient(
        cfg["url"], cfg["username"], cfg["password"],
        api_key=cfg.get("api_key") or None,
    )


def _auth_ready(cfg: dict) -> str:
    """Return which auth method is configured, or None."""
    if cfg.get("api_key"):
        return "api_key"
    if cfg.get("password"):
        return "password"
    return None


@router.post("/test")
def test_connection(user: User = Depends(require_role("admin"))):
    """Test connection to the UniFi controller."""
    cfg = _get_unifi_config()
    auth = _auth_ready(cfg)
    if not auth:
        raise HTTPException(status_code=400, detail="UniFi authentication not configured (API key or password)")

    client = _unifi_client(cfg)
    ok = client.login()
    if ok:
        devices = client.get_devices()
        return {"connected": True, "devices_found": len(devices), "auth": auth}
    reason = client.last_error or "unknown"
    if reason.startswith("HTTP 401"):
        hint = " — wrong credentials, or no Local Access Account / API key on the controller"
    elif reason.startswith("HTTP"):
        hint = " — controller returned an error"
    else:
        hint = " — is the controller reachable from this host?"
    return {"connected": False, "error": f"Login failed ({reason}){hint}"}


@router.post("/ensure-wireless-uplinks")
def ensure_wireless_uplinks(body: dict = None,
                           user: User = Depends(require_any_role("admin", "agent"))):
    """Ensure every ENABLED wireless SSID VLAN is available on every AP uplink
    port (native or tagged), preserving other port settings/exclusions.
    Body: {"dry_run": true} previews without writing. Write action."""
    body = body or {}
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    result = client.ensure_wireless_uplinks(dry_run=bool(body.get("dry_run")))
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result.get("message"))
    return result


@router.get("/firmware-status")
def firmware_status(user: User = Depends(get_current_user)):
    """Current firmware versions of the controller + managed devices (read-only)."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    return client.firmware_status()


@router.post("/wlans/{ssid}/password")
def set_ssid_password(ssid: str, body: dict = None,
                     user: User = Depends(require_any_role("admin", "agent"))):
    """Change a Wi-Fi SSID's passphrase. Body: {"password": "..."} (8-63
    chars). Write action."""
    passphrase = str((body or {}).get("password", "")).strip()
    if not (8 <= len(passphrase) <= 63):
        raise HTTPException(status_code=400, detail="password must be 8-63 characters")
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    result = client.set_ssid_password(ssid, passphrase)
    if not result.get("applied"):
        raise HTTPException(status_code=502, detail=result.get("error", "update failed"))
    return result


@router.post("/networks")
def create_network(body: dict = None, db: Session = Depends(get_db),
                   user: User = Depends(require_any_role("admin", "agent"))):
    """Create a new corporate VLAN network on the controller.

    Body: {"name": "...", "vlan": 12, "subnet": "192.168.12.1/24"?, "dhcp": true?}
    subnet defaults to 192.168.<vlan>.1/24 (third-octet == VLAN convention).
    Write action — approval/autonomy is governed by the policy, not here.
    """
    body = body or {}
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        vlan = int(body.get("vlan"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="vlan must be an integer (1-4094)")
    if not 1 <= vlan <= 4094:
        raise HTTPException(status_code=400, detail="vlan must be 1-4094")
    subnet = body.get("subnet")
    if subnet is not None and not re.match(
            r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$", str(subnet)):
        raise HTTPException(status_code=400, detail="subnet must be a CIDR like 192.168.<vlan>.1/24")
    dhcp = bool(body.get("dhcp", True))
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    new_id = client.create_network(name, vlan, subnet=subnet, dhcp=dhcp)
    if not new_id:
        raise HTTPException(status_code=502, detail="controller rejected network creation (check the name/vlan are unique)")
    log_event(db, "unifi_network_create", user.username, {
        "name": name, "vlan": vlan, "subnet": subnet or f"192.168.{vlan}.1/24",
    })
    return {"created": new_id, "name": name, "vlan": vlan,
            "subnet": subnet or f"192.168.{vlan}.1/24"}


@router.get("/topology")
def topology(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Graph for the Devices page topology view — ONLY adopted (claimed +
    UniFi-managed) devices, with their device-to-device uplink links.
    Endpoint clients are not part of the topology (they live in the list)."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    devices = client.get_devices()
    # Adopted set = devices claimed in BareNOC inventory (onboarded)
    rows = db.query(Device).filter(
        Device.mac_address.isnot(None),
        Device.unifi_managed.is_(True),
        Device.claimed.is_(True),
    ).all()
    adopted_macs = {d.mac_address for d in rows}
    vendor_by_mac = {d.mac_address: d.vendor for d in rows if d.vendor}
    adopted = [d for d in devices if d.get("mac") in adopted_macs]
    for d in adopted:
        d["vendor"] = vendor_by_mac.get(d["mac"]) or "Ubiquiti"
    adopted_set = {d["mac"] for d in adopted}
    links = []
    for d in adopted:
        up = d.get("uplink_mac") or ""
        if up in adopted_set:
            links.append({"from": up, "to": d["mac"],
                          "port": d.get("uplink_remote_port")})
    return {"devices": adopted, "clients": [], "links": links}


@router.get("/networks")
def list_networks(user: User = Depends(get_current_user)):
    """List VLANs/subnets + SSIDs from the UniFi controller (read-only,
    powers the chat 'list my vlans' questions and future network views)."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    return {"networks": client.get_networks(), "wlans": client.get_wlans()}


@router.get("/clients")
def list_clients(online: Optional[bool] = Query(None),
                 wired: Optional[bool] = Query(None),
                 user: User = Depends(get_current_user)):
    """Known + active clients (read-only). Optional filters (combinable):
      online: true | false     (online now)
      wired:  true | false     (wired vs wireless)
    e.g. ?online=true -> online clients only."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    clients = client.get_clients()
    if online is not None:
        clients = [c for c in clients if bool(c.get("online")) == online]
    if wired is not None:
        clients = [c for c in clients if bool(c.get("wired")) == wired]
    online_n = sum(1 for c in clients if c.get("online"))
    return {"clients": clients, "total": len(clients), "online": online_n}


@router.get("/devices")
def list_devices(device_type: Optional[str] = Query(None),
                 status: Optional[str] = Query(None),
                 user: User = Depends(get_current_user)):
    """Managed UniFi network devices (gateways/switches/APs) with health fields
    (read-only). Optional filters (combinable):
      device_type: gateway | switch | ap
      status:      online | offline
    e.g. ?device_type=ap&status=online -> online APs only."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    if device_type not in (None, "gateway", "switch", "ap"):
        raise HTTPException(status_code=400, detail="device_type must be gateway, switch, or ap")
    if status not in (None, "online", "offline"):
        raise HTTPException(status_code=400, detail="status must be online or offline")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    devices = client.get_devices()
    if device_type:
        devices = [d for d in devices if d.get("type") == device_type]
    if status:
        devices = [d for d in devices if d.get("status") == status]
    online = sum(1 for d in devices if d.get("status") == "online")
    return {"devices": devices, "total": len(devices), "online": online}


@router.get("/firewall-rules")
def list_firewall_rules(user: User = Depends(get_current_user)):
    """Custom firewall rules from the controller (read-only) — powers
    'what is blocking X' / 'show my firewall rules' questions."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    return {"rules": client.get_custom_firewall_rules()}


@router.post("/devices/{mac}/restart")
def restart_unifi_device(mac: str, user: User = Depends(require_role("operator"))):
    """Reboot a UniFi-managed device via the controller (soft restart).
    Write action — the Autonomy Policy decides auto-run vs approval."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    if not client.restart_device(mac):
        raise HTTPException(status_code=502, detail=f"Controller rejected restart for {mac}")
    return {"status": "ok", "restarted": mac}


@router.get("/client/{ip}/port")
def client_port(ip: str, user: User = Depends(require_role("operator"))):
    """Which switch port a wired client is connected to (read-only)."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    info = client.find_client_port(ip)
    if not info:
        raise HTTPException(status_code=404, detail=f"No wired port found for {ip} (offline or wireless?)")
    nets = client.get_networks_map()
    info["network_name"] = nets.get(info.get("network_id", ""), {}).get("name", "")
    return info


@router.get("/ports/{switch_mac}")
def switch_ports(switch_mac: str, user: User = Depends(require_role("operator"))):
    """Port table (native/tagged VLANs) for one switch (read-only)."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    ports = client.get_switch_ports(switch_mac)
    nets = client.get_networks_map()
    for pt in ports:
        pt["native_name"] = nets.get(pt["native_network_id"], {}).get("name", "")
        pt["tagged_names"] = [nets.get(i, {}).get("name", i) for i in pt["tagged_network_ids"]]
    return {"switch_mac": switch_mac, "ports": ports}


def _resolve_networks(client, tagged: list, native: str = None) -> tuple:
    """Resolve network names/IDs to network IDs. Returns (native_id, tagged_ids, error)."""
    nets = client.get_networks_map()
    by_name = {v["name"].lower(): k for k, v in nets.items()}
    by_vlan = {str(v["vlan"]): k for k, v in nets.items() if v.get("vlan")}
    tagged_ids = []
    for t in tagged or []:
        t = str(t).strip()
        nid = t if t in nets else by_name.get(t.lower()) or by_vlan.get(t)
        if not nid:
            # tolerate "vlan-4" / "vlan 4" / "vlan4" forms
            import re as _re
            mm = _re.search(r"vlan[\s-]?(\d+)", t, _re.I)
            if mm:
                nid = by_vlan.get(mm.group(1))
        if not nid:
            # tolerate "Name (5)" — a user/AI writing the VLAN in parentheses
            import re as _re
            mm = _re.search(r"^(.*?)\s*\(\s*(\d+)\s*\)$", t)
            if mm and mm.group(1).strip():
                nid = (by_name.get(mm.group(1).strip().lower())
                       or by_vlan.get(mm.group(2)))
        if not nid:
            return None, None, f"Unknown network '{t}' — known: {', '.join(v['name'] for v in nets.values())}"
        tagged_ids.append(nid)
    native_id = None
    if native:
        n = str(native).strip()
        native_id = n if n in nets else by_name.get(n.lower()) or by_vlan.get(n)
        if not native_id:
            import re as _re
            mm = _re.search(r"vlan[\s-]?(\d+)", n, _re.I)
            if mm:
                native_id = by_vlan.get(mm.group(1))
        if not native_id:
            return None, None, f"Unknown native network '{native}'"
    return native_id, tagged_ids, None


@router.post("/ports/{switch_mac}/{port_idx}/bounce")
def bounce_port(switch_mac: str, port_idx: int,
               user: User = Depends(require_any_role("admin", "agent"))):
    """Cycle a switch port (disable -> 2s -> enable). Write action."""
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    result = client.bounce_port(switch_mac, port_idx)
    if not result.get("applied"):
        raise HTTPException(status_code=502, detail=result.get("error", "bounce failed"))
    return result


@router.post("/ports/{switch_mac}/{port_idx}/rename")
def rename_port(switch_mac: str, port_idx: int, body: dict,
                user: User = Depends(require_any_role("admin", "agent"))):
    """Rename a switch port. Body: {"name": "..."}. Write action."""
    name = str((body or {}).get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if len(name) > 32:
        raise HTTPException(status_code=400, detail="name too long (max 32 chars)")
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    result = client.set_port_name(switch_mac, port_idx, name)
    if not result.get("applied"):
        raise HTTPException(status_code=502, detail=result.get("error", "rename failed"))
    return result


@router.post("/ports/{switch_mac}/{port_idx}/vlans")
def set_port_vlans(switch_mac: str, port_idx: int, body: dict,
                   user: User = Depends(require_any_role("admin", "agent"))):
    """Apply native/tagged VLAN networks to a switch port (admin).

    body: {"tagged": ["Storage", "vlan-4", <id>...], "native": "Production"|null,
           "dry_run": true|false}
    Use dry_run=true to preview the resulting port config without writing.
    """
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    client = _unifi_client(cfg)
    if not client.login():
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")

    native_id, tagged_ids, err = _resolve_networks(client, body.get("tagged") or [], body.get("native"))
    if err:
        raise HTTPException(status_code=400, detail=err)

    ports = client.get_switch_ports(switch_mac)
    port = next((p for p in ports if p["port_idx"] == port_idx), None)
    if not port:
        raise HTTPException(status_code=404, detail=f"Port {port_idx} not found on {switch_mac}")
    nets = client.get_networks_map()

    preview = {
        "switch_mac": switch_mac,
        "port_idx": port_idx,
        "port_name": port["name"],
        "before": {"native": nets.get(port["native_network_id"], {}).get("name", ""),
                    "tagged": [nets.get(i, {}).get("name", i) for i in port["tagged_network_ids"]]},
        "after": {"native": nets.get(native_id, {}).get("name", "") if native_id else "(unchanged)",
                   "tagged": [nets.get(i, {}).get("name", i) for i in tagged_ids]},
    }
    if body.get("dry_run"):
        preview["dry_run"] = True
        return preview

    result = client.set_port_vlans(switch_mac, port_idx, tagged_ids,
                                   native_network_id=native_id or port["native_network_id"])
    if not result.get("applied"):
        raise HTTPException(status_code=502, detail=result.get("error", "apply failed"))
    preview["applied"] = True
    preview["profile_id"] = result.get("profile_id")
    return preview


@router.post("/sync")
def sync_from_unifi(db: Session = Depends(get_db), user: User = Depends(require_any_role("admin", "agent"))):
    """Pull devices from UniFi and upsert into inventory."""
    cfg = _get_unifi_config()
    auth = _auth_ready(cfg)
    if not auth:
        raise HTTPException(status_code=400, detail="UniFi authentication not configured (API key or password)")

    client = _unifi_client(cfg)
    if not client.login():
        reason = client.last_error or "unknown"
        if reason.startswith("HTTP 401"):
            hint = " (wrong credentials, or no Local Access Account / API key on the controller)"
        elif reason.startswith("HTTP"):
            hint = f" (controller returned {reason})"
        else:
            hint = f" (connection error: {reason})"
        raise HTTPException(status_code=502, detail=f"Could not log in to UniFi controller{hint}")

    unifi_devices = client.get_devices()
    added = 0
    updated = 0
    skipped = 0
    adopted = 0

    # Auto-adopt: UniFi-managed network gear is claimed automatically once the
    # controller connection works (UNIFI_AUTO_ADOPT, default true). Passive
    # clients are NOT adopted — they stay unclaimed inventory.
    auto_adopt = _env_bool(env_auto := _read_unifi_env().get("UNIFI_AUTO_ADOPT") or "true")

    for ud in unifi_devices:
        # Match existing device by MAC or IP
        existing = None
        if ud["mac"]:
            existing = db.query(Device).filter(Device.mac_address == ud["mac"]).first()
        if not existing and ud["ip"]:
            existing = db.query(Device).filter(Device.ip_address == ud["ip"]).first()

        if existing:
            # Update status and metadata from UniFi
            existing.status = "online" if ud["status"] == "online" else "unreachable"
            existing.unifi_managed = True
            existing.device_type = existing.device_type if existing.device_type != "unknown" else ud["type"]
            existing.vendor = existing.vendor or "Ubiquiti"
            existing.model = existing.model or ud["model"]
            existing.last_seen = datetime.utcnow()
            if auto_adopt and not existing.claimed:
                # Adopt previously-discovered-but-unclaimed gear
                existing.claimed = True
                existing.device_group = existing.device_group or "default"
                if existing.name and existing.name.startswith("unifi-"):
                    existing.name = ud["name"] or existing.name
                adopted += 1
            updated += 1
        else:
            # New device — auto-adopted when the controller connection is
            # established, else left unclaimed for review
            device = Device(
                name=(ud["name"] or f"unifi-{ud['mac']}") if auto_adopt else (f"unifi-{ud['name']}" if ud["name"] else f"unifi-{ud['mac']}"),
                ip_address=ud["ip"] or ud["mac"],
                mac_address=ud["mac"],
                device_type=ud["type"],
                vendor="Ubiquiti",
                model=ud["model"],
                status="online" if ud["status"] == "online" else "unreachable",
                claimed=auto_adopt,
                unifi_managed=True,
                device_group="default",
                last_seen=datetime.utcnow(),
            )
            db.add(device)
            if auto_adopt:
                adopted += 1
            added += 1

    # ── endpoints (clients) ──────────────────────────────────────
    # Full client DB merged with active sessions (see get_clients).
    clients = client.get_clients()
    c_added = c_updated = c_skipped = 0

    for uc in clients:
        # Only invent devices for clients we can actually reach/monitor
        if not uc["ip"]:
            c_skipped += 1
            continue
        # Skip noisy anonymous clients — but keep ONLINE anonymous ones
        # (randomized MACs are real live devices worth identifying, e.g. a Pi)
        if not uc["hostname"] and (not uc["vendor"] or uc["vendor"] == "?") and not uc["online"]:
            c_skipped += 1
            continue

        existing = None
        if uc["mac"]:
            existing = db.query(Device).filter(Device.mac_address == uc["mac"]).first()
        if not existing and uc["ip"]:
            existing = db.query(Device).filter(Device.ip_address == uc["ip"]).first()

        if existing and not existing.mac_address and not existing.claimed and uc["mac"]:
            # Merge a ping-scan find (no MAC, generic 'discovered-*' name) with
            # the UniFi client identity so the duplicate stops lingering.
            existing.mac_address = uc["mac"]
            if existing.name and existing.name.startswith("discovered-"):
                existing.name = uc["name"] or existing.name
            if (existing.hostname or "").startswith("discovered-"):
                existing.hostname = uc["hostname"] or None
            if "unifi-client" not in (existing.tags or []):
                existing.tags = list(existing.tags or []) + [
                    "unifi-client", "wired" if uc.get("wired") else "wireless"]

        if existing:
            # Refresh reachability only — never clobber user configuration
            existing.status = "online" if uc["online"] else "offline"
            existing.hostname = existing.hostname or (uc["hostname"] or None)
            existing.vendor = existing.vendor or (uc["vendor"] or None)
            existing.last_seen = datetime.utcnow()
            # Ensure scan-found records merged earlier are visible as clients
            if "unifi-client" not in (existing.tags or []):
                existing.tags = list(existing.tags or []) + [
                    "unifi-client", "wired" if uc.get("wired") else "wireless"]
            c_updated += 1
            continue

        device_type = _guess_client_type(uc)
        device = Device(
            name=uc["name"],
            ip_address=uc["ip"],
            mac_address=uc["mac"] or None,
            device_type=device_type,
            vendor=uc["vendor"] or None,
            hostname=uc["hostname"] or None,
            status="online" if uc["online"] else "offline",
            claimed=False,
            unifi_managed=False,
            tags=["unifi-client", "wired" if uc["wired"] else "wireless"],
            last_seen=datetime.utcnow(),
        )
        db.add(device)
        c_added += 1

    db.commit()
    return {
        "status": "ok",
        "unifi_devices": len(unifi_devices),
        "added": added,
        "updated": updated,
        "adopted": adopted,
        "skipped": skipped,
        "unifi_clients": len(clients),
        "clients_added": c_added,
        "clients_updated": c_updated,
        "clients_skipped": c_skipped,
    }


def _guess_client_type(uc: dict) -> str:
    """Guess a BareNOC device_type from UniFi client hints."""
    blob = f"{uc.get('hostname', '')} {uc.get('vendor', '')}".lower()
    if any(k in blob for k in ("proxmox", "plex", "omv", "openmediavault", "nas", "server", "rocky", "ubuntu", "debian", "home assistant", "hass")):
        return "server"
    if any(k in blob for k in ("apple", "iphone", "galaxy", "pixel", "z-flip", "samsung", "chromecast", "roku", "tv")):
        return "workstation"
    if "printer" in blob or "hp " in blob or "brother" in blob:
        return "printer"
    return "workstation"
