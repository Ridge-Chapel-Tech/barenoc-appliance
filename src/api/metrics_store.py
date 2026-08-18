"""Metrics store — the time-series persistence layer (telemetry backbone).

Deliberately narrow and channel-agnostic: collectors hand it a list of
``(device_id, metric, ts, value)`` samples, the store batches them into the
``metrics`` table, and the trends reader buckets + aggregates them for the API.

Write-friendly:
  * ``write_samples`` does ONE bulk insert per poll (batched).
  * SQLite WAL mode is already on (database.py) so concurrent reads/writes
    from the alert engine + collectors + API don't block each other.

Retention:
  * ``prune`` deletes whole rows older than the configured window. It also
    understands DISK PRESSURE: when the volume free % drops below the
    configured floor it prunes harder (to ``floor_days``) and checkpoints WAL
    back to the main file so space is actually reclaimed.

The SCHEDULER is the primary retention pruner (it runs a stdlib-sqlite3
version of this same math — see scheduler/main.py), and this module's prune is
also reachable via POST /api/v1/metrics/prune for an admin manual trigger.
"""

import datetime
import logging
import os

from sqlalchemy import func

from database import SessionLocal
from models import Metric

logger = logging.getLogger("barenoc.metrics")

# The trends reader caps the points it loads so a huge range can't OOM the
# api container; bucketing then reduces that to at most MAX_BUCKETS points.
MAX_RAW_POINTS = 100_000
MAX_BUCKETS = 500
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MIN_FREE_PCT = 10
FLOOR_RETENTION_DAYS = 7


# ── write ──────────────────────────────────────────────────────────────────

def write_samples(db, samples: list) -> int:
    """Batch-insert samples. Each sample: dict(device_id, metric, ts, value).

    Invalid samples (missing device, empty metric, non-finite value) are
    skipped rather than raising — a bad collector sample must never take the
    engine down. Returns the number of rows actually written."""
    rows = []
    for s in samples or []:
        try:
            device_id = int(s.get("device_id"))
            metric = str(s.get("metric") or "").strip()
            ts = s.get("ts")
            value = float(s.get("value"))
        except (TypeError, ValueError):
            continue
        if not device_id or not metric or ts is None:
            continue
        if value != value or value in (float("inf"), float("-inf")):
            continue  # NaN / ±inf never belongs in the store
        rows.append(Metric(device_id=device_id, metric=metric, ts=ts, value=value))
    if rows:
        db.add_all(rows)
        db.commit()
    return len(rows)


# ── read / trends ──────────────────────────────────────────────────────────

def _epoch(dt) -> float:
    """Naive-UTC datetime -> epoch seconds (all samples are stored naive UTC)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds()


def _from_epoch(seconds: float) -> datetime.datetime:
    return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + \
        datetime.timedelta(seconds=seconds)


def query_range(db, device_id: int, metric: str, start, end):
    """Raw samples in [start, end] ordered by ts (capped at MAX_RAW_POINTS)."""
    return (
        db.query(Metric)
        .filter(
            Metric.device_id == device_id,
            Metric.metric == metric,
            Metric.ts >= start,
            Metric.ts <= end,
        )
        .order_by(Metric.ts.asc())
        .limit(MAX_RAW_POINTS)
        .all()
    )


def trends(db, device_id: int, metric: str, start, end, agg: str = "avg",
           max_buckets: int = MAX_BUCKETS) -> list:
    """Bucketed min/avg/max over a range.

    Buckets are aligned to the epoch (``floor(ts_epoch / bucket_seconds)``) so
    the same query always returns the same bucket boundaries regardless of the
    from/to — important for stable charts and for the future diff/capacity
    consumers. Empty buckets are OMITTED (a gap stays a gap — no fabricated
    points), which is what lets the UI show data outages honestly.

    Returns ``[{"ts": iso, "value": float, "n": int}]`` where ``value`` is the
    requested aggregate of the bucket and ``n`` the raw sample count."""
    agg = (agg or "avg").lower()
    if agg not in ("min", "avg", "max"):
        agg = "avg"
    rows = query_range(db, device_id, metric, start, end)
    if not rows:
        return []

    span = max(1.0, _epoch(end) - _epoch(start))
    bucket = max(1.0, span / max(1, max_buckets))

    buckets = {}
    for r in rows:
        key = int(_epoch(r.ts) // bucket)
        buckets.setdefault(key, []).append(r.value)

    out = []
    for key in sorted(buckets):
        vals = buckets[key]
        if agg == "min":
            value = min(vals)
        elif agg == "max":
            value = max(vals)
        else:
            value = sum(vals) / len(vals)
        bucket_ts = _from_epoch(key * bucket)
        out.append({
            "ts": bucket_ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "value": round(float(value), 4),
            "n": len(vals),
        })
    return out


def catalog(db) -> list:
    """Distinct (device_id, metric) pairs with sample count + latest ts — what
    the UI dropdowns need to offer a chart without hardcoding metric names."""
    rows = (
        db.query(Metric.device_id, Metric.metric,
                 func.count(Metric.id), func.max(Metric.ts))
        .group_by(Metric.device_id, Metric.metric)
        .order_by(Metric.device_id, Metric.metric)
        .all()
    )
    return [{
        "device_id": r[0],
        "metric": r[1],
        "count": r[2],
        "latest_ts": r[3].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if r[3] else None,
    } for r in rows]


# ── retention ──────────────────────────────────────────────────────────────

def _volume_free_pct() -> float:
    """Free % of the volume holding the sqlite DB (disk-aware pruning)."""
    try:
        from database import engine
        url = str(engine.url)
        path = url.split("sqlite:///", 1)[-1]
        st = os.statvfs(os.path.dirname(path) or "/")
        if st.f_blocks <= 0:
            return 100.0
        return round(st.f_bavail / st.f_blocks * 100.0, 1)
    except Exception:
        return 100.0


def retention_days(configured_days: int, min_free_pct: int,
                   floor_days: int = FLOOR_RETENTION_DAYS,
                   free_pct: float = None) -> int:
    """Effective retention window. Normal: ``configured_days``. Disk pressure
    (free % below ``min_free_pct``): clamp to ``floor_days``. Pure + testable —
    the scheduler's stdlib prune uses the same math."""
    days = max(1, int(configured_days))
    floor = max(1, int(floor_days))
    free = free_pct if free_pct is not None else _volume_free_pct()
    if free < max(1, int(min_free_pct)):
        return min(days, floor)
    return days


def prune(db, days: int = DEFAULT_RETENTION_DAYS,
          min_free_pct: int = DEFAULT_MIN_FREE_PCT) -> dict:
    """Delete samples older than the (possibly disk-adjusted) retention window.

    Returns {deleted, retention_days, free_pct}. Never raises — a failed prune
    is logged, not fatal (the scheduler retries next cycle)."""
    free_pct = _volume_free_pct()
    eff = retention_days(days, min_free_pct, free_pct=free_pct)
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=eff)
    deleted = 0
    try:
        deleted = (
            db.query(Metric)
            .filter(Metric.ts < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        if free_pct < min_free_pct:
            # Reclaim WAL space back into the main file under disk pressure.
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
    except Exception as e:
        db.rollback()
        logger.warning("metrics prune failed: %s", e)
    return {"deleted": deleted, "retention_days": eff, "free_pct": free_pct}


def latest_ts(db, device_id: int = None, metric: str = None) -> "datetime.datetime | None":
    q = db.query(func.max(Metric.ts))
    if device_id is not None:
        q = q.filter(Metric.device_id == device_id)
    if metric is not None:
        q = q.filter(Metric.metric == metric)
    return q.scalar()
