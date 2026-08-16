import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, ForeignKey, JSON
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    email = Column(String(128), unique=True, nullable=True)
    display_name = Column(String(128), nullable=True)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(16), default="admin")  # admin | operator | readonly | tenant
    is_active = Column(Boolean, default=True)
    is_bot = Column(Boolean, default=False)  # True for bot users (Juniper Queue Manager) — chat participants, not humans
    must_change_password = Column(Boolean, default=False)
    default_ticket_status = Column(String(24), nullable=True)   # tickets page default status filter
    default_ticket_priority = Column(String(4), nullable=True)  # tickets page default priority filter
    oidc_sub = Column(String(128), nullable=True, index=True)  # Pocket ID subject
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

    tickets = relationship("Ticket", back_populates="submitter")


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), index=True, nullable=False)
    hostname = Column(String(256), nullable=True)
    ip_address = Column(String(45), nullable=False)
    device_type = Column(String(32), default="unknown")  # gateway | switch | ap | server | workstation
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
