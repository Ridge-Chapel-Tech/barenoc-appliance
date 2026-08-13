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
