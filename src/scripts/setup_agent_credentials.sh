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

# Wait for the api to be READY (health 200), not just the container running.
# On a fresh install deploy.sh can call this while the stack is still booting;
# a silent failure used to skip the DB upsert while STILL writing the file ->
# agent login 401s and the scheduler floods forever (file & DB out of sync).
# Fail loudly instead of writing a credential file with no matching DB user.
for i in $(seq 1 90); do
  if curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/v1/health 2>/dev/null | grep -q 200; then
    break
  fi
  if [ "$i" = "90" ]; then
    echo "!! api not healthy after 180s — aborting without touching credentials" >&2
    exit 1
  fi
  sleep 2
done
echo "api healthy"

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

# 4. Verify the agent login end-to-end (the file must agree with the DB).
#    Catches the silent-failure class where the file is written but the DB
#    upsert never landed (api down during provisioning).
AUTH_CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST \
  https://127.0.0.1/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"agent\",\"password\":\"$PASSWORD\"}")
if [ "$AUTH_CODE" != "200" ]; then
  echo "!! agent login verification FAILED (HTTP $AUTH_CODE) — credentials file and DB are out of sync" >&2
  exit 1
fi
echo "agent login verified (200)"

# 5. Least privilege — SELF-PROTECTION: pi-agent (the AI agent) must NOT be in
#    the docker group. Docker membership would let the agent stop/remove the
#    appliance's OWN containers (docker stop barenoc-api = self-harm); it was
#    only ever added for directory traversal of /opt/barenoc. Traversal is
#    granted directly instead (o+x on the path — no read, no docker).
if getent group docker | grep -qw pi-agent; then
  gpasswd -d pi-agent docker >/dev/null 2>&1 && echo "removed pi-agent from docker group (self-protection)"
fi
for d in /opt/barenoc /opt/barenoc/volumes /opt/barenoc/volumes/logs \
         /opt/barenoc/volumes/secrets /opt/barenoc/scripts; do
  [ -d "$d" ] && chmod o+x "$d" 2>/dev/null || true
done

# pi's LLM key file + the runner's workdir must be pi-agent-readable/owned
# (a fresh install misses these: llm_provider.json lands root:root 0640 so
# pi errors "No API key found for deepseek", and pi-work doesn't exist so
# every pi_task dies with PermissionError).
chgrp pi-agent /opt/barenoc/volumes/secrets 2>/dev/null || true
chmod 2775 /opt/barenoc/volumes/secrets 2>/dev/null || true
if [ -f /opt/barenoc/volumes/secrets/llm_provider.json ]; then
  chown root:pi-agent /opt/barenoc/volumes/secrets/llm_provider.json 2>/dev/null || true
  chmod 640 /opt/barenoc/volumes/secrets/llm_provider.json 2>/dev/null || true
fi
mkdir -p /opt/barenoc/pi-work
chown -R pi-agent:pi-agent /opt/barenoc/pi-work

echo "agent credentials provisioned -> $CREDS_FILE (0600, pi-agent)"
