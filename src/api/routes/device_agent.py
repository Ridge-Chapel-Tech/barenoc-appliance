"""Device-facing job transport for NOC_Agent (design §5).

nginx terminates TLS for /api/v1/device/* and REQUIRES a valid BareNOC CA
client certificate (passed via X-SSL-Client-DN). The CN resolves to a device
record; jobs are scoped to that device only (RLS-equivalent: a device can pull
and complete ONLY its own jobs).

Endpoints:
  POST /api/v1/device/jobs/pull   — fetch up to N pending jobs (atomic claim)
  POST /api/v1/device/jobs/result — report a job outcome (dedupe by job_id+nonce)

No inbound SSH, no stored credentials: identity is the device cert.
"""

import datetime
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Device, DeviceJob
from routes.device_certs import _client_cn

router = APIRouter(prefix="/api/v1/device", tags=["device"])

# The P1b action set (design §6). The appliance validates here (allowlist);
# the agent re-validates against its own embedded catalog — neither side alone
# can widen the other. apply_updates is the gated OS apply (customer-requested
# only, confirm-gated on BOTH sides — it never runs autonomous-unprompted).
AGENT_ACTIONS = {"collect_logs", "reboot", "check_updates", "apply_updates",
                "report_facts"}

_PULL_DEFAULT = 10
_PULL_MAX = 50


def _device_for_cn(db: Session, cn: str) -> "Device | None":
    """Resolve the device record for a cert CN (link order mirrors
    device_certs.device_report). Returns None when the device is unknown."""
    device = db.query(Device).filter(Device.cert_cn == cn).first()
    if device:
        return device
    name = cn[len("device-"):]
    return (db.query(Device)
            .filter(Device.name == name)
            .order_by(Device.id.desc()).first())


