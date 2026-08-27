"""Link-stability monitor — graduated flap/outage tickets per monitored link.

Core idea: "this link should stay plugged in; any state change is suspicious."
Each monitored (device_id, interface) runs a small state machine:

  stable --first down/up transition--> P2 ticket ("Link flap: <name> <iface>"),
                                       30-min episode window
  >=3 flaps (down->up) in the window --> SAME ticket P1 ("Recurring link flap…")
  down > 10 min (persistent)         --> SAME ticket P1 ("Link outage: …")
  no further events for 30 min        --> auto-close with a summary note

Data channels (merged each ~60s cycle):
  * UniFi controller — gateway WAN (stat/health) + per-port up/media/name for
    the gateway + UniFi-managed switches (stat/device port_table). ONE
    long-lived controller session (login once — the controller 429s after
    ~10 rapid logins); port tables change rarely and are cached, refreshed at
    most every 60s.
  * SNMP ifOperStatus — for devices with snmp_configured (servers etc.).
  * Device.status — fallback for opted-in devices without UniFi/SNMP data.

Opt-in: the gateway WAN is ALWAYS monitored; every other link is watched when
its device has notify_state_changes=True (the existing toggle now also means
"watch this device's link stability").

Persistence: link_episodes table (models.LinkEpisode) so a container restart
resumes an in-flight episode. The TICKET is the record; email is best-effort.

This module is imported by alerting.py (one-way dependency) and must NOT
import alerting back.
"""

import datetime
import logging
import os
import re
import subprocess
import time

from database import SessionLocal
from models import Device, Ticket, LinkEpisode
from schemas import generate_ticket_id
from emailer import send_email, get_recipients, alert_html
from audit import log_event
from worknotes import add_note

logger = logging.getLogger("barenoc-link-monitor")

UP = "up"
DOWN = "down"
WAN = "wan"
STATUS_IFACE = "status"   # the Device.status fallback channel's pseudo-interface

FLAP_MIN_INTERVAL = 1800  # 30 min: min gap between emails for one link

_IFDESCR_OID = "1.3.6.1.2.1.2.2.1.2"
_IFOPERSTATUS_OID = "1.3.6.1.2.1.2.2.1.8"
_LOOPBACK_NAMES = {"lo", "lo0", "lo1", "lo2", "loopback"}
# Virtual/container interfaces that flap with container lifecycles — never
# treat them as monitored physical links (a docker0 up/down must not page).
_VIRTUAL_IFACE_PREFIXES = ("docker", "veth", "virbr", "br-", "tun", "tap",
                           "vmnet", "vnet")
_GATEWAY_TYPES = {"ugw", "ucg", "udm", "usg", "ucxg", "uxg"}
_SWITCH_TYPES = {"usw", "us", "us-l2", "us-l2-poe", "us-24", "us-8"}


# ── config (hot-read from .env, os.environ fallback) ────────────────────────

def link_monitor_config() -> dict:
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

    def s(key, default):
        return (env.get(key) or os.getenv(key, default) or default).strip()

    def b(key, default):
        return s(key, default).lower() in ("1", "true", "yes", "on")

    def i(key, default):
        try:
            return int(env.get(key) or os.getenv(key, str(default)) or default)
        except ValueError:
            return default

    return {
        "enabled": b("LINK_MONITOR_ENABLED", "true"),
        "flap_window_min": max(1, i("LINK_FLAP_WINDOW_MIN", 30)),
        "escalate_count": max(2, i("LINK_FLAP_ESCALATE_COUNT", 3)),
        "persist_down_min": max(1, i("LINK_PERSIST_DOWN_MIN", 10)),
        "stable_close_min": max(1, i("LINK_STABLE_CLOSE_MIN", 30)),
        "unifi_cache_seconds": max(30, i("LINK_UNIFI_CACHE_S", 60)),
    }


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


# ── channels ────────────────────────────────────────────────────────────────
# Each channel returns {(device_id, interface): "up"|"down"}, or None when the
# source is unavailable this cycle (the monitor keeps the previous view).

