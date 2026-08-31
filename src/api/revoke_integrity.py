"""Device-revoke integrity sweep — catch un-audited revokes.

The ONLY app path that sets ``adoption_status = "revoked"`` is
``routes/devices.py::revoke_adoption``, and it always writes a
``device_adopt_revoke`` audit event. A revoked device with NO matching
``device_adopt_revoke`` event means the state was changed out-of-band (a
direct DB update) — exactly the "silent revoke" gap (the 08-30 laptop case).

The sweep:
  1. finds every device with ``adoption_status == "revoked"``;
  2. checks for a ``device_adopt_revoke`` audit event matching each device_id
     (matched in the event data);
  3. for any revoked device WITHOUT that event — and WITHOUT an existing
     ``device_revoke_integrity`` event (the once-per-device idempotency
     guard) — writes a ``device_revoke_integrity`` audit event and sends one
     alert email via the shared emailer.

The SCHEDULER is the poller (POST /api/v1/revoke-integrity/sweep) — the same
pattern as service checks. This module is imported by
routes/revoke_integrity.py and must not import FastAPI.
"""

import logging

from database import SessionLocal
from models import Device, AuditLog
from audit import log_event
from emailer import send_email, get_recipients, alert_html

logger = logging.getLogger("barenoc-revoke-integrity")


def _device_ids_for_event(session, event_type: str) -> set:
    """The device_ids referenced by every audit event of the given type.

    Matching is done in Python (the JSON ``data`` column) so it works across
    the SQLite JSON serialization the ORM uses — and the event_type filter
    hits the existing ``ix_audit_log_event_type_actor`` index.
    """
    ids = set()
    for (data,) in session.query(AuditLog.data).filter(
            AuditLog.event_type == event_type).all():
        did = data.get("device_id") if isinstance(data, dict) else None
        if did is None:
            continue
        try:
            ids.add(int(did))
        except (TypeError, ValueError):
            pass
    return ids


def run_sweep(session_factory=SessionLocal) -> dict:
    """One pass. Returns {status, checked, flagged, emailed}. Never raises."""
    summary = {"status": "ok", "checked": 0, "flagged": 0, "emailed": 0}
    session = session_factory()
    try:
        revoked = session.query(Device).filter(
            Device.adoption_status == "revoked").all()
        summary["checked"] = len(revoked)
        if not revoked:
            return summary

        audited = _device_ids_for_event(session, "device_adopt_revoke")
        already_flagged = _device_ids_for_event(session, "device_revoke_integrity")

        for d in revoked:
            if d.id in audited or d.id in already_flagged:
                continue
            name = d.name or d.ip_address or f"device {d.id}"
            log_event(session, "device_revoke_integrity", "system", {
                "device_id": d.id,
                "device": name,
                "ip": d.ip_address or "",
                "adoption_status": "revoked",
            })
            already_flagged.add(d.id)
            summary["flagged"] += 1
            ok, _ = send_email(
                get_recipients("alerts"),
                f"[P1] BareNOC: un-audited device revoke detected — {name}",
                body_html=alert_html("Un-audited device revoke detected", [
                    ("Device", name),
                    ("Device ID", str(d.id)),
                    ("IP", d.ip_address or "—"),
                    ("Finding",
                     "<b style='color:#e03131'>adoption_status=revoked</b> with no "
                     "<code>device_adopt_revoke</code> audit event"),
                    ("Action",
                     "Flagged via a <code>device_revoke_integrity</code> audit event. "
                     "Investigate who changed it — the app path always audits revokes."),
                ]),
                body_text=(
                    f"Device {name} (id {d.id}) is revoked but has no "
                    f"device_adopt_revoke audit event — an un-audited state change."
                ),
            )
            if ok:
                summary["emailed"] += 1
        session.commit()
        return summary
    except Exception:
        session.rollback()
        logger.exception("revoke-integrity sweep error")
        return {"status": "error", "checked": summary["checked"],
                "flagged": summary["flagged"], "emailed": summary["emailed"]}
    finally:
        session.close()
