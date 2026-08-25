import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, ForeignKey, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from database import Base


# ── Role tiers (2026-08-18) ────────────────────────────────────────────────
# Three customer-facing tiers: user (customer) < technician < admin.
# Legacy roles map additively: operator == technician, tenant == user;
# readonly = read-only staff (above user, below technician); agent = a
# service identity that only ever uses exact-match require_any_role (never
# the hierarchy). Single source of truth — BOTH the API and the worker import
# models.py, so keep this dependency-free.
ROLE_LEVELS = {
    "admin": 4,
    "technician": 3,
    "operator": 3,     # legacy alias for technician
    "readonly": 2,     # read-only staff view
    "user": 1,         # customer
    "tenant": 1,       # legacy alias for user
    "agent": 0,        # service identity (exact-match only)
}
TECH_ROLES = ("admin", "technician", "operator")
CUSTOMER_ROLES = ("user", "tenant")


def is_tech(user) -> bool:
    """True for the technician tier (technician + legacy operator) and admin."""
    return getattr(user, "role", "") in TECH_ROLES


def is_customer(user) -> bool:
    """True for the customer tier (user + legacy tenant)."""
    return getattr(user, "role", "") in CUSTOMER_ROLES


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    email = Column(String(128), unique=True, nullable=True)
    display_name = Column(String(128), nullable=True)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(16), default="admin")  # admin | technician | user (+ legacy operator | readonly | tenant)
    is_active = Column(Boolean, default=True)
    is_bot = Column(Boolean, default=False)  # True for bot users (Juniper Queue Manager) — chat participants, not humans
    must_change_password = Column(Boolean, default=False)
    default_ticket_status = Column(String(24), nullable=True)   # tickets page default status filter
    default_ticket_priority = Column(String(4), nullable=True)  # tickets page default priority filter
    oidc_sub = Column(String(128), nullable=True, index=True)  # Pocket ID subject
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_login = Column(DateTime, nullable=True)
    # Compliance controls (2026-08-25): TOTP second factor + login lockout.
    otp_secret = Column(String(64), nullable=True)   # TOTP secret (base32)
    otp_verified = Column(Boolean, default=False)    # True once the user confirmed a code
    failed_logins = Column(Integer, default=0)       # consecutive bad-password attempts
    locked_until = Column(DateTime, nullable=True)   # lockout window end (session policy)
    # Token version (P0 revocation batch 2026-08-25): every access/refresh JWT
    # carries `ver`; bumping this column invalidates ALL outstanding tokens for
    # the user immediately (password change / force-logout).
    token_version = Column(Integer, default=0)

    tickets = relationship("Ticket", back_populates="submitter")


