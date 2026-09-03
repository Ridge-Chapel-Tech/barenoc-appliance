import hashlib
import json
import os
import datetime
from pydantic import BaseModel, Field, field_serializer, field_validator
from typing import Optional
from datetime import datetime


# ── Auth Schemas ──

class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None  # required when MFA enforcement is on (admin/operator tier)


class RegisterRequest(BaseModel):
    """Self-registration (first login = admin; everyone after = user)."""
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    email: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    role: str
    is_active: bool
    must_change_password: bool = False

    class Config:
        from_attributes = True


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ── Ticket Schemas ──

class TicketCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: str = "P3"
    target_device_id: Optional[int] = None
    web_research: Optional[bool] = False   # L3 opt-in: allow Lily web fetch/search


class TicketUpdate(BaseModel):
    status: Optional[str] = None
    resolution: Optional[str] = None
    assigned_to: Optional[str] = None
    priority: Optional[str] = None
    web_research: Optional[bool] = None


class TicketResponse(BaseModel):
    id: int
    ticket_id: str
    title: str
    description: Optional[str] = None
    priority: str
    status: str
    source: str
    submitter_id: Optional[int] = None
    assigned_to: Optional[str] = None
    target_device_id: Optional[int] = None
    web_research: Optional[bool] = None
    action: Optional[str] = None
    llm_confidence: Optional[float] = None
    llm_model: Optional[str] = None
    llm_cost_usd: Optional[float] = None
    llm_cost_estimate: Optional[bool] = None
    resolution: Optional[str] = None
    work_notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None

    @field_validator("assigned_to", mode="before")
    @classmethod
    def _display_assignee(cls, v):
        """Map internal assignee names to display names. Stored values stay
        internal (worker logic uses them); only the API response is cosmetic.
        The agent's display name is the configured assistant (BOT_ASSISTANT_NAME,
        default "Lily") so the UI never shows the pi-agent service account."""
        if v in ("pi-agent", "ai-tech"):
            name = ""
            try:
                with open("/opt/barenoc/.env") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("BOT_ASSISTANT_NAME="):
                            name = line.partition("=")[2].strip()
                            break
            except Exception:
                pass
            return name or os.getenv("BOT_ASSISTANT_NAME") or "Lily"
        return {"human-tech": "Human tech", "customer": "Customer",
                "system": "System"}.get(v, v)

    class Config:
        from_attributes = True

    @field_serializer("created_at", "updated_at", "resolved_at")
    def serialize_datetime(self, dt: datetime, _info) -> str:
        if dt is None:
            return None
        return dt.isoformat()

    @field_serializer("work_notes")
    def serialize_work_notes(self, value, _info) -> str:
        """Always emit work_notes as a JSON array string.

        A corrupted (double-encoded) field (#102) stored a bare JSON string,
        which the chat/ticket UIs then iterated as individual characters —
        blocking ticket readability. Normalize here so every API response
        self-heals the field for clients without mutating the DB row."""
        from worknotes import parse_notes
        return json.dumps(parse_notes(value))


# ── Device Schemas ──

class DeviceCreate(BaseModel):
    name: str
    ip_address: str
    hostname: Optional[str] = None
    device_type: str = "unknown"
    vendor: Optional[str] = None
    model: Optional[str] = None
    mac_address: Optional[str] = None
    tags: list = []
    claimed: Optional[bool] = True
    device_group: Optional[str] = "default"
    snmp_community: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_key: Optional[str] = None
    channels: list = []
    windows_health_schedule: Optional[dict] = None


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    device_type: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    tags: Optional[list] = None
    status: Optional[str] = None
    claimed: Optional[bool] = None
    device_group: Optional[str] = None
    notify_state_changes: Optional[bool] = None
    last_poll_data: Optional[dict] = None
    last_seen: Optional[datetime] = None
    fingerprint: Optional[dict] = None
    snmp_community: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_key: Optional[str] = None
    channels: Optional[list] = None
    windows_health_schedule: Optional[dict] = None


class DeviceResponse(BaseModel):
    id: int
    name: str
    hostname: Optional[str] = None
    ip_address: str
    device_type: str
    vendor: Optional[str] = None
    model: Optional[str] = None
    mac_address: Optional[str] = None
    status: str
    claimed: bool = True
    device_group: str = "default"
    notify_state_changes: bool = False
    tags: list
    last_seen: Optional[datetime] = None
    last_poll_data: Optional[dict] = None
    fingerprint: Optional[dict] = None
    snmp_configured: bool = False
    ssh_configured: bool = False
    unifi_managed: bool = False
    channels: Optional[list] = None
    adoption_status: Optional[str] = "none"
    adoption_method: Optional[str] = "none"
    cert_cn: Optional[str] = None
    windows_health_schedule: Optional[dict] = None
    created_at: datetime

    class Config:
        from_attributes = True

    @field_serializer("last_seen", "created_at")
    def serialize_dt(self, dt: datetime, _info) -> str:
        if dt is None:
            return None
        return dt.isoformat()


# ── Dashboard Schemas ──

class DashboardStats(BaseModel):
    total_devices: int
    online_devices: int
    offline_devices: int
    warning_devices: int
    total_tickets: int
    open_tickets: int
    in_progress_tickets: int
    p1_tickets: int
    p2_tickets: int
    recent_tickets: list
    system_health: str
    customer_name: str = ""
    has_logo: bool = False


# ── Audit Schemas ──

class AuditLogResponse(BaseModel):
    event_id: str
    timestamp: str
    event_type: str
    ticket_id: Optional[str] = None
    actor: str
    data: dict
    sha256_hash: str

    class Config:
        from_attributes = True


# ── Helpers ──

def generate_ticket_id() -> str:
    now = datetime.utcnow()
    date_part = now.strftime("%Y%m%d")
    seq = int(now.timestamp() * 1000) % 10000  # milliseconds as seq
    return f"TKT-{date_part}-{seq:04d}"


def generate_event_id() -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    return f"evt_{ts}"


def compute_hash(data: dict, previous_hash: Optional[str] = None) -> str:
    raw = json.dumps(data, sort_keys=True) + (previous_hash or "0")
    return hashlib.sha256(raw.encode()).hexdigest()
