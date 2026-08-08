#!/usr/bin/env python3
"""BareNOC Scheduler — periodic tasks: SNMP polling, device health checks, digests."""

import os
import sys
import json
import time
import logging
import datetime
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("barenoc-scheduler")

API_BASE = "http://api:8000/api/v1"
POLL_INTERVAL = 300  # 5 minutes
HEALTH_INTERVAL = 60  # 1 minute for connectivity checks
ENV_PATH = "/opt/barenoc/.env"
AUTOSYNC_INTERVALS = (5, 10, 15, 30, 60)  # allowed minutes


def _read_env() -> dict:
    """Read .env fresh each cycle (hot-reload): the scheduler container mounts
    /opt/barenoc/.env so toggle/interval changes apply without a restart."""
    env = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    except Exception:
        pass
    return env


def _get_token() -> str:
    """Get API token for the agent service account (credentials file is
    provisioned by scripts/setup_agent_credentials.sh and mounted read-only;
    never hardcode API credentials in code)."""
    creds = {}
    try:
        with open("/opt/barenoc/agent/credentials") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    creds[k.strip()] = v.strip()
    except Exception as e:
        logger.error(f"Cannot read agent credentials: {e}")
        return ""
    data = json.dumps({"username": creds.get("username", ""),
                       "password": creds.get("password", "")}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/auth/login",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read().decode()).get("access_token", "")


def _api_get(path: str, token: str) -> dict:
    """Make authenticated GET request to API."""
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read().decode())


def _api_patch(path: str, data: dict, token: str):
    """Make authenticated PATCH request to API."""
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="PATCH",
    )
    urllib.request.urlopen(req, timeout=10)


def _api_post(path: str, data: dict, token: str):
    """Make authenticated POST request to API (accepts non-200 gracefully)."""
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


_VERIFY_COOLDOWN = {}   # dev_id -> last queued timestamp (stop the flood)
VERIFY_COOLDOWN_SECONDS = 3600   # one verify attempt per device per hour


def check_device_health(token: str):
    """Ping all managed devices and update their status."""
    try:
        devices = _api_get("/devices?limit=500", token)
        now = time.time()
        for device in devices.get("devices", []):
            dev_id = device["id"]
            ip = device["ip_address"]
            name = device["name"]

            # Skip if recently verified or already pending verification
            if device.get("status") in ("online", "pending"):
                continue
            # Cooldown: don't re-queue a verify for the same device every cycle —
            # that floods the 2-slot agent and starves real work (network_info,
            # fingerprint, UniFi port jobs).
            if now - _VERIFY_COOLDOWN.get(dev_id, 0) < VERIFY_COOLDOWN_SECONDS:
                continue
            _VERIFY_COOLDOWN[dev_id] = now

            # Queue a verify job by calling the verify endpoint
            req = urllib.request.Request(
                f"{API_BASE}/devices/{dev_id}/verify",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                logger.info(f"Queued health check for {name} ({ip})")
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Health check failed: {e}")


def snmp_poll_devices(token: str):
    """Poll devices with SNMP configured."""
    try:
        devices = _api_get("/devices?limit=500", token)
        for device in devices.get("devices", []):
            if not device.get("snmp_configured"):
                continue

            dev_id = device["id"]
            ip = device["ip_address"]
            name = device["name"]

            # Write a job file for the agent to do SNMP poll
            job = {
                "ticket_id": f"snmppoll-{dev_id}-{int(time.time())}",
                "action": "snmp_poll",
                "target": ip,
                "params": {"community": "public"},
                "reason": f"Scheduled SNMP poll for {name}",
                "confidence": 1.0,
                "created_at": datetime.datetime.utcnow().isoformat(),
                "source": "scheduler",
            }
            job_path = f"/opt/barenoc/jobs/incoming/snmppoll-{dev_id}.json"
            with open(job_path, "w") as f:
                json.dump(job, f, indent=2)
            logger.info(f"Queued SNMP poll for {name} ({ip})")

    except Exception as e:
        logger.error(f"SNMP poll failed: {e}")


def _sync_unifi(token: str):
    """Trigger UniFi sync via API (skips silently if not configured)."""
    try:
        data = json.dumps({}).encode()
        req = urllib.request.Request(
            f"{API_BASE}/unifi/sync", data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=20)
        result = json.loads(resp.read().decode())
        logger.info(f"UniFi sync: {result.get('added', 0)} added, {result.get('updated', 0)} updated")
    except Exception:
        # Not configured or unreachable — silent
        pass


def _unifi_autosync_config() -> tuple:
    """Read auto-sync settings from .env. Returns (enabled: bool, interval_minutes: int)."""
    env = _read_env()
    enabled = str(env.get("UNIFI_AUTOSYNC_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")
    try:
        interval = int(env.get("UNIFI_AUTOSYNC_INTERVAL_MIN", "5") or 5)
    except ValueError:
        interval = 5
    if interval not in AUTOSYNC_INTERVALS:
        interval = 5
    return enabled, interval


def check_update_schedule(token: str, last_triggered: dict):
    """Settings → Updates schedule: at the configured day/hour, queue the
    update (POST /updates/now — the host service applies it). Runs at most
    once per calendar day."""
    try:
        sched = _api_get("/updates/schedule", token)
    except Exception as e:
        logger.debug(f"update schedule read failed: {e}")
        return
    if not sched.get("enabled"):
        return
    import datetime as _dt
    now = _dt.datetime.utcnow()
    day = str(sched.get("day", "daily"))
    if day != "daily":
        # config 0=Sunday..6=Saturday; python weekday() 0=Monday..6=Sunday
        sun0 = (now.weekday() + 1) % 7
        if sun0 != int(day):
            return
    if int(sched.get("hour", 2)) != now.hour:
        return
    key = f"{day}-{now.date().isoformat()}"
    if last_triggered.get("update") == key:
        return
    try:
        _api_post("/updates/now", {}, token)
        last_triggered["update"] = key
        logger.info(f"Scheduled update queued ({key})")
    except Exception as e:
        logger.warning(f"Scheduled update not queued: {e}")


def run():
    """Main scheduler loop."""
    logger.info("BareNOC Scheduler starting...")
    os.makedirs("/opt/barenoc/jobs/incoming", exist_ok=True)

    last_health = 0
    last_snmp = 0
    last_unifi = 0
    last_upd_sched = 0
    _last_triggered = {}

    while True:
        try:
            token = _get_token()
            now = time.time()

            # Health checks every 60s
            if now - last_health >= HEALTH_INTERVAL:
                logger.info("Running device health checks...")
                check_device_health(token)
                last_health = now

            # SNMP polls every 300s
            if now - last_snmp >= POLL_INTERVAL:
                logger.info("Running SNMP polls...")
                snmp_poll_devices(token)
                last_snmp = now

            # UniFi auto-sync — config-driven (Settings → UniFi), hot-reloaded
            # from .env each cycle; disabled by default until toggled on.
            enabled, interval_min = _unifi_autosync_config()
            if enabled and now - last_unifi >= interval_min * 60:
                logger.info(f"Running UniFi auto-sync (every {interval_min}m)...")
                _sync_unifi(token)
                last_unifi = now

            # Scheduled update (Settings → Updates) — checked every 60s.
            if now - last_upd_sched >= 60:
                check_update_schedule(token, _last_triggered)
                last_upd_sched = now

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        time.sleep(15)


if __name__ == "__main__":
    run()