class AuthSession(Base):
    """Revocable refresh-token session (P0 batch 2026-08-25).

    Login/register/OIDC mint a refresh JWT whose `jti` is recorded here;
    /refresh validates the row (exists, not revoked, not expired) before
    issuing a new access token; /logout marks the row revoked — the refresh
    token dies instantly instead of living out its 7-day expiry.
    """

    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    jti = Column(String(64), unique=True, index=True, nullable=False)  # refresh-token id
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    ip = Column(String(64), nullable=True)
    user_agent = Column(String(256), nullable=True)


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), index=True, nullable=False)
    hostname = Column(String(256), nullable=True)
    ip_address = Column(String(45), nullable=False)
    device_type = Column(String(32), default="unknown")  # server | switch | ap | router | camera | iot | other (+ legacy gateway|workstation|printer|nas)
    # Explicit control-channel declarations (device_adoption_model.md §5/§8).
    # Auto-derived channels (ssh/snmp/unifi/agent/monitor) come from the
    # credential/adoption columns; this JSON holds channels with no dedicated
    # column (vendor_api) or explicit overrides (e.g. ["monitor"]).
    channels = Column(JSON, default=list)
    vendor = Column(String(64), nullable=True)
    model = Column(String(64), nullable=True)
    mac_address = Column(String(17), nullable=True)
    status = Column(String(16), default="pending")  # online | offline | warning | pending | unreachable | unclaimed
    claimed = Column(Boolean, default=True)  # False = discovered but not configured
    unifi_managed = Column(Boolean, default=False)  # True = status synced from UniFi controller
    notify_state_changes = Column(Boolean, default=False)  # email down/recovery alerts for this device
    site_id = Column(Integer, default=1)
    device_group = Column(String(64), default="default", index=True)  # Pocket ID group gate
    tags = Column(JSON, default=list)
    snmp_community = Column(String(128), nullable=True)  # encrypted at app level
    ssh_user = Column(String(64), nullable=True)
    ssh_key_fingerprint = Column(String(64), nullable=True)
    last_seen = Column(DateTime, nullable=True)
    last_poll_data = Column(JSON, nullable=True)  # cached SNMP/ping results
    fingerprint = Column(JSON, nullable=True)  # nmap fingerprint (ports/vendor/os guess)
    # Device adoption via step-ca certificates (Phase F)
    adoption_method = Column(String(16), default="none")  # none | unifi | ssh | cert | manual
    cert_cn = Column(String(128), nullable=True)      # certificate CN (device identity)
    cert_serial = Column(String(64), nullable=True)   # last certificate serial
    cert_enrolled_at = Column(DateTime, nullable=True)
    cert_last_seen = Column(DateTime, nullable=True)
    # NOC_Agent self-report (P1a) — set when an endpoint agent reports in
    agent_version = Column(String(64), nullable=True)      # e.g. "0.1.0-p1a"
    agent_capabilities = Column(Text, nullable=True)       # JSON array of capability names
    facts_json = Column(Text, nullable=True)               # last-reported host facts as JSON
    adoption_status = Column(String(16), default="none")  # none | enrolling | linked | revoked
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # tenant who adopted it (tenant view = own devices only)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(String(32), unique=True, index=True, nullable=False)  # TKT-YYYYMMDD-NNN
    title = Column(String(256), nullable=False)
    description = Column(Text, nullable=True)
    priority = Column(String(4), default="P3")  # P1 | P2 | P3 | P4
    status = Column(String(24), default="open")  # open | in_progress | awaiting_approval | completed | failed | escalated | closed
    source = Column(String(16), default="manual")  # manual | auto | escalation
    submitter_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    assigned_to = Column(String(64), nullable=True)  # username or "system"
    target_device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    action = Column(String(32), nullable=True)  # from AllowedAction enum
    llm_confidence = Column(Float, nullable=True)
    llm_model = Column(String(64), nullable=True)
    llm_prompt_tokens = Column(Integer, nullable=True)
    llm_response_tokens = Column(Integer, nullable=True)
    llm_cost_usd = Column(Float, nullable=True)
    job_file_path = Column(String(256), nullable=True)
    resolution = Column(Text, nullable=True)
    work_notes = Column(Text, default="[]")  # JSON array of {timestamp, event, detail}
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    submitter = relationship("User", back_populates="tickets")
    target_device = relationship("Device")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(32), unique=True, nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    event_type = Column(String(32), nullable=False)  # ticket_created | llm_request | job_executed | login | escalation
    ticket_id = Column(String(32), nullable=True)
    actor = Column(String(64), nullable=False)
    data = Column(JSON, nullable=False)
    previous_hash = Column(String(64), nullable=True)
    sha256_hash = Column(String(64), nullable=False)


class DeviceJob(Base):
    """A job the appliance enqueues for an endpoint agent to pull + execute
    (design §5). RLS-equivalent scoping by CN: a device can only see/complete
    jobs whose device_id resolves from its own cert CN. The wire job_id is
    str(id); nonce is the replay/idempotency key."""

    __tablename__ = "device_jobs"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), index=True, nullable=False)
    action = Column(String(64), nullable=False)
    params = Column(JSON, default=dict)
    nonce = Column(String(64), nullable=False)
    status = Column(String(16), default="pending", index=True)  # pending | running | done
    result_json = Column(JSON, nullable=True)
    deadline = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ChatMessage(Base):
    """Tech-to-tech internal chat (AIM-style buddy messaging)."""

    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    from_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    to_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    sender = relationship("User", foreign_keys=[from_user_id])
    recipient = relationship("User", foreign_keys=[to_user_id])


