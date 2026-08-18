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
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("barenoc-scheduler")

API_BASE = "http://api:8000/api/v1"
POLL_INTERVAL = 300  # 5 minutes
HEALTH_INTERVAL = 60  # 1 minute for connectivity checks
ENV_PATH = "/opt/barenoc/.env"
CREDS_PATH = "/opt/barenoc/agent/credentials"
AUTOSYNC_INTERVALS = (5, 10, 15, 30, 60)  # allowed minutes
# Startup guard: wait this long (s) for the api to be healthy AND the agent
# credentials to log in before the main loop starts (the 08-14/08-18 family).
STARTUP_READY_TIMEOUT = 600


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


def _creds_file_ready() -> bool:
    """True when the agent credentials file exists as a REGULAR FILE. (A stale
    directory at this path — an old install bug — would break the scheduler's
    bind mount forever, so we only proceed on a real file.)"""
    return os.path.isfile(CREDS_PATH)


def _api_health_ok() -> bool:
    """Unauthenticated api health probe; True only on HTTP 200."""
    try:
        req = urllib.request.Request(f"{API_BASE}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _wait_for_ready(max_wait_seconds: int = STARTUP_READY_TIMEOUT) -> str:
    """Startup guard — block until the api is healthy AND the agent credentials
    file exists + logs in (200). Retries with VISIBLE logging instead of
    letting the main loop 401-flood every cycle (the 08-14/08-18 pattern).

    Returns a fresh token when ready, "" when it gave up (the loop then keeps
    logging loudly — it never goes silent)."""
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        if not _api_health_ok():
            logger.info(f"Waiting for api to become healthy (attempt {attempt})…")
            time.sleep(5)
            continue
        if not _creds_file_ready():
            logger.warning(
                f"api healthy but agent credentials file missing (attempt {attempt}) — "
                f"waiting; run scripts/setup_agent_credentials.sh if this persists")
            time.sleep(5)
            continue
        token = _get_token()
        if token:
            logger.info(f"api healthy + agent credentials verified (attempt {attempt})")
            return token
        logger.warning(
            f"api healthy but agent login failed (attempt {attempt}) — credentials file "
            f"and DB may be out of sync; run scripts/setup_agent_credentials.sh")
        time.sleep(5)
    logger.error(
        "Timed out waiting for api health + agent credentials — entering the loop "
        "anyway (subsequent errors will be logged loudly)")
    return ""


def _get_token() -> str:
    """Get API token for the agent service account (credentials file is
    provisioned by scripts/setup_agent_credentials.sh and mounted read-only;
    never hardcode API credentials in code)."""
    creds = {}
    try:
        with open(CREDS_PATH) as f:
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
    """Make authenticated POST request to API (raises urllib HTTPError on a
    non-200 response — callers decide whether that's fatal)."""
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


def _appliance_tz() -> str:
    """The appliance's LOCAL timezone (TZ from .env) — the schedule runs in
    wall-clock THIS timezone even though the container clock is UTC. Falls back
    to UTC when TZ is unset or invalid."""
    tz = (_read_env().get("TZ") or os.environ.get("TZ") or "").strip()
    return tz or "UTC"


def _zone(tz_name: str = None):
    try:
        return ZoneInfo(tz_name or _appliance_tz())
    except Exception:
        return ZoneInfo("UTC")


def _local_now(tz_name: str = None) -> datetime.datetime:
    """NAIVE wall-clock now in the appliance TZ (safe for hour/day compares)."""
    return datetime.datetime.now(_zone(tz_name)).replace(tzinfo=None)


def _local_to_utc(dt: datetime.datetime, tz_name: str = None) -> datetime.datetime:
    """Convert a NAIVE local wall-clock datetime to an AWARE UTC datetime.
    DST-safe: zoneinfo attaches the correct offset for that local instant."""
    return dt.replace(tzinfo=_zone(tz_name)).astimezone(datetime.timezone.utc)


def _parse_local_dt(s: str) -> datetime.datetime:
    """Parse a local wall-clock datetime ('YYYY-MM-DDTHH:MM[:SS]' or
    'YYYY-MM-DD HH:MM[:SS]'). Returns a NAIVE datetime; raises on bad input."""
    s = (s or "").strip()
    if not s:
        raise ValueError("empty datetime")
    norm = s.replace(" ", "T")
    if len(norm) == 16:  # datetime-local input: YYYY-MM-DDTHH:MM
        dt = datetime.datetime.strptime(norm, "%Y-%m-%dT%H:%M")
    else:
        dt = datetime.datetime.fromisoformat(norm)
    return dt.replace(tzinfo=None)


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


def _queue_update(token: str, key: str, last_triggered: dict,
                  complete: bool, log_label: str):
    """POST /updates/now once; only mark the guard when a release was actually
    available (the endpoint 400s otherwise — the 'only fire when an update is
    available' guarantee). For one-time schedules also mark the schedule fired
    + disabled so it never re-fires."""
    try:
        _api_post("/updates/now", {}, token)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            logger.info(f"{log_label}: due but no release available — staying armed")
            return
        logger.warning(f"{log_label} not queued: {e}")
        return
    except Exception as e:
        logger.warning(f"{log_label} not queued: {e}")
        return
    last_triggered["update"] = key
    logger.info(log_label)
    if complete:
        try:
            _api_post("/updates/schedule/complete", {}, token)
        except Exception as e:
            logger.warning(f"update schedule complete failed: {e}")


def check_update_schedule(token: str, last_triggered: dict):
    """Settings → Updates schedule.

    Two modes:
      - recurring: at the configured LOCAL day/hour, queue the update
        (POST /updates/now — the host service applies it). At most once per
        calendar day.
      - onetime: at (or after) the configured LOCAL datetime, queue the update
        once; then mark it fired + disable (persisted server-side). If no
        release is available yet it stays armed until one is.

    The hour/day/when are LOCAL wall-clock (TZ from .env); this container runs
    UTC so the one-time comparison converts local → UTC (DST-safe via zoneinfo).
    """
    try:
        sched = _api_get("/updates/schedule", token)
    except Exception as e:
        logger.debug(f"update schedule read failed: {e}")
        return
    if not sched.get("enabled"):
        return

    mode = str(sched.get("mode") or "recurring").lower()
    tz = _appliance_tz()

    if mode == "onetime":
        when = str(sched.get("when") or "").strip()
        if not when:
            return
        if sched.get("fired"):
            return
        try:
            when_utc = _local_to_utc(_parse_local_dt(when), tz)
        except Exception as e:
            logger.warning(f"update schedule: bad one-time when '{when}': {e}")
            return
        if datetime.datetime.now(datetime.timezone.utc) < when_utc:
            return  # not yet due
        key = f"onetime-{when}"
        if last_triggered.get("update") == key:
            return
        _queue_update(token, key, last_triggered, complete=True,
                      log_label=f"One-time update queued ({when} local)")
        return

    # recurring — also the backward-compatible default for a mode-less conf.
    now = _local_now(tz)
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
    _queue_update(token, key, last_triggered, complete=False,
                  log_label=f"Scheduled update queued ({key})")


def _queue_netopt(token: str, key: str, last_triggered: dict, complete: bool):
    """POST /netopt/runs (scheduled scan). One-time schedules are marked fired
    + disabled after a successful queue so they never re-fire."""
    try:
        _api_post("/netopt/runs", {"triggered_by": "schedule"}, token)
    except Exception as e:
        logger.warning(f"netopt schedule not queued: {e}")
        return
    last_triggered["netopt"] = key
    logger.info(f"Scheduled Network Optimization scan queued ({key})")
    if complete:
        try:
            _api_post("/netopt/schedule/complete", {}, token)
        except Exception as e:
            logger.warning(f"netopt schedule complete failed: {e}")


def check_netopt_schedule(token: str, last_triggered: dict):
    """Network Optimization schedule (recurring day/hour or one-time local
    datetime — same local-time pattern as updates-schedule-v2). The scan is
    READ-ONLY; the scheduler just triggers it like a manual run."""
    try:
        sched = _api_get("/netopt/schedule", token)
    except Exception as e:
        logger.debug(f"netopt schedule read failed: {e}")
        return
    if not sched.get("enabled"):
        return

    mode = str(sched.get("mode") or "recurring").lower()
    tz = _appliance_tz()

    if mode == "onetime":
        when = str(sched.get("when") or "").strip()
        if not when or sched.get("fired"):
            return
        try:
            when_utc = _local_to_utc(_parse_local_dt(when), tz)
        except Exception as e:
            logger.warning(f"netopt schedule: bad one-time when '{when}': {e}")
            return
        if datetime.datetime.now(datetime.timezone.utc) < when_utc:
            return
        key = f"netopt-onetime-{when}"
        if last_triggered.get("netopt") == key:
            return
        _queue_netopt(token, key, last_triggered, complete=True)
        return

    # recurring
    now = _local_now(tz)
    day = str(sched.get("day", "0"))
    if day != "daily":
        sun0 = (now.weekday() + 1) % 7
        if sun0 != int(day):
            return
    if int(sched.get("hour", 3)) != now.hour:
        return
    key = f"netopt-{day}-{now.date().isoformat()}"
    if last_triggered.get("netopt") == key:
        return
    _queue_netopt(token, key, last_triggered, complete=False)


def check_update_progress(token: str, last_notified: dict):
    """Watch the host self-update service's progress file (exposed via
    /updates/status). When it reaches a terminal stage (done/failed), email
    the alert channel ONCE per transition. Persists the last-notified key so a
    scheduler restart doesn't re-notify."""
    marker = "/opt/barenoc/jobs/update_notified.key"
    try:
        st = _api_get("/updates/status", token)
    except Exception as e:
        logger.debug(f"update progress read failed: {e}")
        return
    prog = st.get("progress") or {}
    stage = prog.get("stage") or ""
    if stage not in ("done", "failed"):
        return
    key = f"{stage}:{prog.get('at', '')}"
    if last_notified.get("update") == key:
        return
    try:
        with open(marker) as f:
            if f.read().strip() == key:
                last_notified["update"] = key
                return
    except Exception:
        pass
    try:
        _api_post("/updates/notify", {
            "stage": stage,
            "message": prog.get("message", ""),
            "version": st.get("current", ""),
        }, token)
        last_notified["update"] = key
        try:
            with open(marker, "w") as f:
                f.write(key)
        except Exception:
            pass
        logger.info(f"Update notification sent ({key})")
    except Exception as e:
        logger.warning(f"Update notification failed: {e}")


def run():
    """Main scheduler loop."""
    logger.info("BareNOC Scheduler starting...")
    os.makedirs("/opt/barenoc/jobs/incoming", exist_ok=True)

    # Health-order guard: on a fresh install the scheduler must not start its
    # poll loop (and 401-flood) before the api is healthy and the agent
    # credentials exist. Wait loudly, then proceed.
    _wait_for_ready()

    last_health = 0
    last_snmp = 0
    last_unifi = 0
    last_upd_sched = 0
    last_upd_prog = 0
    last_netopt = 0
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

            # Update progress notifications (done/failed) — every 60s.
            if now - last_upd_prog >= 60:
                check_update_progress(token, _last_triggered)
                last_upd_prog = now

            # Network Optimization schedule — checked every 60s.
            if now - last_netopt >= 60:
                check_netopt_schedule(token, _last_triggered)
                last_netopt = now

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        time.sleep(15)


if __name__ == "__main__":
    run()
