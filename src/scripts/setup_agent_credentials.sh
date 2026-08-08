#!/bin/bash
# Provision a dedicated `agent` service account + 0600 credential file for
# the Pi Agent Runner, its scripts, and the scheduler container. Removes the
# hardcoded admin credentials that were embedded in runner.py, scheduler/main.py
# and the unifi_*.sh scripts.
#
#   * creates/updates user `agent` (role=admin — required today by
#     unifi/sync + unifi_port_config which are admin-gated; tightening to
#     least-privilege is future work once write-actions get their own identity)
#   * writes /opt/barenoc/agent/credentials  (0600, owned by pi-agent)
#   * audit-logs the provisioning via the app's hash-chained audit
#
# Idempotent — safe to run on every deploy (rotates the password). Needs root.
set -u

AGENT_DIR="/opt/barenoc/agent"
CREDS_FILE="$AGENT_DIR/credentials"

# Guard: a stale DIRECTORY at the credentials path (created when the agent dir
# wasn't pi-agent-owned on a fresh install) breaks the agent login — clear it.
if [ -d "$CREDS_FILE" ]; then
  echo "removing stale credentials directory: $CREDS_FILE"
  rm -rf "$CREDS_FILE"
fi

# 1. Generate a fresh random password (rotate on every run)
PASSWORD=$(openssl rand -hex 24)

# 2. Upsert the agent user + audit row (inside the api container)
if ! docker exec -i barenoc-api python3 - "$PASSWORD" <<'PYEOF'
import sys
from auth import hash_password
from database import SessionLocal
from models import User
from audit import log_event

password = sys.argv[1]
db = SessionLocal()
u = db.query(User).filter(User.username == "agent").first()
if u is None:
    u = User(username="agent", display_name="Pi Agent (service)",
             hashed_password=hash_password(password), role="agent",
             is_active=True, must_change_password=False)
    db.add(u)
    print("agent user created")
else:
    u.hashed_password = hash_password(password)
    u.role = "agent"
    u.is_active = True
    u.must_change_password = False
    print("agent user updated")
db.commit()
log_event(db, "settings_change", "provisioning", {
    "section": "agent-credentials", "fields": ["username"],
    "values": {"username": "agent"},
})
db.commit()
db.close()
PYEOF
then
    echo "!! failed to upsert agent user in api container"
    exit 1
fi

# 3. Write the credential file (0600, pi-agent) — never world-readable
umask 077
printf 'username=agent\npassword=%s\n' "$PASSWORD" > "$CREDS_FILE"
chown pi-agent:pi-agent "$CREDS_FILE"
chmod 600 "$CREDS_FILE"

echo "agent credentials provisioned -> $CREDS_FILE (0600, pi-agent)"
