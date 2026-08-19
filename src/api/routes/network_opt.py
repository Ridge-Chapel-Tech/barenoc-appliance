"""Network Optimization — admin-only API (P1 read-only audit/report).

Endpoints (admin-only, except the scheduler-facing schedule-read + run-trigger
which also allow the internal `agent` identity — same pattern as updates):
  GET    /api/v1/netopt/status            overall state + knobs + schedule + latest
  GET    /api/v1/netopt/runs              run history
  GET    /api/v1/netopt/runs/{id}         run detail + findings
  POST   /api/v1/netopt/runs              start a scan (one-time)
  POST   /api/v1/netopt/runs/{id}/cancel  cancel a running scan
  GET    /api/v1/netopt/schedule          schedule conf
  POST   /api/v1/netopt/schedule          set schedule (recurring | onetime, LOCAL time)
  POST   /api/v1/netopt/schedule/cancel   cancel schedule
  GET    /api/v1/netopt/limits            cost knobs + scope preview
  POST   /api/v1/netopt/limits            update cost knobs (.env)

Scheduling reuses the updates-schedule-v2 local-time pattern (recurring
day/hour or one-time local datetime); the SCHEDULER calls POST /runs when due.
"""

import datetime
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import require_role, require_any_role
from database import get_db
from models import User, ScanRun, Finding
from routes.updates import _local_now, _parse_local_dt, _appliance_tz
import network_opt
from network_opt_rules import SCHEMA_VERSION, CATEGORIES, SEVERITIES, fixability
from netopt_tickets import spawn_optimize_tickets, PER_ITEM_CAP

router = APIRouter(prefix="/api/v1/netopt", tags=["network-optimization"])

STATUS_DIR = "/opt/barenoc/volumes/update_status"
SCHEDULE_FILE = os.path.join(STATUS_DIR, "netopt_schedule.conf")


# ── schedule conf (mirrors updates-schedule-v2) ────────────────────────────

def _default_schedule() -> dict:
    d = dict(network_opt.netopt_config()["default_schedule"])
    d.setdefault("when", "")
    d.setdefault("fired", "")
    d.setdefault("timezone", _appliance_tz())
    return d


def _read_schedule() -> dict:
    conf = _default_schedule()
    try:
        with open(SCHEDULE_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                conf[k.strip()] = v.strip()
    except Exception:
        pass
    try:
        conf["enabled"] = conf.get("enabled") in ("true", "1", "yes") \
            if isinstance(conf.get("enabled"), str) else bool(conf.get("enabled"))
        conf["hour"] = int(conf.get("hour", 3))
    except Exception:
        pass
    if conf.get("mode") not in ("recurring", "onetime"):
        conf["mode"] = "recurring"
    conf["timezone"] = _appliance_tz()
    return conf


def _write_schedule(conf: dict):
    mode = conf.get("mode") or "recurring"
    enabled = conf.get("enabled")
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in ("true", "1", "yes")
    else:
        enabled = bool(enabled)
    day = str(conf.get("day") or "0")
    try:
        hour = int(conf.get("hour", 3))
    except (TypeError, ValueError):
        hour = 3
    when = (conf.get("when") or "").strip()
    fired = (conf.get("fired") or "").strip()
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(SCHEDULE_FILE, "w") as f:
            f.write("# BareNOC Network Optimization schedule (admin; scheduler applies it)\n")
            f.write(f"mode={mode}\n")
            f.write(f"enabled={'true' if enabled else 'false'}\n")
            f.write(f"day={day}\n")
            f.write(f"hour={hour}\n")
            f.write(f"when={when}\n")
            f.write(f"fired={fired}\n")
    except Exception as e:
        raise HTTPException(500, f"could not save the schedule: {e}")


class ScheduleBody(BaseModel):
    enabled: bool
    mode: str = "recurring"   # recurring | onetime
    day: str = "0"            # recurring: "daily" | 0-6 (0=Sunday)
    hour: int = 3             # recurring: 0-23 LOCAL time
    when: str = ""            # onetime: local "YYYY-MM-DDTHH:MM"


# ── limits (.env knobs) ─────────────────────────────────────────────────────

def _write_netopt_env(updates: dict):
    """Write NETOPT_* knobs to /opt/barenoc/.env (in-place, preserve comments)."""
    path = "/opt/barenoc/.env"
    try:
        with open(path) as f:
            lines = f.readlines()
    except Exception:
        lines = []
    for key, value in updates.items():
        new_lines, found = [], False
        for line in lines:
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}\n")
        lines = new_lines
    with open(path, "w") as f:
        f.writelines(lines)


