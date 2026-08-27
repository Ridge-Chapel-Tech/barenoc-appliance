import os
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////opt/barenoc/volumes/db/barenoc.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

# Enable WAL mode for better concurrent access
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    # Migration: add owner_id (tenant ownership) to existing databases
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN owner_id INTEGER"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: add must_change_password to existing databases
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: add oidc_sub (Pocket ID subject) to existing databases
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN oidc_sub VARCHAR(128)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_users_oidc_sub ON users(oidc_sub)"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: add device_group (Pocket ID group gate) to existing databases
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN device_group VARCHAR(64) DEFAULT 'default'"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_devices_device_group ON devices(device_group)"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: add fingerprint (nmap results) to devices
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN fingerprint JSON"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: add notify_state_changes (email down/recovery alerts opt-in)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN notify_state_changes BOOLEAN DEFAULT 0"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: per-user tickets-page default filters
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN default_ticket_status VARCHAR(24)"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN default_ticket_priority VARCHAR(4)"
            ))
    except OperationalError:
        pass  # Columns already exist
    # Migration: device adoption via step-ca certificates (Phase F)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN adoption_method VARCHAR(16) DEFAULT 'none'"
            ))
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN cert_cn VARCHAR(128)"
            ))
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN cert_serial VARCHAR(64)"
            ))
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN cert_enrolled_at DATETIME"
            ))
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN cert_last_seen DATETIME"
            ))
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN adoption_status VARCHAR(16) DEFAULT 'none'"
            ))
    except OperationalError:
        pass  # Columns already exist
    # Migration: NOC_Agent self-report fields (P1a) — idempotent, like the
    # rest: create_all won't add columns to an existing DB, so add them here.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN agent_version VARCHAR(64)"
            ))
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN agent_capabilities TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN facts_json TEXT"
            ))
    except OperationalError:
        pass  # Columns already exist
    # Migration: is_bot (Juniper Queue Manager bot user, Phase 1) — idempotent;
    # create_all won't add a column to an existing users table.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN is_bot BOOLEAN DEFAULT 0"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: users.token_version (P0 revocation batch 2026-08-25) — bump
    # invalidates every outstanding JWT for the user. Guarded like the rest.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: users TOTP second factor + login lockout (compliance controls
    # 2026-08-25). Idempotent ALTERs; create_all won't add columns to an
    # existing users table.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN otp_secret VARCHAR(64)"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN otp_verified BOOLEAN DEFAULT 0"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN failed_logins INTEGER DEFAULT 0"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN locked_until DATETIME"
            ))
    except OperationalError:
        pass  # Columns already exist
    # Migration: auth_sessions (P0 revocation batch 2026-08-25). NEW table —
    # create_all above already creates it; the guarded CREATEs are idempotent
    # belt-and-suspenders no-ops. KEEP IN SYNC with models.AuthSession.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS auth_sessions ("
                " id INTEGER PRIMARY KEY,"
                " user_id INTEGER NOT NULL,"
                " jti VARCHAR(64) NOT NULL,"
                " created_at DATETIME,"
                " expires_at DATETIME NOT NULL,"
                " revoked_at DATETIME,"
                " last_used_at DATETIME,"
                " ip VARCHAR(64),"
                " user_agent VARCHAR(256)"
                ")"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_sessions_jti "
                "ON auth_sessions(jti)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_auth_sessions_user_id "
                "ON auth_sessions(user_id)"
            ))
    except OperationalError:
        pass  # Table/index already exists
    # Migration: channels (explicit control-channel declarations, JSON) —
    # device_adoption_model.md §8. Idempotent like the rest: create_all won't
    # add a column to an existing devices table.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE devices ADD COLUMN channels JSON"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: link_episodes (link-stability monitor state machine). A NEW
    # table — create_all above already creates it, so this guarded CREATE is an
    # idempotent belt-and-suspenders no-op. KEEP IN SYNC with models.LinkEpisode.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS link_episodes ("
                " id INTEGER PRIMARY KEY,"
                " device_id INTEGER NOT NULL,"
                " interface VARCHAR(128) NOT NULL,"
                " state VARCHAR(16) DEFAULT 'flapping',"
                " flap_count INTEGER DEFAULT 0,"
                " flap_timestamps JSON,"
                " window_start DATETIME,"
                " down_since DATETIME,"
                " last_event_at DATETIME,"
                " escalated VARCHAR(4) DEFAULT 'P2',"
                " escalation_reason VARCHAR(32),"
                " ticket_id VARCHAR(32),"
                " created_at DATETIME,"
                " updated_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_link_episodes_dev_iface "
                "ON link_episodes(device_id, interface)"
            ))
    except OperationalError:
        pass  # Table/index already exists
    # Migration: starlink_episodes (Starlink link-health monitor state machine).
    # A NEW table — create_all above already creates it, so this guarded CREATE
    # is an idempotent belt-and-suspenders no-op. KEEP IN SYNC with
    # models.StarlinkEpisode.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS starlink_episodes ("
                " id INTEGER PRIMARY KEY,"
                " device_id INTEGER NOT NULL,"
                " state VARCHAR(16) DEFAULT 'healthy',"
                " degraded_since DATETIME,"
                " outage_since DATETIME,"
                " recovered_since DATETIME,"
                " last_event_at DATETIME,"
                " escalated VARCHAR(4),"
                " escalation_reason VARCHAR(32),"
                " ticket_id VARCHAR(32),"
                " created_at DATETIME,"
                " updated_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_starlink_episodes_device "
                "ON starlink_episodes(device_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_starlink_episodes_ticket_id "
                "ON starlink_episodes(ticket_id)"
            ))
    except OperationalError:
        pass  # Table/index already exists
    # Migration: service_monitors + service_check_episodes (service checks —
    # ping/TCP/HTTP monitors → tickets, 2026-08-25). NEW tables — create_all
    # above already creates them; the guarded CREATEs are idempotent belt-and-
    # suspenders no-ops. KEEP IN SYNC with models.ServiceMonitor/ServiceCheckEpisode.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS service_monitors ("
                " id INTEGER PRIMARY KEY,"
                " name VARCHAR(128) NOT NULL,"
                " check_type VARCHAR(16) DEFAULT 'ping' NOT NULL,"
                " target VARCHAR(256),"
                " target_device_id INTEGER,"
                " params JSON,"
                " interval_min INTEGER DEFAULT 5,"
                " fail_threshold INTEGER DEFAULT 3,"
                " recovery_ok INTEGER DEFAULT 3,"
                " notify BOOLEAN DEFAULT 1,"
                " enabled BOOLEAN DEFAULT 1,"
                " last_status VARCHAR(16) DEFAULT 'unknown',"
                " last_check_at DATETIME,"
                " last_error TEXT,"
                " fail_streak INTEGER DEFAULT 0,"
                " ok_streak INTEGER DEFAULT 0,"
                " created_at DATETIME,"
                " updated_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_service_monitors_enabled "
                "ON service_monitors(enabled)"
            ))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS service_check_episodes ("
                " id INTEGER PRIMARY KEY,"
                " monitor_id INTEGER NOT NULL,"
                " state VARCHAR(16) DEFAULT 'down',"
                " down_since DATETIME,"
                " last_event_at DATETIME,"
                " escalated VARCHAR(4) DEFAULT 'P2',"
                " escalation_reason VARCHAR(32),"
                " ticket_id VARCHAR(32),"
                " created_at DATETIME,"
                " updated_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_service_check_episodes_monitor "
                "ON service_check_episodes(monitor_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_service_check_episodes_ticket_id "
                "ON service_check_episodes(ticket_id)"
            ))
    except OperationalError:
        pass  # Tables/indexes already exist
    # Migration: scan_runs + findings (Network Optimization, P1). NEW tables —
    # create_all above already creates them; the guarded CREATEs are idempotent
    # belt-and-suspenders no-ops. KEEP IN SYNC with models.ScanRun/Finding.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS scan_runs ("
                " id INTEGER PRIMARY KEY,"
                " started_at DATETIME,"
                " finished_at DATETIME,"
                " scope JSON,"
                " status VARCHAR(16) DEFAULT 'queued',"
                " score FLOAT,"
                " summary TEXT,"
                " schema_version INTEGER DEFAULT 1,"
                " triggered_by VARCHAR(16) DEFAULT 'manual',"
                " created_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS findings ("
                " id INTEGER PRIMARY KEY,"
                " run_id INTEGER NOT NULL,"
                " finding_key VARCHAR(64) NOT NULL,"
                " category VARCHAR(16) NOT NULL,"
                " severity VARCHAR(12) NOT NULL,"
                " device_id INTEGER,"
                " interface VARCHAR(128),"
                " title VARCHAR(256) NOT NULL,"
                " detail TEXT,"
                " evidence JSON,"
                " created_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_scan_runs_status ON scan_runs(status)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_findings_run_id ON findings(run_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_findings_finding_key ON findings(finding_key)"
            ))
    except OperationalError:
        pass  # Tables/indexes already exist
    # Migration: findings.fix_ticket_id (optimize → ticket linkage, 08-19) —
    # a finding that has a fix ticket must stop showing as actionable. Guarded
    # ALTER + index (idempotent).
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE findings ADD COLUMN fix_ticket_id VARCHAR(32)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_findings_fix_ticket_id ON findings(fix_ticket_id)"
            ))
    except OperationalError:
        pass  # Column already exists
    # Migration: metrics (telemetry backbone, P0). NEW table — create_all above
    # already creates it; the guarded CREATEs are idempotent belt-and-suspenders
    # no-ops. KEEP IN SYNC with models.Metric.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS metrics ("
                " id INTEGER PRIMARY KEY,"
                " device_id INTEGER NOT NULL,"
                " metric VARCHAR(128) NOT NULL,"
                " ts DATETIME NOT NULL,"
                " value FLOAT NOT NULL"
                ")"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_metrics_device_metric_ts "
                "ON metrics(device_id, metric, ts)"
            ))
    except OperationalError:
        pass  # Table/index already exists
    # Migration: firmware management (maintenance_windows + device_firmware +
    # firmware_upgrades + pending_actions). NEW tables — create_all above
    # already creates them; the guarded CREATEs are idempotent belt-and-
    # suspenders no-ops. KEEP IN SYNC with the models.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS maintenance_windows ("
                " id INTEGER PRIMARY KEY,"
                " name VARCHAR(128) NOT NULL,"
                " mode VARCHAR(16) DEFAULT 'recurring',"
                " day VARCHAR(16) DEFAULT 'daily',"
                " hour INTEGER DEFAULT 3,"
                " duration_minutes INTEGER DEFAULT 60,"
                " when VARCHAR(32) DEFAULT '',"
                " enabled BOOLEAN DEFAULT 1,"
                " timezone VARCHAR(64) DEFAULT '',"
                " created_by VARCHAR(64),"
                " created_at DATETIME,"
                " updated_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS device_firmware ("
                " id INTEGER PRIMARY KEY,"
                " device_id INTEGER,"
                " mac_address VARCHAR(17) NOT NULL,"
                " name VARCHAR(128),"
                " device_type VARCHAR(32) DEFAULT 'unknown',"
                " model VARCHAR(64),"
                " ip VARCHAR(45),"
                " current_version VARCHAR(64) DEFAULT '',"
                " previous_version VARCHAR(64) DEFAULT '',"
                " available_version VARCHAR(64) DEFAULT '',"
                " upgradeable BOOLEAN DEFAULT 0,"
                " online BOOLEAN DEFAULT 0,"
                " prestaged_version VARCHAR(64) DEFAULT '',"
                " last_result VARCHAR(16) DEFAULT '',"
                " last_upgrade_at DATETIME,"
                " last_error TEXT,"
                " updated_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_device_firmware_mac "
                "ON device_firmware(mac_address)"
            ))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS firmware_upgrades ("
                " id INTEGER PRIMARY KEY,"
                " device_id INTEGER,"
                " mac_address VARCHAR(17) NOT NULL,"
                " device_name VARCHAR(128),"
                " device_type VARCHAR(32) DEFAULT 'unknown',"
                " from_version VARCHAR(64) DEFAULT '',"
                " to_version VARCHAR(64) DEFAULT '',"
                " window_id INTEGER,"
                " status VARCHAR(16) DEFAULT 'staging',"
                " stage_started_at DATETIME,"
                " stage_deadline DATETIME,"
                " verify_attempts INTEGER DEFAULT 0,"
                " rollback_attempted BOOLEAN DEFAULT 0,"
                " durations JSON,"
                " error TEXT,"
                " triggered_by VARCHAR(16) DEFAULT 'auto',"
                " started_at DATETIME,"
                " finished_at DATETIME,"
                " created_at DATETIME,"
                " updated_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS pending_actions ("
                " id INTEGER PRIMARY KEY,"
                " kind VARCHAR(16) DEFAULT 'approval',"
                " title VARCHAR(256) NOT NULL,"
                " detail TEXT,"
                " device_id INTEGER,"
                " mac_address VARCHAR(17),"
                " device_name VARCHAR(128),"
                " device_type VARCHAR(32) DEFAULT 'unknown',"
                " firmware_from VARCHAR(64) DEFAULT '',"
                " firmware_to VARCHAR(64) DEFAULT '',"
                " status VARCHAR(16) DEFAULT 'pending',"
                " auto BOOLEAN DEFAULT 0,"
                " required_role VARCHAR(16) DEFAULT 'technician',"
                " resolved_by VARCHAR(64),"
                " resolved_note TEXT,"
                " extra JSON,"
                " created_at DATETIME,"
                " updated_at DATETIME,"
                " resolved_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_firmware_upgrades_status "
                "ON firmware_upgrades(status)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_pending_actions_status "
                "ON pending_actions(status)"
            ))
    except OperationalError:
        pass  # Tables/indexes already exist
