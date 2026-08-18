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
