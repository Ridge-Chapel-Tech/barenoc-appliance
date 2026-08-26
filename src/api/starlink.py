"""Starlink dish gRPC telemetry collector + link-health monitor.

Complements the port-level link-flap monitor (which catches full OUTAGES on
the WAN port): the dish exposes rich telemetry over a LOCAL, unauthenticated
gRPC API at 192.168.100.1:9200 — ping latency (dish-reported), link up/down
(dish view), throughput up/down, signal quality + obstruction stats, uptime
and drop counts. This collector polls it on a cadence and writes the values
into the metrics store (device = the dish), so the NOC sees link HEALTH
(degradation *before* an outage).

The gRPC client is the light `starlink-grpc-core` package (just the reflection
client + grpcio/protobuf/yagrc — no influxdb/mqtt/prometheus bloat). It is
imported lazily so this module (and the test suite) still load without grpc
installed. If the package is missing, or the dish is unreachable, the collector
returns no samples — an honest GAP (the trends API omits empty buckets) — and
never raises.

Health state machine (graduated-ticket pattern, mirroring link_monitor.py):
  sustained degradation -> P2 ticket "Starlink link degraded" (kept open)
  dish link down (its view) -> SAME ticket escalates to P1 "Starlink link outage"
  sustained recovery        -> auto-close with a summary note

Persistence: starlink_episodes table (one row per dish device) so a container
restart resumes an in-flight episode. The TICKET is the record; email is
best-effort (no-ops when SMTP is unconfigured).

This module is imported by telemetry.py (collector) and alerting.py (health
monitor). It must NOT import alerting back.
"""

import datetime
import logging
import os
import threading
import time

from database import SessionLocal
from models import Device, Ticket, StarlinkEpisode
from schemas import generate_ticket_id
from emailer import send_email, get_recipients, alert_html
from audit import log_event
from worknotes import add_note

logger = logging.getLogger("barenoc.starlink")

DEFAULT_ADDRESS = "192.168.100.1:9200"
DEFAULT_INTERVAL = 60          # seconds between polls
DEFAULT_TIMEOUT = 10           # gRPC call timeout
EMAIL_MIN_INTERVAL = 1800      # 30 min: min gap between emails (one dish)

# Metric names (device = the dish). ``snr`` is the dish's "signal above noise
# floor" boolean (the modern dish gRPC API obsoleted the raw SNR value), so it
# is stored as 0/1 — honest about what the API actually reports.
METRIC_PING_MS = "starlink.ping_ms"
METRIC_LINK_UP = "starlink.link_up"
METRIC_DOWN_MBPS = "starlink.down_mbps"
METRIC_UP_MBPS = "starlink.up_mbps"
METRIC_SNR = "starlink.snr"
METRIC_OBSTRUCTED = "starlink.obstructed"
METRIC_OBSTRUCTION_FRACTION = "starlink.obstruction_fraction"
METRIC_UPTIME = "starlink.uptime_seconds"
METRIC_DROP_RATE = "starlink.ping_drop_rate"

ALL_METRICS = [METRIC_PING_MS, METRIC_LINK_UP, METRIC_DOWN_MBPS, METRIC_UP_MBPS,
               METRIC_SNR, METRIC_OBSTRUCTED, METRIC_OBSTRUCTION_FRACTION,
               METRIC_UPTIME, METRIC_DROP_RATE]

HEALTHY = "healthy"
DEGRADED = "degraded"
OUTAGE = "outage"


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
    for k, v in os.environ.items():
        if k.startswith("STARLINK_"):
            env.setdefault(k, v)
    return env


