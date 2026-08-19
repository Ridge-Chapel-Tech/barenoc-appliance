"""Starlink link-health read surface — one lean endpoint for the System page.

  GET  /api/v1/starlink/status   (operator+)

Returns the collector's config + the dish device + the latest starlink.*
metrics (the honest last samples), the episode health state, any open
degradation/outage ticket, and a mini trend (last hour) for the two headline
signals (ping + downlink throughput). Gaps show as "no data" rather than
fabricated points.
"""

import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import require_role
from database import get_db
from models import User, Device, Ticket, Metric, StarlinkEpisode
import metrics_store
import starlink

router = APIRouter(prefix="/api/v1/starlink", tags=["starlink"])

_TREND_METRICS = (("starlink.ping_ms", "ping_ms"),
                  ("starlink.down_mbps", "down_mbps"))


def _iso(dt) -> "str | None":
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if dt else None


@router.get("/status")
def status(db: Session = Depends(get_db),
           user: User = Depends(require_role("operator"))):
    cfg = starlink.starlink_config()
    device = (db.query(Device).filter(Device.device_type == "dish")
              .order_by(Device.id.asc()).first())
    if device is None:
        return {"enabled": cfg["enabled"], "address": cfg["address"],
                "device": None, "latest": {}, "latest_ts": None,
                "health": "unknown", "open_ticket": None, "trend": {}}

    latest = {}
    latest_ts = None
    for metric in starlink.ALL_METRICS:
        row = (db.query(Metric)
               .filter(Metric.device_id == device.id, Metric.metric == metric)
               .order_by(Metric.ts.desc()).first())
        if row:
            latest[metric] = row.value
            if latest_ts is None or row.ts > latest_ts:
                latest_ts = row.ts

    ep = (db.query(StarlinkEpisode)
          .filter(StarlinkEpisode.device_id == device.id).first())
    health = ep.state if ep is not None else "unknown"

    ticket = None
    if ep is not None and ep.ticket_id:
        t = db.query(Ticket).filter(Ticket.ticket_id == ep.ticket_id).first()
        if t is not None and t.status in ("open", "in_progress"):
            ticket = {"ticket_id": t.ticket_id, "priority": t.priority,
                      "status": t.status, "title": t.title}

    trend = {}
    now = datetime.datetime.utcnow()
    for metric, name in _TREND_METRICS:
        trend[name] = metrics_store.trends(
            db, device.id, metric, now - datetime.timedelta(hours=1), now,
            "avg", max_buckets=30)

    return {
        "enabled": cfg["enabled"],
        "address": cfg["address"],
        "device": {"id": device.id, "name": device.name},
        "latest": latest,
        "latest_ts": _iso(latest_ts),
        "health": health,
        "open_ticket": ticket,
        "trend": trend,
    }
