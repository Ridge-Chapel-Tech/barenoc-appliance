"""Customer support bundle — exportable diagnostics for bug reports.

The exported markdown is the handoff artifact: a customer attaches it (plus a
bug description) to a bug report, and a support agent / dev worker diagnoses
from it. Redaction is a HARD requirement — no .env values, API keys, tokens,
passwords, certs, or audit payloads ever leave the appliance unscrubbed.

Logs are fetched via the Docker engine API over the unix socket (the api
container mounts /var/run/docker.sock — same mechanism as
routes/system._container_status). Every line is redacted before inclusion.
"""

import datetime
import json
import os
import re
import socket
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import require_role
from database import get_db
from models import AuditLog, Device, Ticket
from version import APP_VERSION

router = APIRouter(prefix="/api/v1/support", tags=["support"])

# ── redaction ──────────────────────────────────────────────────────────────
# Order matters: longer/more-specific patterns first.
_REDACTIONS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"), r"sk-***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b"), "jwt***"),
    (re.compile(r"(?i)((?:password|passwd|api[_-]?key|secret|client[_-]?secret|token)\s*[=:]\s*)[^\s\"',;]+"), r"\1***"),
    (re.compile(r"(?i)((?:authorization|x-csrf-token|set-cookie)\s*[:=]\s*)[^\s;]+"), r"\1***"),
    (re.compile(r"\b([A-Z][A-Z0-9_]{2,}_(?:KEY|SECRET|PASSWORD|TOKEN)=)\S+\b"), r"\1***"),
    (re.compile(r"(-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)", re.S), "***private-key***"),
    (re.compile(r"(-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----)", re.S), "***certificate***"),
]


def redact(text: str) -> str:
    """Scrub secrets from arbitrary text (log lines, audit payloads, titles)."""
    for pat, repl in _REDACTIONS:
        text = pat.sub(repl, text)
    return text