def starlink_config(env: dict = None) -> dict:
    env = env if env is not None else _read_env()

    def s(key, default):
        return (env.get(key) or default or "").strip()

    def b(key, default):
        return s(key, default).lower() in ("1", "true", "yes", "on")

    def i(key, default):
        try:
            return int(env.get(key) or default)
        except (TypeError, ValueError):
            return default

    def f(key, default):
        try:
            return float(env.get(key) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": b("STARLINK_ENABLED", "true"),
        "address": s("STARLINK_ADDRESS", DEFAULT_ADDRESS) or DEFAULT_ADDRESS,
        "interval": max(15, i("STARLINK_INTERVAL_S", DEFAULT_INTERVAL)),
        "timeout_s": max(2, i("STARLINK_TIMEOUT_S", DEFAULT_TIMEOUT)),
        # degradation thresholds (sustained over degrade_window_min -> P2)
        "ping_degrade_ms": max(1.0, f("STARLINK_PING_DEGRADE_MS", 150.0)),
        "snr_min": f("STARLINK_SNR_MIN", 1.0),          # 1 = above noise floor
        "down_min_mbps": max(0.0, f("STARLINK_DOWN_MIN_MBPS", 10.0)),
        "up_min_mbps": max(0.0, f("STARLINK_UP_MIN_MBPS", 2.0)),
        "obstruction_max": min(1.0, max(0.0, f("STARLINK_OBSTRUCTION_MAX", 0.2))),
        # state-machine windows (minutes)
        "degrade_window_min": max(1, i("STARLINK_DEGRADE_WINDOW_MIN", 5)),
        "outage_window_min": max(1, i("STARLINK_OUTAGE_WINDOW_MIN", 3)),
        "recover_window_min": max(1, i("STARLINK_RECOVER_WINDOW_MIN", 10)),
    }


# ── gRPC client (isolated so tests never need grpc) ─────────────────────────

class StarlinkClient:
    """Thin wrapper over starlink-grpc-core's reflection client.

    ``fetch_status`` returns a normalized dict (or None when the dish is
    unreachable / the package is missing). It NEVER raises — a bad poll is a
    gap, not a crash."""

    def __init__(self, address: str = None, timeout_s: int = None):
        self.address = address or DEFAULT_ADDRESS
        self.timeout_s = timeout_s or DEFAULT_TIMEOUT

    def fetch_status(self) -> "dict | None":
        try:
            from starlink_grpc import status_data, ChannelContext
        except Exception as e:
            logger.warning("starlink-grpc-core unavailable (%s) — collector no-op", e)
            return None
        ctx = ChannelContext(self.address)
        try:
            status, obstruction, _alerts = status_data(ctx)
            return normalize_status(status, obstruction)
        except Exception as e:
            logger.warning("starlink gRPC poll failed (%s): %s", self.address, e)
            return None
        finally:
            ctx.close()


# ── normalization + extraction (pure, testable) ─────────────────────────────

def _float(v):
    try:
        if v is None:
            return None
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _bool01(v):
    if v is None:
        return None
    return 1.0 if v else 0.0


def _mbps(bps):
    """bps -> Mbps (the dish reports throughput in bits/sec)."""
    v = _float(bps)
    return None if v is None else round(v / 1_000_000.0, 4)


def normalize_status(status: dict, obstruction: dict = None) -> dict:
    """Normalize a starlink_grpc.status_data() status dict into the flat
    snapshot the collector + health monitor share. ``snr`` maps
    ``is_snr_above_noise_floor`` to 0/1 (raw SNR is obsoleted in the modern
    dish gRPC API)."""
    obstruction = obstruction or {}
    state = str(status.get("state") or "UNKNOWN").upper()
    return {
        "state": state,
        "link_up": state == "CONNECTED",
        "ping_ms": _float(status.get("pop_ping_latency_ms")),
        "down_mbps": _mbps(status.get("downlink_throughput_bps")),
        "up_mbps": _mbps(status.get("uplink_throughput_bps")),
        "snr": _bool01(status.get("is_snr_above_noise_floor")),
        "obstructed": _bool01(status.get("currently_obstructed")),
        "obstruction_fraction": _float(status.get("fraction_obstructed")),
        "uptime_seconds": _float(status.get("uptime")),
        "ping_drop_rate": _float(status.get("pop_ping_drop_rate")),
    }


