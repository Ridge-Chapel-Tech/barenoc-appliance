"""Telemetry trends API — the read surface for the future UI, NetOpt
capacity checks, the status page and SLA reports.

Admin/operator-gated like other data endpoints:
  GET  /api/v1/metrics/trends?device=&metric=&from=&to=&agg=
       min/avg/max, bucketed (empty buckets omitted = honest gaps)
  GET  /api/v1/metrics/catalog     distinct (device, metric) pairs for the UI
  POST /api/v1/metrics/ingest     batch-write samples (admin/agent — collectors
                                   write direct, this is for future channels)
  POST /api/v1/metrics/prune      manual retention prune (admin; the scheduler
                                   is the primary pruner)

The SCHEDULER prunes retention on its own cadence (stdlib sqlite3 — see
scheduler/main.py); this module just exposes the same store math for the admin
and tests.
"""

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import require_role, require_any_role
from database import get_db
from models import User, Device
import metrics_store
import telemetry

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


def _parse_ts(s: str, default: datetime.datetime) -> datetime.datetime:
    if not s:
        return default
    s = s.strip().replace("Z", "")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"bad timestamp: {s!r} (ISO 8601)")
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


@router.get("/trends")
def trends(device: int = Query(..., ge=1),
           metric: str = Query(..., min_length=1, max_length=128),
           from_: str = Query(None, alias="from"),
           to: str = Query(None),
           agg: str = Query("avg", pattern="^(min|avg|max)$"),
           db: Session = Depends(get_db),
           user: User = Depends(require_role("operator"))):
    """Bucketed min/avg/max series for one (device, metric) over a range."""
    to_dt = _parse_ts(to, datetime.datetime.utcnow())
    from_dt = _parse_ts(from_, to_dt - datetime.timedelta(hours=24))
    if from_dt >= to_dt:
        raise HTTPException(422, "from must be before to")
    if not db.query(Device).filter(Device.id == device).first():
        raise HTTPException(404, "device not found")
    points = metrics_store.trends(db, device, metric, from_dt, to_dt, agg)
    return {
        "device": device,
        "metric": metric,
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "agg": agg,
        "points": points,
    }


@router.get("/catalog")
def catalog(db: Session = Depends(get_db),
            user: User = Depends(require_role("operator"))):
    """Distinct (device_id, metric) pairs with counts — what the UI needs to
    offer a metric dropdown without hardcoding names."""
    rows = metrics_store.catalog(db)
    names = {d.id: d.name for d in db.query(Device).all()}
    for r in rows:
        r["device_name"] = names.get(r["device_id"], "")
    return {"metrics": rows, "total": len(rows)}


class IngestSample(BaseModel):
    device_id: int = Field(..., ge=1)
    metric: str = Field(..., min_length=1, max_length=128)
    ts: datetime.datetime
    value: float


class IngestBody(BaseModel):
    samples: list[IngestSample] = Field(..., max_length=10_000)


@router.post("/ingest", status_code=201)
def ingest(body: IngestBody, db: Session = Depends(get_db),
           user: User = Depends(require_any_role("admin", "agent"))):
    """Batch-write samples (bounded). The in-process collectors write direct;
    this endpoint is the door for future out-of-process channels."""
    samples = [{"device_id": s.device_id, "metric": s.metric, "ts": s.ts,
                "value": s.value} for s in body.samples]
    written = metrics_store.write_samples(db, samples)
    return {"status": "ok", "written": written, "received": len(samples)}


@router.post("/prune")
def prune(days: int = Query(None, ge=1, le=3650),
          db: Session = Depends(get_db),
          user: User = Depends(require_role("admin"))):
    """Manual retention prune (admin). The scheduler does this automatically;
    this is the override + test hook."""
    cfg = telemetry.telemetry_config()
    days = days or cfg["retention_days"]
    result = metrics_store.prune(db, days=days,
                                 min_free_pct=cfg["disk_min_free_pct"])
    return result