class LinkEpisode(Base):
    """In-flight link-stability episode — one row per (device_id, interface).

    The ticket is the user-facing record; this table is the link monitor's
    state-machine memory (state, flap count/timestamps, outage timer, current
    ticket) so a container restart resumes an open episode instead of losing
    it. Rows are deleted when the episode auto-closes.
    """

    __tablename__ = "link_episodes"
    __table_args__ = (
        UniqueConstraint("device_id", "interface", name="uq_link_episodes_dev_iface"),
    )

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), index=True, nullable=False)
    interface = Column(String(128), nullable=False)  # "wan" | port name | SNMP ifDescr | "status"
    state = Column(String(16), default="flapping")   # flapping | outage
    flap_count = Column(Integer, default=0)          # total down->up recoveries
    flap_timestamps = Column(JSON, default=list)     # ISO timestamps of each recovery
    window_start = Column(DateTime, default=datetime.datetime.utcnow)
    down_since = Column(DateTime, nullable=True)     # when the current down began
    last_event_at = Column(DateTime, default=datetime.datetime.utcnow)
    escalated = Column(String(4), default="P2")      # current ticket priority
    escalation_reason = Column(String(32), nullable=True)  # recurrence | outage | wan_probe
    ticket_id = Column(String(32), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class StarlinkEpisode(Base):
    """In-flight Starlink link-health episode — one row per dish device.

    The ticket is the user-facing record; this table is the Starlink health
    monitor's state-machine memory (state, degradation/outage/recovery timers,
    current ticket) so a container restart resumes an open episode instead of
    losing it. Rows are deleted when the episode auto-closes.
    """

    __tablename__ = "starlink_episodes"
    __table_args__ = (
        UniqueConstraint("device_id", name="uq_starlink_episodes_device"),
    )

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), index=True, nullable=False)
    state = Column(String(16), default="healthy")     # healthy | degraded | outage
    degraded_since = Column(DateTime, nullable=True)   # when sustained degradation began
    outage_since = Column(DateTime, nullable=True)     # when the link-down window began
    recovered_since = Column(DateTime, nullable=True)  # start of the current healthy streak
    last_event_at = Column(DateTime, default=datetime.datetime.utcnow)
    escalated = Column(String(4), nullable=True)       # current ticket priority (P2 | P1)
    escalation_reason = Column(String(32), nullable=True)  # degraded | outage
    ticket_id = Column(String(32), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ScanRun(Base):
    """One Network Optimization scan (P1: read-only audit/report).

    Future-proofed per the user-approved data model (2026-08-18):
      - ``scope`` is the JSON snapshot of what was scanned (device ids + the
        excluded self-protection list + cost knobs in effect).
      - ``summary`` is STRUCTURED JSON (category scores + counts) that a later
        phase can extend with an LLM executive summary in the same slot.
      - ``schema_version`` pins the findings/evidence schema for the future
        trend/diff + SOC-appliance versioned-JSON export.
    """

    __tablename__ = "scan_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    scope = Column(JSON, default=dict)      # {devices: [...], excluded: [...], knobs: {...}}
    status = Column(String(16), default="queued", index=True)  # queued|running|completed|failed|cancelled
    score = Column(Float, nullable=True)    # overall 0-100
    summary = Column(Text, nullable=True)   # JSON: category scores + counts (+ LLM prose later)
    schema_version = Column(Integer, default=1)
    triggered_by = Column(String(16), default="manual")  # manual | schedule
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Metric(Base):
    """One time-series sample (telemetry backbone, P0).

    A single (device, metric, timestamp) -> value row. Values are always
    NUMERIC (a rate, a percentage, a latency, a 0/1 status). Bandwidth is
    stored as a RATE (bytes/sec) computed by the collector from consecutive
    counter reads — the store never needs to know the channel's raw units.

    Write-friendly by design: collectors batch-insert rows and the table is
    indexed for the range queries the trends API performs
    (device_id, metric, ts). Retention pruning deletes whole rows by ts — no
    per-metric bookkeeping.
    """

    __tablename__ = "metrics"
    __table_args__ = (
        Index("ix_metrics_device_metric_ts", "device_id", "metric", "ts"),
    )

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), index=True, nullable=False)
    metric = Column(String(128), index=True, nullable=False)   # e.g. "ping.latency_ms"
    ts = Column(DateTime, index=True, nullable=False)          # naive UTC sample time
    value = Column(Float, nullable=False)