def collect_starlink_metrics(device_id: int, snapshot: dict) -> list:
    """Gauge samples for one normalized status snapshot. All fields are
    instantaneous gauges (the dish reports throughput in bps directly, not as
    cumulative counters), so no counter->rate math is needed."""
    now = datetime.datetime.utcnow()
    out = []

    def g(metric, value):
        if value is None:
            return
        out.append({"device_id": device_id, "metric": metric,
                    "value": float(value), "ts": now, "kind": "gauge"})

    g(METRIC_PING_MS, snapshot.get("ping_ms"))
    g(METRIC_LINK_UP, snapshot.get("link_up"))
    g(METRIC_DOWN_MBPS, snapshot.get("down_mbps"))
    g(METRIC_UP_MBPS, snapshot.get("up_mbps"))
    g(METRIC_SNR, snapshot.get("snr"))
    g(METRIC_OBSTRUCTED, snapshot.get("obstructed"))
    g(METRIC_OBSTRUCTION_FRACTION, snapshot.get("obstruction_fraction"))
    g(METRIC_UPTIME, snapshot.get("uptime_seconds"))
    g(METRIC_DROP_RATE, snapshot.get("ping_drop_rate"))
    return out


# ── classification (pure, testable) ─────────────────────────────────────────

def classify(snapshot: dict, cfg: dict) -> tuple:
    """("healthy" | "degraded" | "outage", [reasons]).

    A dish-reported link-down is an OUTAGE; otherwise any threshold breach is
    DEGRADED; otherwise healthy. Only ``link_up is False`` (not a gap/None)
    counts as an outage — an unreachable dish is handled upstream as a gap."""
    if snapshot.get("link_up") is False:
        return OUTAGE, ["link down (dish view)"]
    reasons = []
    ping = snapshot.get("ping_ms")
    if ping is not None and ping > cfg["ping_degrade_ms"]:
        reasons.append(f"ping {ping:.0f}ms > {cfg['ping_degrade_ms']:.0f}ms")
    snr = snapshot.get("snr")
    if snr is not None and snr < cfg["snr_min"]:
        reasons.append("signal below noise floor")
    down = snapshot.get("down_mbps")
    if down is not None and down < cfg["down_min_mbps"]:
        reasons.append(f"down {down:.1f} Mbps < {cfg['down_min_mbps']:.1f}")
    up = snapshot.get("up_mbps")
    if up is not None and up < cfg["up_min_mbps"]:
        reasons.append(f"up {up:.1f} Mbps < {cfg['up_min_mbps']:.1f}")
    obstructed = snapshot.get("obstruction_fraction")
    if obstructed is not None and obstructed > cfg["obstruction_max"]:
        reasons.append(f"obstruction {obstructed:.0%} > {cfg['obstruction_max']:.0%}")
    if reasons:
        return DEGRADED, reasons
    return HEALTHY, []


# ── shared snapshot cache (one gRPC poll per interval, both consumers) ──────

_CLIENT = None
_CACHE = {"snapshot": None, "fetched_at": 0.0}
_CACHE_LOCK = threading.Lock()


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = StarlinkClient()
    return _CLIENT


def get_snapshot(cfg: dict, client=None, force: bool = False):
    """Return the latest normalized snapshot (or None = gap). Cached for one
    interval so the telemetry collector and the health monitor share a single
    gRPC poll per cycle. ``client`` is injectable for tests."""
    client = client if client is not None else _client()
    client.address = cfg.get("address") or DEFAULT_ADDRESS
    client.timeout_s = cfg.get("timeout_s") or DEFAULT_TIMEOUT
    now = time.time()
    if not force and (now - _CACHE["fetched_at"]) < cfg.get("interval", DEFAULT_INTERVAL):
        return _CACHE["snapshot"]
    with _CACHE_LOCK:
        if not force and (time.time() - _CACHE["fetched_at"]) < cfg.get("interval", DEFAULT_INTERVAL):
            return _CACHE["snapshot"]
        snapshot = client.fetch_status()
        _CACHE["snapshot"] = snapshot
        _CACHE["fetched_at"] = time.time()
        return snapshot


# ── dish device record ──────────────────────────────────────────────────────

def _dish_ip(address: str) -> str:
    return address.split(":")[0] if ":" in (address or "") else (address or DEFAULT_ADDRESS.split(":")[0])


PHANTOM_METRIC_WINDOW_DAYS = 7  # telemetry within this window = a real dish

