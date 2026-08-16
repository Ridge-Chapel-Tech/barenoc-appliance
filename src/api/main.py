import os
import json
import logging
import secrets
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from sqlalchemy.orm import Session

from datetime import datetime
from database import init_db, SessionLocal, get_db
from models import User, Device, Ticket, AuditLog
from schemas import generate_ticket_id, generate_event_id, compute_hash
from auth import hash_password, decode_token, require_page_session, require_role
from routes import auth, tickets, devices, dashboard, jobs, admin, unifi_sync, system, settings, users, branding, chat, client, device_certs, device_agent, onboard, updates, setup, support
from oidc import oidc_config, oauth_login_config
from version import APP_VERSION
from ratelimit import RateLimitMiddleware

logging.basicConfig(level=os.getenv("LOG_LEVEL", "info").upper(),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    force=True)
logger = logging.getLogger("barenoc")


def seed_demo_data():
    """Create demo data if database is empty."""
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return  # Already seeded

        logger.info("Seeding demo data...")

        # Admin user
        # First-login passwords: ADMIN_PASSWORD / OPERATOR_PASSWORD env, else a
        # random one generated here (logged once at first boot — never in code).
        def _seed_pw(env_key: str, label: str) -> str:
            pw = os.getenv(env_key)
            if not pw:
                pw = secrets.token_urlsafe(12)
                logger.warning("No %s set — generated a random first-login password for %s",
                               env_key, label)
                logger.warning("Seeded %s password: %s (change it on first login)", label, pw)
            return pw

        admin = User(
            username=os.getenv("ADMIN_USERNAME", "admin"),
            email="admin@barenoc.local",
            hashed_password=hash_password(_seed_pw("ADMIN_PASSWORD", "admin")),
            role="admin",
            is_active=True,
            must_change_password=True,
        )
        db.add(admin)

        # Operator user (demo) — same treatment: random unless OPERATOR_PASSWORD env.
        operator = User(
            username="operator",
            email="operator@barenoc.local",
            hashed_password=hash_password(_seed_pw("OPERATOR_PASSWORD", "operator")),
            role="operator",
            is_active=True,
            must_change_password=True,
        )
        db.add(operator)

        # Demo devices
        devices = [
            Device(
                name="Main Gateway",
                hostname="ucg-ultra",
                ip_address="10.0.10.1",
                device_type="gateway",
                vendor="Ubiquiti",
                model="UCG-Ultra",
                status="online",
                tags=["core", "wan"],
            ),
            Device(
                name="Office Switch",
                hostname="sw-office",
                ip_address="10.0.10.5",
                device_type="switch",
                vendor="Ubiquiti",
                model="USW-Lite-8-PoE",
                status="online",
                tags=["access"],
            ),
            Device(
                name="Main AP",
                hostname="ap-main",
                ip_address="10.0.10.10",
                device_type="ap",
                vendor="Ubiquiti",
                model="U6-Pro",
                status="online",
                tags=["wireless"],
            ),
            Device(
                name="NAS Storage",
                hostname="nas-01",
                ip_address="10.0.10.20",
                device_type="server",
                vendor="Synology",
                model="DS923+",
                status="online",
                tags=["storage"],
            ),
            Device(
                name="Backup Server",
                hostname="backup-01",
                ip_address="10.0.10.21",
                device_type="server",
                vendor="Custom",
                model="Proxmox Backup",
                status="warning",
                tags=["backup"],
            ),
            Device(
                name="Dev Workstation",
                hostname="dev-01",
                ip_address="10.0.10.50",
                device_type="workstation",
                vendor="Dell",
                model="OptiPlex 7080",
                status="offline",
                tags=["engineering"],
            ),
        ]
        # Demo fleet + tickets ONLY when explicitly requested (SEED_DEMO=true —
        # the SaaS/demo deployment). A real appliance must start clean: the
        # wizard's adoption flow is the front door, not a fake 10.0.10.x fleet.
        demo = os.getenv("SEED_DEMO", "").strip().lower() in ("1", "true", "yes")
        if demo:
            db.add_all(devices)
            db.flush()  # Get IDs

        # Demo tickets
        tickets = [
            Ticket(
                ticket_id="TKT-20250730-001",
                title="High latency on WAN link",
                description="WAN latency spiked to 350ms. Gateway needs investigation.",
                priority="P2",
                status="in_progress",
                source="auto",
                submitter_id=admin.id,
                assigned_to="system",
                target_device_id=devices[0].id,
            ),
            Ticket(
                ticket_id="TKT-20250730-002",
                title="Firmware update available for Office Switch",
                description="New firmware 6.6.55 available for USW-Lite-8-PoE. Fixes CVE-2025-1234.",
                priority="P3",
                status="awaiting_approval",
                source="auto",
                submitter_id=admin.id,
                assigned_to="admin",
                target_device_id=devices[1].id,
            ),
            Ticket(
                ticket_id="TKT-20250730-003",
                title="Dev Workstation offline",
                description="Dev-01 has not responded to ping for 2 hours. Check power or network.",
                priority="P2",
                status="open",
                source="auto",
                submitter_id=admin.id,
                target_device_id=devices[5].id,
            ),
            Ticket(
                ticket_id="TKT-20250730-004",
                title="Morning network health summary",
                description="All devices operational. 1 warning (Backup Server disk at 82%).",
                priority="P4",
                status="completed",
                source="auto",
                submitter_id=admin.id,
                assigned_to="system",
                resolution="Routine. Disk usage within thresholds.",
                resolved_at=datetime(2025, 7, 30, 8, 0, 0),
            ),
        ]
        if demo:
            db.add_all(tickets)
            # Seed audit log entry
            audit = AuditLog(
                event_id=generate_event_id(),
                event_type="system_start",
                actor="system",
                data={"action": "seed_demo_data", "users": 2, "devices": 6, "tickets": 4},
                sha256_hash=compute_hash({"action": "seed_demo_data"}),
            )
            db.add(audit)
        db.commit()
        logger.info("Seeded 2 users" + (f", {len(devices)} demo devices, {len(tickets)} demo tickets" if demo else " (no demo data)"))

    except Exception as e:
        db.rollback()
        logger.error(f"Seed failed: {e}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle."""
    logger.info("Starting BareNOC API...")
    init_db()
    seed_demo_data()
    from alerting import start_alert_engine
    start_alert_engine()  # background: device-down alerts + daily digest
    from routes.settings import _write_provider_secret
    _write_provider_secret()  # sync the pi-agent provider key at startup
    from routes.settings import _remount_net_backup
    _remount_net_backup()  # reconnect the NAS backup share (best-effort)
    yield
    logger.info("Shutting down BareNOC API...")


app = FastAPI(
    title="BareNOC API",
    version=APP_VERSION,
    lifespan=lifespan,
    # Security: don't expose API schema/docs unauthenticated
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Rate limiting (M2-T9) — in-memory fixed-window per client IP, rules in
# .env (RATE_LIMIT_LOGIN / _CHAT / _API). Env re-read every 15s, so .env
# edits hot-reload. 429 + Retry-After on exceed.
app.add_middleware(RateLimitMiddleware)

# Static files
api_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(api_dir, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Templates
templates = Jinja2Templates(directory=os.path.join(api_dir, "templates"))

# Register API routes
app.include_router(auth.router)
app.include_router(tickets.router)
app.include_router(devices.router)
app.include_router(dashboard.router)
app.include_router(jobs.router)
app.include_router(admin.router)
app.include_router(unifi_sync.router)
app.include_router(system.router)
app.include_router(settings.router)
app.include_router(users.router)
app.include_router(updates.router)
app.include_router(setup.router)
app.include_router(branding.router)
app.include_router(chat.router)
app.include_router(client.router)
app.include_router(device_certs.router)
app.include_router(device_agent.router)
app.include_router(onboard.router)
app.include_router(support.router)


# ── Health Check ──

@app.get("/api/v1/health")
def health_check():
    return {
        "status": "healthy",
        "version": APP_VERSION,
        "service": "barenoc-api",
    }


# ── Web UI Routes ──

def _session_valid(request: Request, db: Session) -> bool:
    """True when the browser's session cookie carries a valid, unexpired token."""
    token = request.cookies.get("access_token")
    if not token:
        return False
    payload = decode_token(token)
    if not payload or not payload.get("sub"):
        return False
    user = db.query(User).filter(User.username == payload["sub"]).first()
    return bool(user and user.is_active)


def _first_run_setup() -> bool:
    """True until the /setup wizard completes (fresh install)."""
    try:
        from routes.settings import _read_env_file
        return str(_read_env_file().get("SETUP_COMPLETE", "")).strip().lower() \
            not in ("1", "true", "yes")
    except Exception:
        return False


def _account_claimed(db: Session) -> bool:
    """True once the wizard's set-your-own-admin step has been completed
    (an admin exists and is no longer forced to change their password).
    Until then / and /login route to the wizard (it IS the front door);
    after that the login page must be reachable — a mid-wizard session
    expiry would otherwise trap the user in a /setup <-> /login loop."""
    admin = db.query(User).filter(User.role == "admin").order_by(User.id.asc()).first()
    return bool(admin and not admin.must_change_password)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    if _session_valid(request, db):
        return RedirectResponse(url="/dashboard")
    if _first_run_setup() and not _account_claimed(db):
        # first boot before the admin is claimed: the wizard IS the front door
        return RedirectResponse(url="/setup")
    return templates.TemplateResponse("login.html", {
        "request": request,
        "oidc_enabled": oidc_config().get("enabled"),
        "oauth": oauth_login_config(),
    })


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if _session_valid(request, db):
        return RedirectResponse(url="/dashboard")
    if _first_run_setup() and not _account_claimed(db):
        return RedirectResponse(url="/setup")
    return templates.TemplateResponse("login.html", {
        "request": request,
        "oidc_enabled": oidc_config().get("enabled"),
        "oauth": oauth_login_config(),
    })


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    """Mobile chat front door — home users & tenants talk to the Queue
    Manager here. Public page: the client JS handles login/register; new
    self-registered accounts are role=tenant (see /api/v1/auth/register)."""
    site = ""
    try:
        from llm_providers import read_env_file
        site = (read_env_file().get("CUSTOMER_NAME") or "").strip()
    except Exception:
        pass
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "site_name": site or "BareNOC",
    })


@app.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request, _: User = Depends(require_page_session)):
    return templates.TemplateResponse("change-password.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, _: User = Depends(require_page_session)):
    setup_complete = True
    try:
        from routes.settings import _read_env_file
        env = _read_env_file()
        setup_complete = str(env.get("SETUP_COMPLETE", "")).strip().lower() in ("1", "true", "yes")
    except Exception:
        pass
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "setup_complete": setup_complete,
    })


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    """First-run wizard: set-your-own admin account → LLM → TZ → site → email
    → autonomy → backups → adopt first device → share the chat URL. Public
    while the setup is incomplete (no admin session exists yet); after
    SETUP_COMPLETE it just renders (the APIs are admin-gated)."""
    return templates.TemplateResponse("setup.html", {"request": request})