class UniFiChannel:
    """Gateway WAN (stat/health) + per-port up/media/name (stat/device
    port_table) for the gateway and UniFi-managed switches. One long-lived
    controller session; port tables cached and refreshed at most every 60s."""

    name = "unifi"
    missing_means_down = True   # UniFi drops a down port from port_table entirely

    def __init__(self, client=None, cache_seconds=None):
        self._client = client          # injected for tests
        self._cache_seconds = cache_seconds
        self._last_fetch = 0.0
        self._cached_devices = None    # raw stat/device list
        self._cached_wan = {}          # mac -> wan health dict

    def _cfg(self) -> dict:
        env = _read_env()
        return {
            "url": (env.get("UNIFI_URL") or os.getenv("UNIFI_URL", "")).strip(),
            "username": env.get("UNIFI_USER", "admin"),
            "password": env.get("UNIFI_PASSWORD", ""),
            "api_key": env.get("UNIFI_API_KEY", ""),
        }

    def _client_or_login(self):
        if self._client is not None:
            return self._client
        cfg = self._cfg()
        if not (cfg.get("api_key") or cfg.get("password")):
            return None
        from unifi import UniFiClient
        self._client = UniFiClient(cfg["url"], cfg["username"], cfg["password"],
                                   api_key=cfg.get("api_key") or None)
        if not self._client.login():
            self._client = None
            return None
        return self._client

    def collect(self, session) -> dict:
        cache_seconds = self._cache_seconds
        if cache_seconds is None:
            cache_seconds = link_monitor_config()["unifi_cache_seconds"]
        now_ts = time.time()
        client = self._client_or_login()
        if client is None:
            return None
        if self._cached_devices is None or (now_ts - self._last_fetch) >= cache_seconds:
            raw = client.get_raw_devices()
            if raw is None:
                err = client.last_error or ""
                if err.startswith(("HTTP 401", "HTTP 403")):
                    self._client = None  # re-login next cycle (session expired)
                return None
            self._cached_devices = raw
            self._cached_wan = {}
            self._last_fetch = now_ts

        macs = [(d.get("mac") or "").lower() for d in self._cached_devices]
        macs = [m for m in macs if m]
        rows = session.query(Device).filter(Device.mac_address.in_(macs)).all() if macs else []
        by_mac = {(r.mac_address or "").lower(): r for r in rows}

        out = {}
        for d in self._cached_devices:
            mac = (d.get("mac") or "").lower()
            row = by_mac.get(mac)
            if row is None:
                continue
            dtype = (d.get("type") or "").lower()
            if dtype in _GATEWAY_TYPES:
                wan = self._wan_health(mac)
                if wan is not None:
                    st = (wan.get("status") or "").lower()
                    out[(row.id, WAN)] = UP if st in ("ok", "up") else DOWN
            if dtype in _GATEWAY_TYPES or dtype in _SWITCH_TYPES:
                for pt in (d.get("port_table") or []):
                    idx = pt.get("port_idx")
                    if idx is None:
                        continue
                    name = (pt.get("name") or "").strip()
                    is_up = bool(pt.get("up"))
                    if not is_up and not name:
                        continue  # empty unnamed port — not a monitored link
                    out[(row.id, name or f"port {idx}")] = UP if is_up else DOWN
        return out

    def _wan_health(self, mac: str):
        if mac in self._cached_wan:
            return self._cached_wan[mac]
        if self._client is None:
            return None
        h = self._client.get_wan_health(mac)
        self._cached_wan[mac] = h
        return h

    def wan_picture(self, mac: str) -> "dict | None":
        """WAN health + the raw wan1/wan2 config for one gateway — the uplink
        card's UniFi data source. Reuses the same login + device cache as
        ``collect()`` so the link-flap monitor and the Devices uplink card
        share one controller session (no duplicate logins / 429s)."""
        client = self._client_or_login()
        if client is None:
            return None
        cache_seconds = self._cache_seconds
        if cache_seconds is None:
            cache_seconds = link_monitor_config()["unifi_cache_seconds"]
        if self._cached_devices is None or (time.time() - self._last_fetch) >= cache_seconds:
            raw = client.get_raw_devices()
            if raw is None:
                return None
            self._cached_devices = raw
            self._last_fetch = time.time()
        health = self._wan_health(mac)
        wan = {}
        target = (mac or "").lower()
        for d in (self._cached_devices or []):
            if (d.get("mac") or "").lower() == target:
                wan = {"wan1": d.get("wan1"), "wan2": d.get("wan2"),
                       "model": d.get("model") or "",
                       "name": d.get("name") or "",
                       "type": d.get("type") or ""}
                break
        return {"health": health, "wan": wan}