def starlink_has_live_dish() -> bool:
    """True when a REAL dish is evidenced on this box: a dish device record
    with telemetry within PHANTOM_METRIC_WINDOW_DAYS (the 08-26 evidence
    rule — same test the purge uses). Drives the System 'Starlink Link
    Health' card, which must not render for no-dish boxes (forum 9eaa106e)."""
    from models import Device, Metric
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=PHANTOM_METRIC_WINDOW_DAYS)
    db = SessionLocal()
    try:
        for d in db.query(Device).filter(Device.device_type == "dish").all():
            if db.query(Metric).filter(
                    Metric.device_id == d.id, Metric.ts >= cutoff).first():
                return True
    finally:
        db.close()
    return False


def purge_phantom_dish_at_startup() -> None:
    """Startup sweep: remove dish records on boxes with no real dish, even when
    the telemetry collector is disabled or has never run (the 08-20 phantom fix
    only purged inside the collector loop — boxes that updated but had
    STARLINK_ENABLED=false kept the fabricated 'Starlink Dish' record; see
    forum thread 9eaa106e). A real configured dish in an outage keeps its
    record (the purge's keep rule). Idempotent + crash-safe."""
    try:
        db = SessionLocal()
        try:
            scfg = starlink_config()
            _purge_phantom_dish(db, scfg["address"])
        finally:
            db.close()
    except Exception as e:
        logger.warning("starlink startup phantom purge failed: %s", e)


def _purge_phantom_dish(db, address: str) -> None:
    """Remove dish device records that have no real dish behind them.

    A phantom = a dish-type record with NO recent telemetry (found 08-20: the
    appliance fabricated+claimed a 'Starlink Dish' record on every box; the
    old keep rule then preserved it because the installer seeds the DEFAULT
    dish address in .env — forum thread 9eaa106e). A real dish — evidenced by
    telemetry rows within PHANTOM_METRIC_WINDOW_DAYS — keeps its record, even
    mid-outage."""
    # Evidence-based keep rule (08-26): a dish record is REAL only when it has
    # recent telemetry (the collector wrote metric rows for it — a real dish
    # answers; phantoms never do). The old rule (STARLINK_ADDRESS set) kept
    # phantoms on no-dish boxes because the installer seeds the DEFAULT dish
    # address (192.168.100.1) in .env — forum thread 9eaa106e. A real dish in
    # an outage keeps its record (metrics exist from before the outage).
    from models import Metric
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=PHANTOM_METRIC_WINDOW_DAYS)
    phantoms = []
    for d in db.query(Device).filter(Device.device_type == "dish").all():
        has_evidence = db.query(Metric).filter(
            Metric.device_id == d.id, Metric.ts >= cutoff).first() is not None
        if not has_evidence:
            phantoms.append(d)
    for d in phantoms:
        log_event(db, "starlink_phantom_removed", "system",
                  {"ip": d.ip_address}, None)
        # Clear the dish's own rows first (FK: metrics reference devices.id
        # without a cascade — a phantom's stale evidence rows go with it).
        db.query(Metric).filter(Metric.device_id == d.id).delete()
        db.delete(d)
    if phantoms:
        db.commit()


def ensure_dish_device(db, address: str = DEFAULT_ADDRESS) -> int:
    """Find-or-create the device record that owns starlink.* metrics + tickets.
    Identified by device_type == 'dish' first, then the dish IP. Commits its
    own write (self-contained helper)."""
    ip = _dish_ip(address)
    d = db.query(Device).filter(Device.device_type == "dish").order_by(Device.id.asc()).first()
    if d is None:
        d = db.query(Device).filter(Device.ip_address == ip).first()
    if d is None:
        d = Device(name="Starlink Dish", ip_address=ip, device_type="dish",
                   vendor="Starlink", model="Dish", status="online",
                   claimed=True, channels=["monitor"])
        db.add(d)
        db.flush()
        log_event(db, "starlink_device_created", "system",
                  {"ip": ip, "device_type": "dish"}, None)
    else:
        changed = False
        if d.ip_address != ip:
            d.ip_address = ip
            changed = True
        if d.vendor != "Starlink":
            d.vendor = "Starlink"
            changed = True
        if d.claimed is not True:
            d.claimed = True
            changed = True
        if changed:
            db.commit()
    return d.id


def _set_device_status(db, device_id: int, status: str):
    d = db.get(Device, device_id)
    if d is not None:
        if d.status != status:
            d.status = status
        d.last_seen = datetime.datetime.utcnow()