@app.get("/tickets", response_class=HTMLResponse)
def tickets_page(request: Request, _: User = Depends(require_page_session)):
    return templates.TemplateResponse("tickets.html", {"request": request})


@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, _: User = Depends(require_page_session)):
    return templates.TemplateResponse("devices.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, _: User = Depends(require_page_session)):
    return templates.TemplateResponse("admin.html", {"request": request})


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request, _: User = Depends(require_page_session)):
    return templates.TemplateResponse("system.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _: User = Depends(require_role("admin"))):
    return templates.TemplateResponse("settings.html", {"request": request})


# ── Dev / build-status board (temp snapshot tooling) ────────────

@app.get("/dev/tasks", response_class=HTMLResponse)
def dev_tasks_page(request: Request, _: User = Depends(require_page_session)):
    devdata = os.path.join(api_dir, "devdata")
    with open(os.path.join(devdata, "tasks.json")) as f:
        board = json.load(f)
    with open(os.path.join(devdata, "task_sat_map.json")) as f:
        sat_map = json.load(f)
    return templates.TemplateResponse("dev_tasks.html", {
        "request": request, "stages": board["stages"],
        "sat_map": sat_map, "updated": board.get("updated", ""),
    })


@app.get("/dev/sat", response_class=HTMLResponse)
def dev_sat_page(request: Request, _: User = Depends(require_page_session)):
    devdata = os.path.join(api_dir, "devdata")
    with open(os.path.join(devdata, "sat.json")) as f:
        data = json.load(f)
    sections = [s for s in data["sections"] if s.get("tests")]
    return templates.TemplateResponse("dev_sat.html", {
        "request": request, "sections": sections,
    })


# ── User Wiki ────────────────────────────────────────────────────

WIKI_PAGES = [
    ("index", "Welcome"),
    ("workflows", "Workflows"),
    ("getting-started", "Getting Started"),
    ("tickets", "Tickets"),
    ("devices", "Devices"),
    ("discovery", "Network Discovery"),
    ("chat-client", "Chat Client"),
    ("security", "Security"),
    ("autonomy", "Autonomy Policy"),
    ("backups", "Backups"),
]
WIKI_DIR = os.path.join(api_dir, "wiki")


def _render_wiki(request: Request, slug: str):
    if slug not in [s for s, _ in WIKI_PAGES]:
        slug = "index"
    title = dict(WIKI_PAGES).get(slug, "Wiki")
    path = os.path.join(WIKI_DIR, f"{slug}.md")
    raw = "# Not found"
    if os.path.exists(path):
        with open(path) as f:
            raw = f.read()
    import markdown as _md
    body_html = _md.markdown(raw, extensions=["fenced_code", "tables"])
    return templates.TemplateResponse(
        "wiki.html",
        {"request": request, "pages": WIKI_PAGES, "active": slug,
         "title": title, "body_html": body_html},
    )


@app.get("/wiki", response_class=HTMLResponse)
def wiki_index(request: Request):
    return _render_wiki(request, "index")


@app.get("/wiki/{slug}", response_class=HTMLResponse)
def wiki_page(slug: str, request: Request):
    return _render_wiki(request, slug)


@app.get("/downloads", response_class=HTMLResponse)
def downloads_page(request: Request):
    platforms = [
        {"key": "linux", "label": "Linux", "icon": "🐧",
         "filename": f"barenoc-chat-{APP_VERSION}-linux.tar.gz",
         "blurb": "All distros with apt, dnf or pacman (Ubuntu, Fedora, Arch…).",
         "steps": [
             "Install Python 3.8+ with tkinter (e.g. sudo apt install python3-tk).",
             "Extract the archive and run ./install.sh (adds the barenoc-chat launcher).",
             "Run barenoc-chat and sign in.",
         ]},
        {"key": "windows", "label": "Windows", "icon": "🪟",
         "filename": f"barenoc-chat-{APP_VERSION}-windows.zip",
         "blurb": "Windows 10/11 with Python installed from python.org.",
         "steps": [
             "Install Python 3.8+ and tick 'Add to PATH'.",
             "Extract the zip and run run.bat (or: python barenoc_chat.py).",
         ]},
        {"key": "macos", "label": "macOS", "icon": "🍎",
         "filename": f"barenoc-chat-{APP_VERSION}-macos.zip",
         "blurb": "macOS with python3 + tkinter (python.org installer includes tk).",
         "steps": [
             "Install python3 (python.org installer includes tkinter).",
             "Extract the zip and run run.command (chmod +x first if needed).",
         ]},
    ]
    return templates.TemplateResponse(
        "downloads.html",
        {"request": request, "version": APP_VERSION, "platforms": platforms},
    )