# ── docker logs (unix socket, like system._container_status) ──────────────
def _docker_logs(container: str, tail: int = 150) -> str:
    """Fetch `docker logs --tail N` for a container via the engine socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect("/var/run/docker.sock")
        sock.sendall(
            f"GET /containers/{container}/logs?stdout=1&stderr=1&tail={tail} HTTP/1.0\r\nHost: docker\r\n\r\n".encode()
        )
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        sock.close()
        _, _, body = data.partition(b"\r\n\r\n")
        return _parse_log_frames(body)
    except Exception:
        return ""


def _parse_log_frames(body: bytes) -> str:
    """Docker log multiplexed frames: [1 stream][3 pad][4 len BE][payload]…"""
    out = []
    i, n = 0, len(body)
    while i + 8 <= n:
        frame_len = int.from_bytes(body[i + 4:i + 8], "big")
        i += 8
        if i + frame_len > n:
            break
        out.append(body[i:i + frame_len])
        i += frame_len
    if not out and body:
        out.append(body)  # plain (non-multiplexed) text
    return b"".join(out).decode("utf-8", "replace")


# ── config presence (key names + safe values only) ────────────────────────
# Values of these keys may be shown; everything else is presence-only.
_SAFE_CONFIG_VALUES = {
    "TZ", "LOG_LEVEL", "SITE_ID", "APPLIANCE_HOST", "APPLIANCE_IP",
    "UNIFI_AUTOSYNC_ENABLED", "UNIFI_AUTOSYNC_INTERVAL_MIN", "UNIFI_AUTO_ADOPT",
    "UNIFI_CLIENT_RETENTION_DAYS", "CHAT_CLIENT_ENABLED", "SEED_DEMO",
    "PATCH_ALLOWLIST", "MAX_CONCURRENT", "RATE_LIMIT_LOGIN", "RATE_LIMIT_CHAT",
    "RATE_LIMIT_API", "SETUP_COMPLETE",
}


def _config_presence() -> list:
    """Redacted view of the appliance .env: key -> 'set' | safe value."""
    out = []
    path = "/opt/barenoc/.env"
    try:
        with open(path) as f:
            lines = f.readlines()
    except Exception:
        return [{"note": "config file not readable from the api container"}]
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in _SAFE_CONFIG_VALUES:
            out.append({"key": key, "value": redact(value)})
        else:
            out.append({"key": key, "value": "<set>" if value else "<empty>"})
    return sorted(out, key=lambda r: r["key"].lower())


# ── bundle assembly ────────────────────────────────────────────────────────
def _md(lines) -> str:
    return "\n".join(str(l) for l in lines) + "\n"


class BundleRequest(BaseModel):
    bug_description: Optional[str] = None


def build_bundle(desc: str, db: Session, user) -> str:
    """Assemble the redacted diagnostic markdown bundle.

    Shared by the /bundle download endpoint and the Submit Report flow (which
    ships the same bundle to the forum). Returns the markdown text.
    """
    desc = (desc or "").strip()[:2000]
    now = datetime.datetime.utcnow()
    local_tz = os.environ.get("TZ", "UTC")

    # ── system snapshot (reuse routes.system.system_status) ──
    from routes.system import system_status
    sysdata = system_status(db, user)

    # ── inventory (safe fields only — never credentials) ──
    devices = db.query(Device).order_by(Device.id.desc()).limit(200).all()
    inv = []
    for d in devices:
        inv.append({
            "id": d.id, "name": redact(d.name or ""), "ip": redact(d.ip_address or ""),
            "mac": redact(d.mac_address or ""), "type": d.device_type,
            "model": redact(d.model or ""), "vendor": redact(d.vendor or ""),
            "hostname": redact(d.hostname or ""), "status": d.status,
            "claimed": bool(d.claimed), "managed": d.unifi_managed,
            "adoption": d.adoption_status or "none",
            "method": d.adoption_method or None,
            "last_seen": d.last_seen.isoformat() if d.last_seen else None,
            "agent_version": d.agent_version if hasattr(d, "agent_version") else None,
        })

    # ── tickets (summary only) ──
    tickets = db.query(Ticket).order_by(Ticket.id.desc()).limit(15).all()
    tsum = [{
        "ticket": t.ticket_id, "status": t.status, "priority": t.priority,
        "source": t.source, "created": t.created_at.isoformat() if getattr(t, "created_at", None) else None,
    } for t in tickets]

    # ── audit (payloads redacted) ──
    audit = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(30).all()
    asum = [{
        "at": a.timestamp.isoformat() if a.timestamp else None,
        "actor": redact(a.actor or ""), "event": a.event_type,
        "ticket": a.ticket_id, "data": redact(json.dumps(a.data or {})[:200]),
    } for a in audit]

    # ── logs (redacted) ──
    log_sections = []
    error_lines = []
    for cname in ("barenoc-api", "barenoc-worker", "barenoc-scheduler",
                  "barenoc-nginx", "barenoc-step-ca", "barenoc-pocket-id",
                  "barenoc-dns"):
        raw = _docker_logs(cname, 150)
        if not raw.strip():
            continue
        lines = [redact(l) for l in raw.splitlines()]
        log_sections.append((cname, lines))
        error_lines += [l for l in lines
                        if re.search(r"(?i)\b(error|traceback|exception|failed|panic)\b", l)]

    # Host-side agent runner log (jobs it executed / results it posted — the
    # first place to look when an auto-executed job stalls silently).
    runner_log = ""
    try:
        with open("/opt/barenoc/volumes/logs/agent/agent.log") as f:
            runner_log = "\n".join(f.read().splitlines()[-150:])
    except Exception:
        pass
    if runner_log.strip():
        rl = [redact(l) for l in runner_log.splitlines()]
        log_sections.append(("barenoc-agent-runner (host)", rl))
        error_lines += [l for l in rl
                        if re.search(r"(?i)\b(error|traceback|exception|failed|panic)\b", l)]

    L = []
    L.append("# BareNOC support bundle")
    L.append("")
    L.append(f"- **product**: BareNOC appliance")
    L.append(f"- **version**: {APP_VERSION}")
    L.append(f"- **generated (UTC)**: {now.isoformat()}Z")
    L.append(f"- **app timezone**: {local_tz}")
    L.append(f"- **bug description**: {desc or '(none provided)'}")
    L.append("")
    L.append("> Attach this file to your bug report. Secrets are scrubbed —")
    L.append("> if a support agent asks for a value not shown, they will guide")
    L.append("> you to a safe way to provide it.")
    L.append("")
    L.append("## 1. System snapshot")
    L.append("")
    L.append("```json")
    L.append(json.dumps(sysdata, indent=1, default=str)[:6000])
    L.append("```")
    L.append("")
    L.append("## 2. App config (redacted — presence only)")
    L.append("")
    for row in _config_presence():
        L.append(f"- `{row['key']}` = {row['value']}")
    L.append("")
    L.append(f"## 3. Inventory ({len(inv)} devices — safe fields, no credentials)")
    L.append("")
    L.append("```json")
    L.append(json.dumps(inv, indent=1, default=str)[:8000])
    L.append("```")
    L.append("")
    L.append(f"## 4. Tickets (last {len(tsum)})")
    L.append("")
    L.append("```json")
    L.append(json.dumps(tsum, indent=1, default=str))
    L.append("```")
    L.append("")
    L.append(f"## 5. Audit trail (last {len(asum)}, payloads redacted)")
    L.append("")
    L.append("```json")
    L.append(json.dumps(asum, indent=1, default=str))
    L.append("```")
    L.append("")
    if error_lines:
        L.append(f"## 6. Error signals from logs ({len(error_lines)} lines)")
        L.append("")
        L.append("```")
        L.append("\n".join(error_lines[-60:]))
        L.append("```")
        L.append("")
    L.append(f"## 7. Container logs (last 150 lines each, redacted)")
    L.append("")
    for cname, lines in log_sections:
        L.append(f"### {cname}")
        L.append("")
        L.append("```")
        L.append("\n".join(lines[-150:]))
        L.append("```")
        L.append("")
    L.append("---")
    L.append("*End of bundle. Redaction applied: secrets/tokens/keys/certs are never exported.*")

    return _md(L)


@router.post("/bundle")
def export_bundle(body: BundleRequest = None,
                  db: Session = Depends(get_db),
                  user=Depends(require_role("admin"))):
    """Export a redacted diagnostic markdown bundle (admin only).

    Attach the download + a bug description to a support ticket; a support
    agent/worker diagnoses from it. Secrets are scrubbed before export.
    """
    body = body or BundleRequest()
    md = build_bundle(body.bug_description, db, user)
    now = datetime.datetime.utcnow()
    fname = f"barenoc-support-{now.strftime('%Y%m%d-%H%M%S')}.md"
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