def collect_starlink_telemetry(cfg: dict, db) -> list:
    """One Starlink collection pass: fetch + write gauge samples. Returns the
    samples for telemetry._persist to batch-write. On a gap returns [] (honest
    gap — no fabricated samples)."""
    scfg = starlink_config()
    if not scfg["enabled"]:
        return []
    snapshot = get_snapshot(scfg)
    if snapshot is None:
        # No reachable dish — honest gap: never fabricate a record, and purge
        # a phantom one (a dish record on a box with no real Starlink).
        _purge_phantom_dish(db, scfg["address"])
        return []
    device_id = ensure_dish_device(db, scfg["address"])
    _set_device_status(db, device_id, "online")
    db.commit()
    return collect_starlink_metrics(device_id, snapshot)


# ── health monitor (graduated-ticket state machine) ─────────────────────────

class StarlinkHealthMonitor:
    """Watches the dish's link HEALTH and drives the degraded->P2 / outage->P1
    / recovery->close ticket lifecycle. Reads the shared snapshot cache (no
    gRPC of its own unless the cache is cold), so it complements — not
    duplicates — the telemetry collector."""

    def __init__(self, client=None, now_fn=None, session_factory=SessionLocal):
        self._client = client
        self._now = now_fn or (lambda: datetime.datetime.utcnow())
        self._session_factory = session_factory
        self._last_email = 0.0

    def check(self) -> None:
        cfg = starlink_config()
        if not cfg["enabled"]:
            return
        snapshot = get_snapshot(cfg, client=self._client)
        if snapshot is None:
            # unreachable — honest gap; also purge a phantom dish record (a box
            # with no real Starlink must not hold a fabricated 'Starlink Dish').
            session = self._session_factory()
            try:
                _purge_phantom_dish(session, cfg["address"])
                session.commit()
            except Exception:
                session.rollback()
            return
        session = self._session_factory()
        try:
            device_id = ensure_dish_device(session, cfg["address"])
            self._advance(session, device_id, snapshot, cfg)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("starlink health monitor cycle error")
        finally:
            session.close()

    def _device_name(self, session, device_id: int) -> str:
        d = session.get(Device, device_id)
        return (d.name if d else None) or "Starlink Dish"

    def _ticket(self, session, ep):
        if not ep.ticket_id:
            return None
        return session.query(Ticket).filter(Ticket.ticket_id == ep.ticket_id).first()

    def _advance(self, session, device_id, snapshot, cfg):
        now = self._now()
        state, reasons = classify(snapshot, cfg)
        ep = session.query(StarlinkEpisode).filter(
            StarlinkEpisode.device_id == device_id).first()
        if ep is None:
            # First observation seeds the baseline — never alert on day one.
            ep = StarlinkEpisode(
                device_id=device_id, state=state,
                degraded_since=now if state in (DEGRADED, OUTAGE) else None,
                outage_since=now if state == OUTAGE else None,
                last_event_at=now, escalated=None, escalation_reason=None)
            session.add(ep)
            return
        ep.last_event_at = now
        if state == HEALTHY:
            self._on_healthy(session, ep, now, cfg)
        elif state == DEGRADED:
            self._on_degraded(session, ep, now, cfg, reasons)
        else:
            self._on_outage(session, ep, now, cfg, reasons)

    def _on_healthy(self, session, ep, now, cfg):
        ep.degraded_since = None
        ep.outage_since = None
        if ep.recovered_since is None:
            ep.recovered_since = now
        if ep.state in (DEGRADED, OUTAGE) and ep.recovered_since is not None:
            if (now - ep.recovered_since).total_seconds() >= cfg["recover_window_min"] * 60:
                self._close(session, ep, now, cfg)

    def _on_degraded(self, session, ep, now, cfg, reasons):
        ep.recovered_since = None
        ep.outage_since = None  # link is up again (was outage -> degraded)
        if ep.degraded_since is None:
            ep.degraded_since = now
        if ep.state == HEALTHY:
            if (now - ep.degraded_since).total_seconds() >= cfg["degrade_window_min"] * 60:
                self._open_degraded(session, ep, now, cfg, reasons)
        elif ep.state == OUTAGE:
            # Recovered from a full outage to "just degraded" — keep the ticket.
            ep.state = DEGRADED
            t = self._ticket(session, ep)
            if t is not None:
                add_note(t, "starlink_link_restored_degraded",
                         "Link restored (dish view) but still degraded: "
                         + "; ".join(reasons) or "degradation persists")

    def _on_outage(self, session, ep, now, cfg, reasons):
        ep.recovered_since = None
        if ep.degraded_since is None:
            ep.degraded_since = now
        if ep.outage_since is None:
            ep.outage_since = now
        if ep.state != OUTAGE:
            ep.state = OUTAGE
        if ep.escalated != "P1" and ep.outage_since is not None:
            if (now - ep.outage_since).total_seconds() >= cfg["outage_window_min"] * 60:
                self._escalate_outage(session, ep, now, cfg, reasons)

    def _open_degraded(self, session, ep, now, cfg, reasons):
        device_id = ep.device_id
        name = self._device_name(session, device_id)
        title = "Starlink link degraded"
        ticket = Ticket(
            ticket_id=generate_ticket_id(),
            title=title,
            description="Starlink link health degraded (sustained): "
                        + "; ".join(reasons),
            priority="P2", status="open", source="auto",
            assigned_to="system", target_device_id=device_id)
        session.add(ticket)
        ep.state = DEGRADED
        ep.escalated = "P2"
        ep.escalation_reason = "degraded"
        ep.ticket_id = ticket.ticket_id
        ep.updated_at = now
        log_event(session, "starlink_degraded", "system", {
            "device_id": device_id, "ticket_id": ticket.ticket_id,
            "reasons": reasons,
        }, ticket.ticket_id)
        self._email(device_id, f"[P2] BareNOC: {title}",
                    "Starlink link degraded",
                    [("Device", name), ("Ticket", ticket.ticket_id),
                     ("Reasons", "; ".join(reasons))],
                    f"Starlink link degraded: {'; '.join(reasons)}.")

    def _escalate_outage(self, session, ep, now, cfg, reasons):
        device_id = ep.device_id
        name = self._device_name(session, device_id)
        ticket = self._ticket(session, ep)
        if ticket is not None:
            if ticket.priority != "P1":
                ticket.priority = "P1"
            ticket.title = "Starlink link outage"
            add_note(ticket, "starlink_outage",
                     "Dish reports link down (its view) — escalating the open "
                     "degradation ticket to P1.")
            log_event(session, "starlink_outage", "system", {
                "ticket_id": ticket.ticket_id, "device_id": device_id,
                "reasons": reasons,
            }, ticket.ticket_id)
        ep.escalated = "P1"
        ep.escalation_reason = "outage"
        ep.state = OUTAGE
        ep.updated_at = now
        self._email(device_id, f"[P1] BareNOC: Starlink link outage",
                    "Starlink link outage",
                    [("Device", name),
                     ("Ticket", ticket.ticket_id if ticket else "—"),
                     ("Reasons", "; ".join(reasons))],
                    f"Starlink link outage: {'; '.join(reasons)}.")

    def _close(self, session, ep, now, cfg):
        device_id = ep.device_id
        name = self._device_name(session, device_id)
        ticket = self._ticket(session, ep)
        if ticket is not None:
            add_note(ticket, "starlink_recovered",
                     f"Auto-closed after {cfg['recover_window_min']} min of healthy "
                     f"metrics. Degraded since "
                     f"{(ep.degraded_since.isoformat() if ep.degraded_since else '?')}.")
            ticket.status = "closed"
            ticket.resolution = (f"Starlink link healthy for "
                                 f"{cfg['recover_window_min']} min — episode auto-closed")
            ticket.resolved_at = now
            log_event(session, "starlink_recovered", "system", {
                "ticket_id": ticket.ticket_id, "device_id": device_id,
            }, ticket.ticket_id)
        session.delete(ep)
        self._email(device_id, f"[CLOSED] BareNOC: Starlink link healthy",
                    "Starlink link healthy",
                    [("Device", name)], "Starlink link healthy — episode closed.")

    def _email(self, key, subject, title, rows, body_text):
        now = time.time()
        if now - self._last_email < EMAIL_MIN_INTERVAL:
            return
        self._last_email = now
        send_email(get_recipients("alerts"), subject,
                   body_html=alert_html(title, rows), body_text=body_text)