def _scope_preview(db: Session, config: dict) -> dict:
    scope = network_opt.build_scope(db, config)
    return {
        "included": [{"id": d.id, "name": d.name, "ip": d.ip_address,
                      "type": d.device_type} for d in scope["devices"]],
        "excluded": scope["excluded"],
        "count": len(scope["devices"]),
        "max_hosts": scope["max_hosts"],
    }


# ── endpoints ──────────────────────────────────────────────────────────────

@router.get("/status")
def netopt_status(db: Session = Depends(get_db),
                  user: User = Depends(require_role("admin"))):
    config = network_opt.netopt_config()
    active = None
    running = db.query(ScanRun).filter(
        ScanRun.status.in_(("running", "queued"))).order_by(
        ScanRun.id.desc()).first()
    if running:
        active = {"run_id": running.id, "status": running.status,
                  "progress": network_opt.run_progress(running.id)}
    latest = db.query(ScanRun).order_by(ScanRun.id.desc()).first()
    latest_summary = None
    if latest:
        latest_summary = {
            "run_id": latest.id,
            "status": latest.status,
            "score": latest.score,
            "started_at": latest.started_at.isoformat() if latest.started_at else None,
            "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
            "triggered_by": latest.triggered_by,
            "total_findings": db.query(Finding).filter(Finding.run_id == latest.id).count(),
        }
    return {
        "enabled": config["enabled"],
        "knobs": {"max_hosts": config["max_hosts"], "profile": config["profile"],
                  "concurrency": config["concurrency"],
                  "default_schedule": config["default_schedule"]},
        "schedule": _read_schedule(),
        "active": active,
        "latest": latest_summary,
        "schema_version": SCHEMA_VERSION,
    }


@router.get("/runs")
def list_runs(limit: int = Query(50, ge=1, le=200),
              db: Session = Depends(get_db),
              user: User = Depends(require_role("admin"))):
    runs = db.query(ScanRun).order_by(ScanRun.id.desc()).limit(limit).all()
    out = []
    for r in runs:
        out.append({
            "run_id": r.id,
            "status": r.status,
            "score": r.score,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "triggered_by": r.triggered_by,
            "total_findings": db.query(Finding).filter(Finding.run_id == r.id).count(),
        })
    return {"runs": out, "total": len(out)}


@router.get("/runs/{run_id}")
def run_detail(run_id: int, db: Session = Depends(get_db),
               user: User = Depends(require_role("admin"))):
    run = db.query(ScanRun).filter(ScanRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "scan run not found")
    findings = db.query(Finding).filter(Finding.run_id == run_id).order_by(
        Finding.severity, Finding.category, Finding.finding_key).all()
    summary = {}
    try:
        summary = json.loads(run.summary or "{}")
    except (ValueError, TypeError):
        pass
    return {
        "run_id": run.id,
        "status": run.status,
        "score": run.score,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "triggered_by": run.triggered_by,
        "schema_version": run.schema_version,
        "scope": run.scope or {},
        "summary": summary,
        "findings": [{
            "id": f.id,
            "finding_key": f.finding_key,
            "category": f.category,
            "severity": f.severity,
            "device_id": f.device_id,
            "interface": f.interface,
            "title": f.title,
            "detail": f.detail,
            "evidence": f.evidence or {},
            "fixable": fixability(f.finding_key)["fixable"],
            "suggested_action": fixability(f.finding_key)["suggested_action"],
            "high_risk": fixability(f.finding_key)["high_risk"],
            "fix_ticket_id": f.fix_ticket_id,
        } for f in findings],
    }


