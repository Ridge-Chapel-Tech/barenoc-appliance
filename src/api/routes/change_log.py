"""Change log views + download artifact (L2 managed-environment intelligence).

Endpoints:
  GET /api/v1/environment/changes?view=customer|agent&limit=&offset=
      The two views: customer (customer_visible events, one-line readable,
      no technical detail) and agent (full detail + links).
  GET /api/v1/environment/changes/download?view=...&format=markdown|json
      The downloadable artifact — Markdown (human-keepable documentation) or
      JSON (machine-readable) — with a Content-Disposition attachment.

Route-prefix claim (coordination note): this lane owns ONLY
`/api/v1/environment/changes*`. The rest of `/api/v1/environment/*` is the
knowledge-layer (L1) lane's query surface — see the PR body / handoff.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from auth import get_access_context, require_any_role
from database import get_db
import change_log

router = APIRouter(prefix="/api/v1/environment", tags=["change-log"])

VIEWS = ("customer", "agent")
FORMATS = ("markdown", "md", "json")


def _resolve_view(view: str) -> str:
    view = (view or "agent").strip().lower()
    if view not in VIEWS:
        raise HTTPException(status_code=400,
                            detail="view must be 'customer' or 'agent'")
    return view


def _gate_agent_view(ctx: dict) -> None:
    """The agent view carries technical detail + links — tech tier only
    (admin/technician/operator) or the internal agent identity."""
    require_any_role("technician", "operator", "admin", "agent")(ctx["user"])


@router.get("/changes")
def list_changes(view: str = "agent",
                 limit: int = Query(200, ge=1, le=1000),
                 offset: int = Query(0, ge=0),
                 db: Session = Depends(get_db),
                 ctx: dict = Depends(get_access_context)):
    view = _resolve_view(view)
    if view == "agent":
        _gate_agent_view(ctx)
    total, rows = change_log.query_entries(db, view=view, limit=limit, offset=offset)
    return {
        "view": view,
        "total": total,
        "limit": limit,
        "offset": offset,
        "events": [change_log.entry_dict(r, view=view) for r in rows],
    }


@router.get("/changes/download")
def download_changes(view: str = "agent", format: str = "markdown",
                     db: Session = Depends(get_db),
                     ctx: dict = Depends(get_access_context)):
    view = _resolve_view(view)
    if view == "agent":
        _gate_agent_view(ctx)
    fmt = (format or "markdown").strip().lower()
    if fmt == "md":
        fmt = "markdown"
    if fmt not in FORMATS:
        raise HTTPException(status_code=400,
                            detail="format must be 'markdown' or 'json'")

    total, rows = change_log.query_entries(db, view=view, limit=5000, offset=0)
    if fmt == "json":
        body = change_log.render_json(rows, view=view)
        media = "application/json"
        filename = f"change-log-{view}.json"
    else:
        body = change_log.render_markdown(rows, view=view)
        media = "text/markdown; charset=utf-8"
        filename = f"change-log-{view}.md"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
