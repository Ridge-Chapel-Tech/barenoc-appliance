#!/bin/bash
# AI Technician: look up a ticket's live status by its TKT-… id (read-only).
# Usage: ticket_status.sh <TKT-…>
#
# Answers "status on TKT-…" / "where's TKT-… at" / "is TKT-… done?" tickets.
# Fetches the derived stage + idle age + last note from the API's existing
# GET /api/v1/tickets/{id}/status endpoint — the SAME source the "🔄 Status"
# button and the Juniper responder use, so the answer is byte-identical to the
# web UI. Read-only and parallel-safe: querying never creates work or
# interrupts the technician.
#
# Auth mirrors the other agent scripts (network_info.sh, unifi_*.sh): read the
# agent service-account credentials (/opt/barenoc/agent/credentials, 0600),
# log in for a short-lived token, then GET with a Bearer header. No SSH, no
# DB access. Unknown ticket -> "not found" + non-zero exit (never exit 0).
set -u

TICKET_ID="${1:-}"

python3 - "$TICKET_ID" <<'PYEOF'
import sys, json, ssl, urllib.request, urllib.error

ticket_id = (sys.argv[1] if len(sys.argv) > 1 else "").strip()

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def call(method, path, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        "https://localhost" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=20, context=ctx)
        return json.loads(r.read().decode()), None, r.status
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode()).get("detail", str(e)), e.code
        except Exception:
            return None, str(e), e.code
    except Exception as e:
        return None, str(e), None


def load_creds():
    creds = {}
    try:
        with open("/opt/barenoc/agent/credentials") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    creds[k.strip()] = v.strip()
    except Exception as e:
        print(json.dumps({"ticket_id": ticket_id, "status": "not found",
                          "error": f"cannot read agent credentials: {e}"}))
        sys.exit(1)
    return creds


def not_found(err, code=None):
    status = "not found" if code == 404 else "error"
    print(json.dumps({
        "ticket_id": ticket_id, "status": status, "stage": None, "label": None,
        "idle_seconds": None, "last_event": None, "action": None,
        "confidence": None, "resolution": None, "error": err or "unknown error",
    }))
    sys.exit(1)


if not ticket_id:
    not_found("missing ticket_id argument")

creds = load_creds()
if not creds.get("username") or not creds.get("password"):
    not_found("agent credentials file is incomplete")

login, err, _ = call("POST", "/api/v1/auth/login",
                     {"username": creds["username"], "password": creds["password"]})
if err or not login:
    not_found(f"API login failed: {err}")

data, err, code = call("GET", f"/api/v1/tickets/{ticket_id.upper()}/status",
                       token=login.get("access_token", ""))
if err or data is None:
    not_found(err, code)

# Pass through the endpoint body (the canonical status JSON) with the exact
# keys the ticket formatter reads.
print(json.dumps({
    "ticket_id": data.get("ticket_id", ticket_id.upper()),
    "status": data.get("status"),
    "stage": data.get("stage"),
    "label": data.get("label"),
    "idle_seconds": data.get("idle_seconds"),
    "last_event": data.get("last_event"),
    "action": data.get("action"),
    "confidence": data.get("confidence"),
    "resolution": data.get("resolution"),
}))
PYEOF