@router.post("/runs", status_code=201)
def start_run(body: dict = None, db: Session = Depends(get_db),
              user: User = Depends(require_any_role("admin", "agent"))):
    # The scheduler (agent identity) triggers scheduled runs; the admin triggers
    # manual runs from the UI. Both are internal/trusted — not tenant-facing.
    triggered_by = str((body or {}).get("triggered_by") or "manual")
    if triggered_by not in ("manual", "schedule"):
        triggered_by = "manual"
    config = network_opt.netopt_config()
    if not config["enabled"]:
        raise HTTPException(400, "Network Optimization is disabled (NETOPT_ENABLED=false)")
    scope = network_opt.build_scope(db, config)
    if not scope["devices"]:
        raise HTTPException(400, "No network gear in scope — onboard a gateway/switch/AP first")
    run = ScanRun(status="queued", scope={
        "devices": [d.id for d in scope["devices"]],
        "excluded": scope["excluded"],
        "max_hosts": scope["max_hosts"],
    }, triggered_by=triggered_by)
    db.add(run)
    db.commit()
    db.refresh(run)
    network_opt.start_scan(run.id, triggered_by=triggered_by)
    return {"status": "queued", "run_id": run.id,
            "hosts": len(scope["devices"]),
            "note": "Scan started in the background — watch the Network Optimization tab"}


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, db: Session = Depends(get_db),
               user: User = Depends(require_role("admin"))):
    run = db.query(ScanRun).filter(ScanRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "scan run not found")
    if run.status not in ("running", "queued"):
        raise HTTPException(400, f"run is {run.status}, not running")
    network_opt.cancel_run(run_id)
    run.status = "cancelled"
    run.finished_at = datetime.datetime.utcnow()
    db.commit()
    return {"status": "ok", "run_id": run_id, "note": "cancellation requested"}


class OptimizeBody(BaseModel):
    finding_ids: list[int]
    mode: str = "per_item"     # batched | per_item
    comments: dict = {}         # {str(finding_id): text}


@router.post("/runs/{run_id}/optimize")
def optimize_run(run_id: int, body: OptimizeBody, db: Session = Depends(get_db),
                 user: User = Depends(require_role("admin"))):
    """Turn selected findings into admin tickets (per-item or batched).

    The Optimize button creates TICKETS, never direct controller writes — the
    tickets then flow through the normal pipeline (Juniper/Lily + approval
    gates). Validation: admin-only, findings belong to this run, fixable only,
    per-item cap of 10 (past that: use batched).
    """
    run = db.query(ScanRun).filter(ScanRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "scan run not found")

    # Dedupe while preserving order; coerce each id to int.
    ids = []
    for fid in (body.finding_ids or []):
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            raise HTTPException(400, f"invalid finding id: {fid}")
        if fid not in ids:
            ids.append(fid)
    if not ids:
        raise HTTPException(400, "select at least one finding")

    findings = db.query(Finding).filter(
        Finding.run_id == run_id, Finding.id.in_(ids)).all()
    by_id = {f.id: f for f in findings}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise HTTPException(400, f"finding(s) not in this run: {missing}")
    ordered = [by_id[i] for i in ids]

    # Fixable only — non-fixable findings are informational and not actionable.
    nonfixable = [f for f in ordered if not fixability(f.finding_key)["fixable"]]
    if nonfixable:
        names = ", ".join(f"{f.finding_key} (#{f.id})" for f in nonfixable)
        raise HTTPException(400, f"informational — not actionable: {names}")

    mode = str(body.mode or "per_item").strip().lower().replace("-", "_")
    if mode in ("peritem", "item", "individual"):
        mode = "per_item"
    if mode not in ("batched", "per_item"):
        raise HTTPException(400, "mode must be 'batched' or 'per_item'")
    if mode == "per_item" and len(ordered) > PER_ITEM_CAP:
        raise HTTPException(
            400, f"more than {PER_ITEM_CAP} findings for per-item tickets — "
                 "use batched mode instead (or select ≤10 findings)")

    result = spawn_optimize_tickets(
        db, run_id, ordered, mode=mode, comments=body.comments or {},
        submitter_id=user.id)
    db.commit()
    return {"status": "ok", "run_id": run_id, "mode": result["mode"],
            "tickets": result["created"], "count": result["count"]}


@router.get("/schedule")
def get_schedule(user: User = Depends(require_any_role("admin", "agent"))):
    return _read_schedule()