class Finding(Base):
    """One deterministic rule-based finding from a scan.

    ``finding_key`` is a STABLE identifier (e.g. "perf.duplex_half") so later
    phases can diff runs ("what changed since last scan") and link findings to
    fixes without fragile title matching. ``evidence`` is the raw JSON the rule
    produced — structured + exportable, not prose.
    """

    __tablename__ = "findings"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("scan_runs.id"), index=True, nullable=False)
    finding_key = Column(String(64), index=True, nullable=False)
    category = Column(String(16), index=True, nullable=False)   # performance|security|reliability|hygiene
    severity = Column(String(12), index=True, nullable=False)   # critical|warning|info
    fix_ticket_id = Column(String(32), nullable=True, index=True)  # admin fix ticket (optimize → ticket linkage, 08-19)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    interface = Column(String(128), nullable=True)
    title = Column(String(256), nullable=False)
    detail = Column(Text, nullable=True)
    evidence = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MaintenanceWindow(Base):
    """A low-impact LOCAL-time window during which scheduled firmware upgrades
    may run. Reusable by other scheduled ops (same shape as the updates-schedule-v2
    / netopt schedules): recurring (day/hour) or one-time (local ``when``), plus
    a duration. ``timezone`` is a snapshot of TZ at creation for display; the
    engine always evaluates in the appliance's current TZ.
    """

    __tablename__ = "maintenance_windows"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    mode = Column(String(16), default="recurring")   # recurring | onetime
    day = Column(String(16), default="daily")        # recurring: daily | 0-6 (0=Sunday)
    hour = Column(Integer, default=3)                # recurring: 0-23 LOCAL
    duration_minutes = Column(Integer, default=60)
    when = Column(String(32), default="")            # onetime: local 'YYYY-MM-DDTHH:MM'
    enabled = Column(Boolean, default=True)
    timezone = Column(String(64), default="")
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class DeviceFirmware(Base):
    """Per-managed-device firmware state (UniFi-managed gear only in v1).

    Keyed by MAC (the controller's identity) with an optional device_id link to
    the appliance inventory. ``prestaged_version`` records a firmware the device
    has already DOWNLOADED (pre-staged) but not yet applied — the upgrade engine
    skips the cache step when it matches the target.
    """

    __tablename__ = "device_firmware"
    __table_args__ = (UniqueConstraint("mac_address", name="uq_device_firmware_mac"),)

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=True, index=True)
    mac_address = Column(String(17), nullable=False, index=True)
    name = Column(String(128), nullable=True)
    device_type = Column(String(32), default="unknown")   # gateway | switch | ap
    model = Column(String(64), nullable=True)
    ip = Column(String(45), nullable=True)
    current_version = Column(String(64), default="")
    previous_version = Column(String(64), default="")
    available_version = Column(String(64), default="")
    upgradeable = Column(Boolean, default=False)
    online = Column(Boolean, default=False)
    prestaged_version = Column(String(64), default="")
    last_result = Column(String(16), default="")       # success | failed | rolled_back | skipped
    last_upgrade_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class FirmwareUpgrade(Base):
    """One firmware upgrade attempt/run record — history log AND in-flight
    state machine memory (a container restart resumes a staged/upgrading/
    verifying/rolling_back row instead of losing it).

    ``durations`` is a JSON dict of stage -> seconds (plus 'total').
    """

    __tablename__ = "firmware_upgrades"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=True, index=True)
    mac_address = Column(String(17), nullable=False, index=True)
    device_name = Column(String(128), nullable=True)
    device_type = Column(String(32), default="unknown")
    from_version = Column(String(64), default="")
    to_version = Column(String(64), default="")
    window_id = Column(Integer, ForeignKey("maintenance_windows.id"), nullable=True)
    status = Column(String(16), default="staging", index=True)
    # staging | upgrading | verifying | rolling_back | success | rolled_back | failed | cancelled
    stage_started_at = Column(DateTime, nullable=True)
    stage_deadline = Column(DateTime, nullable=True)
    verify_attempts = Column(Integer, default=0)
    rollback_attempted = Column(Boolean, default=False)
    durations = Column(JSON, default=dict)
    error = Column(Text, nullable=True)
    triggered_by = Column(String(16), default="auto")   # auto | approval | manual
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class PendingAction(Base):
    """Approvals + escalations queue — persisted actionable items with role
    visibility. This is the DATA the roles-and-chat-context worker consumes
    (Juniper surfaces it in chat; that worker owns presentation, we own data + API).

    Visibility: admin sees all; the technician tier (operator today, a real
    technician role later) sees non-admin items only when
    FIRMWARE_TECH_VISIBILITY is on; gateway approvals are admin-only regardless
    (required_role="admin").
    """

    __tablename__ = "pending_actions"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(16), default="approval", index=True)   # approval | escalation
    title = Column(String(256), nullable=False)
    detail = Column(Text, nullable=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=True, index=True)
    mac_address = Column(String(17), nullable=True)
    device_name = Column(String(128), nullable=True)
    device_type = Column(String(32), default="unknown")
    firmware_from = Column(String(64), default="")
    firmware_to = Column(String(64), default="")
    status = Column(String(16), default="pending", index=True)
    # pending | approved | deferred | resolved
    auto = Column(Boolean, default=False)          # auto-approved (non-blocking notice)
    required_role = Column(String(16), default="technician")   # minimum role to act
    resolved_by = Column(String(64), nullable=True)
    resolved_note = Column(Text, nullable=True)
    extra = Column(JSON, default=dict)            # runbook, severity, window, etc.
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