def _iso(dt: "datetime.datetime | None") -> "str | None":
    """Serialize a naive-UTC datetime as RFC3339 (the agent parses RFC3339)."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _validate_enqueue(action: str, params: dict) -> None:
    """Appliance-side allowlist + param safety for enqueued agent jobs."""
    if action not in AGENT_ACTIONS:
        raise ValueError(
            f"unknown agent action {action!r} (P1b set: {sorted(AGENT_ACTIONS)})")
    if action == "reboot" and not (params or {}).get("confirm"):
        raise ValueError("reboot requires params.confirm=true")
    if action == "apply_updates" and not (params or {}).get("confirm"):
        # Apply writes to the endpoint OS: the ticket's explicit request is
        # required. Never enqueue autonomously (mirrors the reboot gate).
        raise ValueError("apply_updates requires params.confirm=true")


def enqueue_job(db: Session, device_id: int, action: str, params: dict = None,
                deadline: "datetime.datetime | None" = None,
                ttl_seconds: "int | None" = None) -> DeviceJob:
    """Enqueue a job for an endpoint agent. This is the appliance's push path:
    callers (scheduled checks, the Lily/pi workflow, the UI) create a DeviceJob
    row; the agent pulls it on its next poll. Returns the persisted job."""
    _validate_enqueue(action, params or {})
    nonce = secrets.token_hex(32)
    if deadline is None and ttl_seconds:
        deadline = datetime.datetime.utcnow() + datetime.timedelta(seconds=ttl_seconds)
    job = DeviceJob(device_id=device_id, action=action, params=params or {},
                    nonce=nonce, status="pending", deadline=deadline)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _serialize_job(job: DeviceJob) -> dict:
    return {
        "job_id": str(job.id),
        "action": job.action,
        "params": job.params or {},
        "deadline": _iso(job.deadline),
        "nonce": job.nonce,
    }


class PullRequest(BaseModel):
    limit: Optional[int] = _PULL_DEFAULT


@router.post("/jobs/pull")
async def jobs_pull(body: PullRequest, request: Request,
                    db: Session = Depends(get_db)):
    """Return up to N pending jobs for the cert CN, claiming them atomically
    (status pending→running inside one transaction, so a re-pull or a second
    poller never sees the same job)."""
    cn = _client_cn(request)
    device = _device_for_cn(db, cn)
    if device is None:
        # A valid cert for an as-yet-unlinked CN has no jobs to claim.
        return {"ok": True, "jobs": [], "claimed": 0}
    if device.adoption_status == "revoked":
        raise HTTPException(status_code=403, detail="device adoption revoked")

    limit = max(1, min(int(body.limit or _PULL_DEFAULT), _PULL_MAX))
    now = datetime.datetime.utcnow()

    # Claim: select pending jobs for THIS device, flip them to running, commit.
    # SQLite serializes write transactions, so this select+update commit is
    # atomic against concurrent pulls; the device_id scope is the RLS
    # equivalent (no other device can ever claim these rows).
    jobs = (db.query(DeviceJob)
            .filter(DeviceJob.device_id == device.id,
                    DeviceJob.status == "pending")
            .order_by(DeviceJob.created_at.asc(), DeviceJob.id.asc())
            .limit(limit).all())
    for job in jobs:
        job.status = "running"

    device.cert_last_seen = now
    device.last_seen = now
    device.status = "online"
    db.commit()

    return {"ok": True, "jobs": [_serialize_job(j) for j in jobs],
            "claimed": len(jobs)}


class ResultRequest(BaseModel):
    job_id: str
    nonce: str
    ok: bool = False
    output: Optional[Any] = None
    duration_ms: Optional[int] = None
    exit_code: Optional[int] = None


@router.post("/jobs/result")
async def jobs_result(body: ResultRequest, request: Request,
                      db: Session = Depends(get_db)):
    """Report a job outcome. Dedupes by (job_id, nonce); stores the result;
    refreshes the device (status/last_seen); audits via log_event. NEVER 404s
    on an unknown job_id (the runner callback pattern)."""
    from audit import log_event

    cn = _client_cn(request)
    device = _device_for_cn(db, cn)
    now = datetime.datetime.utcnow()

    job = None
    try:
        job_id = int(body.job_id)
    except (TypeError, ValueError):
        job_id = None
    if job_id is not None:
        job = db.query(DeviceJob).filter(DeviceJob.id == job_id).first()

    # Unknown job_id → ok (never 404).
    if job is None:
        return {"ok": True, "no_such_job": True}

    # RLS-equivalent scoping: a device may only complete its OWN jobs. Don't
    # leak anything to a device poking at another device's job ids.
    if device is None or job.device_id != device.id:
        log_event(db, "device_agent_result_rejected", cn, {
            "job_id": body.job_id, "reason": "job not scoped to this device"})
        return {"ok": True, "ignored": True}

    if device.adoption_status == "revoked":
        raise HTTPException(status_code=403, detail="device adoption revoked")

    # Nonce is the replay/idempotency key (design §5).
    if job.nonce != body.nonce:
        log_event(db, "device_agent_result_rejected", cn, {
            "job_id": body.job_id, "reason": "nonce mismatch"})
        return {"ok": True, "ignored": True, "reason": "nonce mismatch"}

    if job.status == "done":
        return {"ok": True, "deduplicated": True, "status": "done"}

    job.result_json = {
        "ok": body.ok,
        "output": body.output,
        "duration_ms": body.duration_ms,
        "exit_code": body.exit_code,
    }
    job.status = "done"
    device.cert_last_seen = now
    device.last_seen = now
    device.status = "online"
    db.commit()

    log_event(db, "device_agent_job_result", cn, {
        "job_id": str(job.id),
        "action": job.action,
        "ok": body.ok,
        "duration_ms": body.duration_ms,
        "exit_code": body.exit_code,
        "device_id": device.id,
        "device": device.name,
    })
    return {"ok": True, "status": "done", "job_id": str(job.id)}
