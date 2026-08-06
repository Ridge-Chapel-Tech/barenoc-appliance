from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from typing import Optional
import os
from datetime import datetime, timedelta
from database import get_db
from models import Ticket, AuditLog, User
from auth import get_current_user, require_role

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/llm-usage")
def llm_usage(
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Get LLM usage statistics for the last N days.

    Data source: the hash-chained audit log (event_type='llm_request') — the
    durable record. Ticket rows are ephemeral (they can be wiped), so usage is
    never derived from Ticket.llm_* columns.
    """
    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.event_type == "llm_request", AuditLog.timestamp >= since)
        .all()
    )

    # Batch title lookup for surviving tickets (deleted tickets -> "—")
    ticket_ids = [r.data.get("ticket_id") for r in rows if isinstance(r.data, dict) and r.data.get("ticket_id")]
    titles = {}
    if ticket_ids:
        for t in db.query(Ticket).filter(Ticket.ticket_id.in_(ticket_ids)).all():
            titles[t.ticket_id] = t.title

    daily = {}
    model_breakdown = {}
    total_tokens = 0
    total_cost = 0.0
    recent = []

    for r in rows:
        data = r.data if isinstance(r.data, dict) else {}
        day = r.timestamp.date().isoformat() if r.timestamp else "unknown"
        pt = data.get("prompt_tokens") or 0
        rt = data.get("response_tokens") or 0
        cost = data.get("cost_usd") or 0.0
        model = data.get("model") or "unknown"

        d = daily.setdefault(day, {"calls": 0, "prompt_tokens": 0, "response_tokens": 0, "cost": 0.0})
        d["calls"] += 1
        d["prompt_tokens"] += pt
        d["response_tokens"] += rt
        d["cost"] += cost

        m = model_breakdown.setdefault(model, {"calls": 0, "tokens": 0, "cost": 0.0})
        m["calls"] += 1
        m["tokens"] += pt + rt
        m["cost"] += cost

        total_tokens += pt + rt
        total_cost += cost

        tid = data.get("ticket_id") or ""
        recent.append({
            "ticket_id": tid,
            "title": titles.get(tid) or "—",
            "model": model,
            "confidence": data.get("confidence"),
            "cost": cost,
            "tokens": pt + rt,
            "action": data.get("action"),
            "created_at": r.timestamp.isoformat() if r.timestamp else None,
        })

    recent = sorted(recent, key=lambda x: x["created_at"] or "", reverse=True)[:20]

    return {
        "period_days": days,
        "total_calls": sum(d["calls"] for d in daily.values()),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "daily": [{"date": k, **daily[k]} for k in sorted(daily.keys(), reverse=True)],
        "by_model": model_breakdown,
        "recent": recent,
    }


@router.get("/config")
def get_config(user: User = Depends(require_role("admin"))):
    """Get the ACTIVE LLM provider config (redacting secrets)."""
    from llm_providers import load_providers, active_provider_name
    from routes.settings import _read_env_file
    env = _read_env_file()
    providers = load_providers(env)
    active = active_provider_name(env)
    p = providers.get(active) or next(iter(providers.values()), {})
    return {
        "provider": active,
        "chat_model": p.get("chat_model", ""),
        "reasoner_model": p.get("reasoner_model", ""),
        "has_api_key": bool(p.get("api_key")),
        "access_token_expire_minutes": int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")),
        "site_id": os.getenv("SITE_ID", "1"),
        "customer_name": os.getenv("CUSTOMER_NAME", "Demo Site"),
    }


@router.post("/config")
def update_config(
    config: dict,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Update LLM configuration (saved to .env)."""
    import os
    env_path = "/opt/barenoc/.env"
    updates = 0

    # Allowed config keys
    allowed = {
        "deepseek_chat_model": "DEEPSEEK_CHAT_MODEL",
        "deepseek_reasoner_model": "DEEPSEEK_REASONER_MODEL",
        "access_token_expire_minutes": "ACCESS_TOKEN_EXPIRE_MINUTES",
    }

    try:
        with open(env_path, "r") as f:
            lines = f.readlines()

        for key, env_key in allowed.items():
            if key in config:
                new_lines = []
                for line in lines:
                    if line.startswith(f"{env_key}="):
                        new_lines.append(f"{env_key}={config[key]}\n")
                        updates += 1
                    else:
                        new_lines.append(line)
                lines = new_lines

        with open(env_path, "w") as f:
            f.writelines(lines)

        return {"status": "ok", "updates": updates}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))