class SnmpChannel:
    """ifOperStatus + ifDescr for devices with snmp_configured (servers etc.).
    Reuses the SNMP executor's v2c/OID approach (scripts/snmp_executor.py,
    snmp_poll.sh) via snmpwalk — loopback + virtual interfaces are skipped."""

    name = "snmp"
    missing_means_down = True   # an interface that vanishes from ifDescr is down

    def collect(self, session) -> dict:
        rows = session.query(Device).filter(
            Device.notify_state_changes.is_(True),
            Device.snmp_community.isnot(None),
            Device.snmp_community != "",
            Device.unifi_managed.is_(False),
        ).all()
        out = {}
        for d in rows:
            community = _snmp_community(d.snmp_community)
            if not community:
                continue
            ifaces = _snmp_ifaces(d.ip_address, community)
            if ifaces is None:
                continue  # device unreachable — no signal this cycle
            for name, state in ifaces.items():
                out[(d.id, name)] = state
        return out


class StatusChannel:
    """Device.status as the fallback channel for opted-in devices (and any
    gateway) without UniFi/SNMP data. online -> up; offline/unreachable/warning
    -> down (matches the device monitor's DOWN_STATES); pending/unknown ->
    omitted (no signal — a device not yet observed healthy must not flap)."""

    name = "status"
    missing_means_down = False  # 'pending'/'unknown' = no signal, never down

    def collect(self, session) -> dict:
        rows = session.query(Device).filter(
            Device.unifi_managed.is_(False),
        ).filter(
            (Device.snmp_community.is_(None)) | (Device.snmp_community == "")
        ).filter(
            (Device.notify_state_changes.is_(True)) | (Device.device_type == "gateway")
        ).all()
        out = {}
        for d in rows:
            st = (d.status or "pending").lower()
            if st == "online":
                out[(d.id, STATUS_IFACE)] = UP
            elif st in ("offline", "unreachable", "warning"):
                out[(d.id, STATUS_IFACE)] = DOWN
        return out


def _snmp_community(value: str) -> str:
    try:
        from crypto import decrypt
        plain = decrypt(value or "")
    except Exception:
        return ""
    if not plain or plain == "[encrypted]":
        return ""
    return plain


