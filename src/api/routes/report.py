"""Submit Report — in-app "Submit Report" → forum bug thread + bundle.

System → Support / Bug Report gains a Submit Report button beside the existing
Download support bundle. The flow:

  1. mandatory comment (the bug description)
  2. AI vets it (one LLM call): bug / not-bug / unclear
  3. gate check (REPORT_GATE: open during beta; support-gated at GA)
  4. forum-submit edge function creates the bug thread (attributed to the
     logged-in BareNOC user) + uploads the support bundle to session-logs.

The vetting is advisory UX (never a security gate); the REPORT_GATE is the real
gate. Submit re-checks the gate server-side and ships the comment + generated
bundle to the forum.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import require_role
from database import get_db
from models import User
import report_gate
import report_submit
import report_vet
from routes import support

router = APIRouter(prefix="/api/v1/report", tags=["report"])


class VetRequest(BaseModel):
    comment: str = Field(..., min_length=1, max_length=4000)


class SubmitRequest(BaseModel):
    comment: str = Field(..., min_length=1, max_length=4000)
    flagged: bool = False  # true when submitted after an "unclear" prompt


@router.post("/vet")
def vet_report(body: VetRequest, user: User = Depends(require_role("admin"))):
    """Gate state + one-LLM-call classification of the comment."""
    gate = report_gate.report_gate_status(user)
    verdict = report_vet.vet_comment(body.comment)
    return {"gate": gate, **verdict}


@router.post("/submit")
def submit_report(body: SubmitRequest,
                  db: Session = Depends(get_db),
                  user: User = Depends(require_role("admin"))):
    """Generate the bundle + POST to the forum-submit edge function.

    Returns the forum thread id/url. The REPORT_GATE is enforced here (the
    authoritative check — the UI's vet step is advisory).
    """
    gate = report_gate.report_gate_status(user)
    if not report_gate.report_gate_allowed(user):
        raise HTTPException(status_code=403, detail=gate["note"])

    bundle = support.build_bundle(body.comment, db, user)
    try:
        result = report_submit.submit_report(
            body.comment, user,
            bundle=bundle,
            bundle_filename="barenoc-support.md",
            flagged=body.flagged,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, **result}