@router.post("/schedule")
def set_schedule(body: ScheduleBody, user: User = Depends(require_role("admin"))):
    mode = (body.mode or "recurring").strip().lower()
    if mode not in ("recurring", "onetime"):
        raise HTTPException(422, "mode must be 'recurring' or 'onetime'")
    day = str(body.day or "0").strip()
    hour = int(body.hour)
    when = (body.when or "").strip()

    if mode == "recurring":
        if hour < 0 or hour > 23:
            raise HTTPException(422, "hour must be 0-23 (local time)")
        if day != "daily":
            try:
                day = int(day)
            except ValueError:
                raise HTTPException(422, "day must be 'daily' or 0-6")
            if day < 0 or day > 6:
                raise HTTPException(422, "day must be 'daily' or 0-6")
            day = str(day)
        when = ""
    else:
        try:
            when_dt = _parse_local_dt(when)
        except Exception:
            raise HTTPException(422, "when must be a local datetime like 'YYYY-MM-DDTHH:MM'")
        if when_dt <= _local_now():
            raise HTTPException(422, "when must be in the future (local time)")
        when = when_dt.strftime("%Y-%m-%dT%H:%M")
        day = "0"

    _write_schedule({"mode": mode, "enabled": bool(body.enabled), "day": day,
                     "hour": hour, "when": when, "fired": ""})
    return {"status": "ok", "schedule": _read_schedule()}


@router.post("/schedule/cancel")
def cancel_schedule(user: User = Depends(require_role("admin"))):
    conf = _read_schedule()
    conf["enabled"] = False
    conf["when"] = ""
    conf["fired"] = ""
    _write_schedule(conf)
    return {"status": "ok", "schedule": _read_schedule()}


@router.post("/schedule/complete")
def complete_schedule(user: User = Depends(require_any_role("admin", "agent"))):
    """Mark a one-time schedule as fired + disable it (persisted). Called by
    the scheduler AFTER it has queued the scan."""
    conf = _read_schedule()
    if conf.get("mode") == "onetime":
        conf["fired"] = datetime.datetime.utcnow().isoformat() + "Z"
        conf["enabled"] = False
        _write_schedule(conf)
    return {"status": "ok", "schedule": _read_schedule()}


@router.get("/limits")
def get_limits(db: Session = Depends(get_db),
               user: User = Depends(require_role("admin"))):
    config = network_opt.netopt_config()
    out = {"enabled": config["enabled"], "max_hosts": config["max_hosts"],
           "profile": config["profile"], "concurrency": config["concurrency"],
           "profiles": sorted(network_opt.PROFILES.keys()),
           "default_schedule": config["default_schedule"]}
    out["scope"] = _scope_preview(db, config)
    return out


@router.post("/limits")
def set_limits(body: dict, db: Session = Depends(get_db),
               user: User = Depends(require_role("admin"))):
    updates = {}
    if "enabled" in body:
        updates["NETOPT_ENABLED"] = "true" if bool(body["enabled"]) else "false"
    if "max_hosts" in body:
        try:
            mh = int(body["max_hosts"])
        except (TypeError, ValueError):
            raise HTTPException(400, "max_hosts must be an integer (1-250)")
        if not 1 <= mh <= 250:
            raise HTTPException(400, "max_hosts must be 1-250")
        updates["NETOPT_MAX_HOSTS"] = str(mh)
    if "concurrency" in body:
        try:
            cc = int(body["concurrency"])
        except (TypeError, ValueError):
            raise HTTPException(400, "concurrency must be an integer (1-16)")
        if not 1 <= cc <= 16:
            raise HTTPException(400, "concurrency must be 1-16")
        updates["NETOPT_CONCURRENCY"] = str(cc)
    if "profile" in body:
        profile = str(body["profile"]).strip().lower()
        if profile not in network_opt.PROFILES:
            raise HTTPException(400, "profile must be 'standard' or 'light'")
        updates["NETOPT_SCAN_PROFILE"] = profile
    if updates:
        try:
            _write_netopt_env(updates)
        except Exception as e:
            raise HTTPException(500, f"could not save limits: {e}")
    return get_limits(db=db, user=user)