def _snmpwalk(target: str, community: str, oid: str, timeout: int = 6):
    try:
        r = subprocess.run(
            ["snmpwalk", "-v2c", "-c", community, "-t", "3", "-r", "1",
             "-On", target, oid],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return r.stdout
    except Exception:
        return None


def _snmp_ifaces(target: str, community: str):
    """Walk ifDescr + ifOperStatus -> {interface_name: up|down}, or None when
    the device doesn't answer (unavailable, not empty)."""
    descr = _snmpwalk(target, community, _IFDESCR_OID)
    oper = _snmpwalk(target, community, _IFOPERSTATUS_OID)
    if descr is None or oper is None:
        return None
    names = {}
    for line in descr.splitlines():
        m = re.search(r"\.1\.3\.6\.1\.2\.1\.2\.2\.1\.2\.(\d+)\s*=\s*[^:]+:\s*(.+)$", line)
        if m:
            names[int(m.group(1))] = m.group(2).strip().strip('"')
    statuses = {}
    for line in oper.splitlines():
        m = re.search(r"\.1\.3\.6\.1\.2\.1\.2\.2\.1\.8\.(\d+)\s*=\s*[^:]+:\s*(\d+)$", line)
        if m:
            statuses[int(m.group(1))] = int(m.group(2))
    out = {}
    for idx, name in names.items():
        lname = (name or "").lower()
        if lname in _LOOPBACK_NAMES or lname.startswith(_VIRTUAL_IFACE_PREFIXES):
            continue
        status = statuses.get(idx, 2)  # ifOperStatus 1=up, else down
        out[name or f"if{idx}"] = UP if status == 1 else DOWN
    return out


# ── the monitor ─────────────────────────────────────────────────────────────

class LinkMonitor:
    """State machine over the merged channel snapshots. See module docstring."""

    def __init__(self, channels=None, now_fn=None, session_factory=SessionLocal):
        self._channels = channels if channels is not None else [
            UniFiChannel(), SnmpChannel(), StatusChannel()]
        self._now = now_fn or (lambda: datetime.datetime.utcnow())
        self._session_factory = session_factory
        self._last_channel_snapshot = {}   # channel name -> {key: state}
        self._last_email = {}              # key -> datetime of last email

    def check(self) -> None:
        cfg = link_monitor_config()
        if not cfg["enabled"]:
            return
        session = self._session_factory()
        try:
            meta, eligible_ids = self._device_meta(session)
            prev, snapshot = self._collect(session, eligible_ids)
            self._process(session, prev, snapshot, cfg, meta, eligible_ids)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("link monitor cycle error")
        finally:
            session.close()

    def _device_meta(self, session):
        """{device_id: {name, ip}} + the set of eligible (opted-in or gateway)
        device ids. Every gateway is always eligible so its WAN/ports are always
        watched; all other links are watched only when notify_state_changes is on."""
        meta = {}
        eligible = set()
        for d in session.query(Device).all():
            meta[d.id] = {"name": d.name or d.ip_address or f"device {d.id}",
                          "ip": d.ip_address or ""}
            if d.notify_state_changes or (d.device_type or "").lower() == "gateway":
                eligible.add(d.id)
        return meta, eligible

    def _collect(self, session, eligible_ids):
        """Merge live channel snapshots. A channel returning None keeps its
        previous view. For channels with missing_means_down (UniFi port tables
        drop a down port entirely; SNMP ifDescr can lose an interface), a
        previously-up link missing from a live snapshot is treated as down, and
        a previously-down missing link stays down (once tracked, keep tracking
        until the link recovers). Channels without it (status fallback) treat a
        missing key as no-signal this cycle — e.g. status 'pending'."""
        prev = {}
        for ch in self._channels:
            prev.update(self._last_channel_snapshot.get(ch.name, {}))
        merged = {}
        for ch in self._channels:
            snap = None
            try:
                snap = ch.collect(session)
            except Exception:
                logger.exception("link channel %s failed", ch.name)
                snap = None
            old = self._last_channel_snapshot.get(ch.name, {})
            if snap is None:
                snap = dict(old)  # channel unavailable — keep last view
            elif getattr(ch, "missing_means_down", True):
                for key, st in old.items():
                    if key not in snap:
                        snap[key] = DOWN if st == UP else st
            snap = {k: v for k, v in snap.items() if k[0] in eligible_ids}
            self._last_channel_snapshot[ch.name] = snap
            merged.update(snap)
        return prev, merged

    def _process(self, session, prev, snapshot, cfg, meta, eligible_ids):
        episodes = {(e.device_id, e.interface): e
                    for e in session.query(LinkEpisode).all()}
        now = self._now()
        for key, state in snapshot.items():
            if state not in (UP, DOWN):
                continue
            ep = episodes.get(key)
            # A wan_probe-promoted episode is owned by the internet probe, not
            # this state machine: UniFi WAN "ok" does NOT mean the internet is
            # reachable (an upstream ISP outage reads ok on the WAN link). The
            # link monitor must not flap-count or auto-close it;
            # alerting.InternetMonitor._recovered closes it on confirmed recovery.
            if ep is not None and ep.escalation_reason == "wan_probe":
                continue
            self._advance(session, key, prev.get(key), state, ep, cfg, now, meta)
        # A device opted out of monitoring (notify_state_changes toggled off)
        # mid-episode, or deleted, would otherwise leave an orphan episode +
        # open ticket forever. Close it so the toggle-off is honored immediately.
        for key, ep in episodes.items():
            if ep.device_id not in eligible_ids:
                self._close_unwatched(session, key, ep, now, meta)

    # ── state machine ──────────────────────────────────────────

    def _ticket(self, session, ep):
        if not ep.ticket_id:
            return None
        return session.query(Ticket).filter(Ticket.ticket_id == ep.ticket_id).first()

    def _flaps_in_window(self, ep, cfg, now):
        ts = ep.flap_timestamps or []
        if not ts:
            return 0
        cutoff = now - datetime.timedelta(minutes=cfg["flap_window_min"])
        n = 0
        for t in ts:
            try:
                dt = datetime.datetime.fromisoformat(str(t))
            except (ValueError, TypeError):
                continue
            if dt >= cutoff:
                n += 1
        return n

    def _advance(self, session, key, prev_state, state, ep, cfg, now, meta):
        if ep is None:
            # no episode: only a transition from a known baseline opens one
            # (first observation seeds the baseline — never alert on day one)
            if prev_state is not None and prev_state != state:
                self._open_episode(session, key, prev_state, state, now, meta)
            return

        if state == DOWN:
            if ep.down_since is None:
                # transition into down (or resumed into down): start the timer
                ep.down_since = now
                ep.last_event_at = now
                ep.state = "flapping"
            if ep.state != "outage":
                age = (now - ep.down_since).total_seconds() if ep.down_since else 0
                if age >= cfg["persist_down_min"] * 60:
                    self._escalate_outage(session, key, ep, cfg, now, meta)
        else:  # UP
            if ep.down_since is not None:
                # recovery -> one flap (down->up)
                ep.flap_count = (ep.flap_count or 0) + 1
                ep.flap_timestamps = list(ep.flap_timestamps or []) + [now.isoformat()]
                ep.down_since = None
                ep.last_event_at = now
                ep.state = "flapping"
                if (ep.escalation_reason != "recurrence"
                        and self._flaps_in_window(ep, cfg, now) >= cfg["escalate_count"]):
                    self._escalate_recurrence(session, key, ep, cfg, now, meta)
            else:
                last = ep.last_event_at or ep.window_start
                if last and (now - last).total_seconds() >= cfg["stable_close_min"] * 60:
                    self._close_episode(session, key, ep, cfg, now, meta)

    def _open_episode(self, session, key, prev_state, state, now, meta):
        device_id, iface = key
        name = meta.get(device_id, {}).get("name", f"device {device_id}")
        flap_count = 0
        timestamps = []
        down_since = None
        if prev_state == UP and state == DOWN:
            down_since = now
        elif prev_state == DOWN and state == UP:
            flap_count = 1
            timestamps = [now.isoformat()]
        title = f"Link flap: {name} {iface}"
        ticket = Ticket(
            ticket_id=generate_ticket_id(),
            title=title,
            description=f"Link stability event on {name} {iface}: {prev_state} → {state}.",
            priority="P2", status="open", source="auto",
            assigned_to="system", target_device_id=device_id,
        )
        session.add(ticket)
        ep = LinkEpisode(
            device_id=device_id, interface=iface, state="flapping",
            flap_count=flap_count, flap_timestamps=timestamps,
            window_start=now, down_since=down_since, last_event_at=now,
            escalated="P2", escalation_reason=None, ticket_id=ticket.ticket_id,
        )
        session.add(ep)
        log_event(session, "link_flap", "system", {
            "device_id": device_id, "interface": iface,
            "transition": f"{prev_state}->{state}", "ticket_id": ticket.ticket_id,
        }, ticket.ticket_id)
        self._email(key, f"[P2] BareNOC: {title}", "Link flap detected",
                    [("Device", name), ("Interface", iface),
                     ("Transition", f"{prev_state} → {state}"),
                     ("Ticket", ticket.ticket_id)],
                    f"Link flap on {name} {iface}: {prev_state} -> {state}.")

    def _escalate_recurrence(self, session, key, ep, cfg, now, meta):
        device_id, iface = key
        name = meta.get(device_id, {}).get("name", f"device {device_id}")
        ticket = self._ticket(session, ep)
        if ticket is not None:
            if ticket.priority != "P1":
                ticket.priority = "P1"
            ticket.title = f"Recurring link flap: {name} {iface}"
            add_note(ticket, "link_flap_escalate",
                     f"Recurring link flap: {ep.flap_count} flap(s) within "
                     f"{cfg['flap_window_min']} min. "
                     f"Timestamps: {', '.join(ep.flap_timestamps or [])}")
            log_event(session, "link_flap_escalate", "system", {
                "ticket_id": ticket.ticket_id, "interface": iface,
                "flap_count": ep.flap_count,
            }, ticket.ticket_id)
        ep.escalated = "P1"
        ep.escalation_reason = "recurrence"
        ep.updated_at = now
        self._email(key, f"[P1] BareNOC: Recurring link flap: {name} {iface}",
                    "Recurring link flap",
                    [("Device", name), ("Interface", iface),
                     ("Flaps", str(ep.flap_count)),
                     ("Window", f"{cfg['flap_window_min']} min")],
                    f"Recurring link flap on {name} {iface}: {ep.flap_count} flaps.")

    def _escalate_outage(self, session, key, ep, cfg, now, meta):
        device_id, iface = key
        name = meta.get(device_id, {}).get("name", f"device {device_id}")
        ticket = self._ticket(session, ep)
        if ticket is not None:
            if ticket.priority != "P1":
                ticket.priority = "P1"
            ticket.title = f"Link outage: {name} {iface}"
            add_note(ticket, "link_outage",
                     f"Link down for more than {cfg['persist_down_min']} min — "
                     "persistent outage (not flapping).")
            log_event(session, "link_outage", "system", {
                "ticket_id": ticket.ticket_id, "interface": iface,
                "down_since": (ep.down_since.isoformat() if ep.down_since else None),
            }, ticket.ticket_id)
        ep.escalated = "P1"
        ep.escalation_reason = "outage"
        ep.state = "outage"
        ep.updated_at = now
        self._email(key, f"[P1] BareNOC: Link outage: {name} {iface}",
                    "Link outage",
                    [("Device", name), ("Interface", iface),
                     ("Down for", f"> {cfg['persist_down_min']} min")],
                    f"Link outage on {name} {iface}: down > {cfg['persist_down_min']} min.")

    def _close_episode(self, session, key, ep, cfg, now, meta):
        device_id, iface = key
        name = meta.get(device_id, {}).get("name", f"device {device_id}")
        ticket = self._ticket(session, ep)
        if ticket is not None:
            add_note(ticket, "link_stable",
                     f"Auto-closed after {cfg['stable_close_min']} min with no further "
                     f"events. {ep.flap_count or 0} flap(s), episode window "
                     f"{(ep.window_start.isoformat() if ep.window_start else '?')} → "
                     f"{now.isoformat()}. Timestamps: "
                     f"{', '.join(ep.flap_timestamps or []) or 'none'}.")
            ticket.status = "closed"
            ticket.resolution = (f"Link stable for {cfg['stable_close_min']} min — "
                                 "episode auto-closed")
            ticket.resolved_at = now
            log_event(session, "link_stable", "system", {
                "ticket_id": ticket.ticket_id, "interface": iface,
                "flap_count": ep.flap_count,
            }, ticket.ticket_id)
        session.delete(ep)
        self._email(key, f"[CLOSED] BareNOC: Link stable: {name} {iface}",
                    "Link stable",
                    [("Device", name), ("Interface", iface),
                     ("Flaps", str(ep.flap_count or 0))],
                    f"Link stable on {name} {iface} — episode closed.")

    def _close_unwatched(self, session, key, ep, now, meta):
        """Close an episode whose device is no longer monitored (opt-in toggled
        off, or the device was deleted). Honors the notify_state_changes toggle
        immediately instead of leaving an orphan open ticket."""
        device_id, iface = key
        name = meta.get(device_id, {}).get("name", f"device {device_id}")
        ticket = self._ticket(session, ep)
        if ticket is not None and ticket.status in ("open", "in_progress"):
            add_note(ticket, "link_monitor_off",
                     f"Device no longer monitored (link-stability opt-in off) — "
                     f"episode closed. {ep.flap_count or 0} flap(s) recorded.")
            ticket.status = "closed"
            ticket.resolution = "Device removed from link-stability monitoring"
            ticket.resolved_at = now
            log_event(session, "link_monitor_off", "system", {
                "ticket_id": ticket.ticket_id, "device_id": device_id,
                "interface": iface,
            }, ticket.ticket_id)
        session.delete(ep)

    def _email(self, key, subject, title, rows, body_text):
        now = self._now()
        last = self._last_email.get(key)
        if last and (now - last).total_seconds() < FLAP_MIN_INTERVAL:
            return
        self._last_email[key] = now
        send_email(get_recipients("alerts"), subject,
                   body_html=alert_html(title, rows), body_text=body_text)


# ── WAN single-ticket lifecycle helpers (used by alerting.InternetMonitor) ──

def find_open_wan_episode(session):
    """The open gateway-WAN episode (interface == 'wan'), if any. The WAN flap
    ticket IS the WAN outage ticket, so the internet probe promotes it rather
    than opening a duplicate."""
    return session.query(LinkEpisode).filter(LinkEpisode.interface == WAN).first()


def promote_wan_ticket(session, episode) -> bool:
    """Promote an open WAN link-flap ticket to P1 (update title/notes). Returns
    True when a promotion happened. Caller commits."""
    if episode is None or not episode.ticket_id:
        return False
    ticket = session.query(Ticket).filter(Ticket.ticket_id == episode.ticket_id).first()
    if ticket is None or ticket.status not in ("open", "in_progress"):
        return False
    device = session.get(Device, episode.device_id) if episode.device_id else None
    name = (device.name if device else None) or "gateway"
    if ticket.priority != "P1":
        ticket.priority = "P1"
    ticket.title = f"Link outage: {name} {WAN}"
    add_note(ticket, "wan_outage",
             "Internet probe confirmed real internet loss (3 consecutive 60s "
             "failures) — promoting the open WAN link-flap ticket to P1 instead "
             "of opening a duplicate 'Internet connectivity down' ticket.")
    episode.escalated = "P1"
    episode.escalation_reason = "wan_probe"
    episode.state = "outage"
    return True
