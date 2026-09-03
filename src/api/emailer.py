"""Email module — multi-transport for BareNOC alerts & digests.

Transport selection (per send, read from the .env file — hot-reloadable):

  1. Gmail OAuth2 REST API (preferred) — when GOOGLE_CLIENT_ID +
     GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN are set:
       * exchange the refresh token for a short-lived access token at
         https://oauth2.googleapis.com/token
       * build an RFC 2822 MIME message, encode it URL-safe base64
         (no '=' padding), and POST it to
         https://gmail.googleapis.com/gmail/v1/users/me/messages/send
         with a Bearer token.
     This works for accounts where Google has retired Less-Secure-Apps /
     standard app passwords (OAuth 2.0 only).

  2. SMTP (fallback for other providers, or OAuth2-capable SMTP relays) —
     SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD with STARTTLS.

  3. Vendor-managed notify (DEFAULT — out-of-the-box) — the appliance POSTs to
     the vendor `notify` edge function (shared NOTIFY_TOKEN in the 0600
     /opt/barenoc/volumes/secrets/notify.json, the forum-submit pattern), which
     sends via Resend from noreply@notify.barenoc.com. Used when no SMTP/Gmail
     is configured, so alerts/digests/EOD/check-ins work with zero setup.

Multi-recipient: ALERT_EMAIL may be comma/space/semicolon separated.
All sending is best-effort: failures are logged, never raised.
"""

import base64
import html
import json
import logging
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Union

from worknotes import parse_notes

logger = logging.getLogger("barenoc-email")

ENV_PATH = "/opt/barenoc/.env"

# Strict TLS verification for Google endpoints (never relax this in prod —
# tests may point it at a local mock).
SSL_CONTEXT = ssl.create_default_context()

GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

NOTIFY_SECRET_FILE = "/opt/barenoc/volumes/secrets/notify.json"
NOTIFY_URL_DEFAULT = "https://eqivajpnvansfpxkegpr.supabase.co/functions/v1/notify"
NOTIFY_TIMEOUT = 30

_EMAIL_KEYS = (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL",
    "ALERT_RECIPIENTS", "DIGEST_RECIPIENTS", "EOD_RECIPIENTS",
    "REPORT_MORNING_DIGEST", "REPORT_EOD_SUMMARY", "DIGEST_HOUR", "EOD_HOUR",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "GOOGLE_SENDER",
    "EMAIL_TRANSPORT", "EMAIL_REPLY_TO", "CUSTOMER_NAME", "SITE_ID",
)


def _read_email_env() -> dict:
    env = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception:
        pass
    for k in _EMAIL_KEYS:
        if k not in env and k in os.environ:
            env[k] = os.environ[k]
    return env


def gmail_configured() -> bool:
    env = _read_email_env()
    return bool(env.get("GOOGLE_CLIENT_ID") and env.get("GOOGLE_CLIENT_SECRET")
                and env.get("GOOGLE_REFRESH_TOKEN"))


def _read_notify_secret() -> dict:
    """Read the 0600 notify config {url, token} (never in .env)."""
    try:
        with open(NOTIFY_SECRET_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_notify_config() -> dict:
    """Return {url, token} for the vendor notify transport. The URL has a
    default (the shared Supabase project) and can be overridden in .env via
    NOTIFY_URL; the token lives only in the 0600 secret file."""
    cfg = _read_notify_secret()
    env = _read_email_env()
    url = (cfg.get("url") or "").strip() or env.get("NOTIFY_URL", "").strip() \
        or NOTIFY_URL_DEFAULT
    return {"url": url, "token": (cfg.get("token") or "").strip()}


def vendor_configured() -> bool:
    """True when the vendor notify transport is usable (token + URL present)."""
    return bool(load_notify_config()["token"])


def _smtp_creds(env: dict) -> bool:
    return bool(env.get("SMTP_HOST") and env.get("SMTP_USER") and env.get("SMTP_PASSWORD"))


def transport_mode(env: Optional[dict] = None) -> str:
    """Resolve the effective email transport: 'vendor' or 'smtp'.

    EMAIL_TRANSPORT is the explicit Settings choice (vendor-managed vs your own
    SMTP). When unset (fresh install or upgrade), the self-hosted override wins
    if SMTP/Gmail is configured — otherwise the out-of-the-box vendor transport
    is the default."""
    env = env if env is not None else _read_email_env()
    choice = (env.get("EMAIL_TRANSPORT") or "").strip().lower()
    if choice in ("vendor", "smtp"):
        return choice
    if gmail_configured() or _smtp_creds(env):
        return "smtp"
    return "vendor"


def smtp_configured() -> bool:
    """True when ANY email transport is configured — Gmail OAuth, SMTP, or the
    vendor-managed notify transport (the out-of-the-box default). This is the
    gate the alert engine's email half uses: vendor-managed counts as
    configured so alerts/digests/EOD/check-ins run with zero setup."""
    env = _read_email_env()
    if transport_mode(env) == "smtp":
        return gmail_configured() or _smtp_creds(env)
    return vendor_configured()


def _split_emails(value: str) -> list:
    """Parse a comma/space/semicolon separated email list into a clean, de-duped list."""
    out = []
    for part in re.split(r"[,;\s]+", value or ""):
        p = part.strip()
        if p and "@" in p and p not in out:
            out.append(p)
    return out


def get_alert_recipients() -> list:
    """Recipients for immediate alerts (P1/P2 tickets, device down/recovery).
    ALERT_RECIPIENTS wins; falls back to the legacy ALERT_EMAIL key."""
    env = _read_email_env()
    value = env.get("ALERT_RECIPIENTS", "") or env.get("ALERT_EMAIL", "")
    return _split_emails(value)


def notify_customer_action(db, ticket) -> None:
    """Email the ticket's submitter (or the alert recipients) that their input
    is needed. Used whenever a ticket moves to Customer Action — both from the
    worker's pipeline and the agent-result path (jobs.py). Best-effort: runs in
    a background thread, never raises."""
    import threading
    try:
        recipients = []
        if getattr(ticket, "submitter_id", None):
            from models import User
            u = db.query(User).filter(User.id == ticket.submitter_id).first()
            if u and u.email and not u.email.lower().endswith(".local"):
                recipients = [u.email]
        if not recipients:
            recipients = get_recipients("alerts")
        if not recipients:
            return
        question = ""
        notes = parse_notes(ticket.work_notes)
        for n in reversed(notes):
            if isinstance(n, dict) and n.get("event") == "customer_input":
                question = n.get("detail") or ""
                break
        subject = f"BareNOC needs your input on {ticket.ticket_id}"
        title = ticket.title or "(untitled)"
        body_text = (f"Your ticket {ticket.ticket_id} — '{title}' needs your input.\n\n"
                     f"The AI Technician wrote:\n{question or '(see ticket)'}\n\n"
                     f"Reply to this ticket in the BareNOC web UI (or chat client) and it "
                     f"will continue automatically.")
        body_html = (f"<p>Your ticket <b>{ticket.ticket_id}</b> — '{html.escape(title)}' needs your input.</p>"
                     f"<blockquote style='border-left:3px solid #ccc;margin:8px 0;padding:4px 12px;color:#444'>"
                     f"{html.escape(question or '(see ticket)')}</blockquote>"
                     f"<p>Reply to this ticket in the BareNOC web UI (or chat client) and it will "
                     f"continue automatically.</p>")

        def _send():
            try:
                send_email(recipients, subject, body_html=body_html, body_text=body_text)
            except Exception:
                logging.getLogger("barenoc-email").exception("customer-action email failed")
        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        logging.getLogger("barenoc-email").exception("notify_customer_action failed")


def get_recipients(kind: str = "alerts") -> list:
    """Recipients for a specific email type.

    kind: "alerts" (immediate), "digest" (morning digest), "eod" (end-of-day summary).
    Each has its own list key; blanks fall back to ALERT_RECIPIENTS, then ALERT_EMAIL.
    """
    env = _read_email_env()
    key = {"alerts": "ALERT_RECIPIENTS", "digest": "DIGEST_RECIPIENTS", "eod": "EOD_RECIPIENTS"}.get(kind, "ALERT_RECIPIENTS")
    value = env.get(key, "")
    if not value:
        value = env.get("ALERT_RECIPIENTS", "") or env.get("ALERT_EMAIL", "")
    return _split_emails(value)


# ── Gmail OAuth2 transport ──────────────────────────────────────

def _gmail_access_token(env: dict) -> tuple:
    """Exchange the refresh token for a short-lived access token.
    Returns (token, error)."""
    body = urllib.parse.urlencode({
        "client_id": env["GOOGLE_CLIENT_ID"],
        "client_secret": env["GOOGLE_CLIENT_SECRET"],
        "refresh_token": env["GOOGLE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        GMAIL_TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT)
        data = json.loads(resp.read().decode())
        token = data.get("access_token")
        if not token:
            return None, f"no access_token in token response: {list(data.keys())}"
        return token, None
    except urllib.error.HTTPError as e:
        return None, f"token endpoint HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return None, str(e)


def _send_via_gmail(env: dict, to: list, msg) -> tuple:
    token, err = _gmail_access_token(env)
    if err:
        return False, f"OAuth token exchange failed: {err}"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    req = urllib.request.Request(
        GMAIL_SEND_URL,
        data=json.dumps({"raw": raw}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT)
        logger.info(f"Gmail API send OK (to {len(to)} recipient(s))")
        return True, None
    except urllib.error.HTTPError as e:
        detail = f"Gmail API HTTP {e.code}: {e.read().decode()[:300]}"
        logger.error(f"Gmail send failed: {detail}")
        return False, detail
    except Exception as e:
        logger.error(f"Gmail send failed: {e}")
        return False, str(e)


# ── SMTP transport (fallback) ───────────────────────────────────

def _send_via_smtp(env: dict, to: list, msg, from_addr: str) -> tuple:
    host = env.get("SMTP_HOST", "")
    port = int(env.get("SMTP_PORT", "587") or 587)
    user = env.get("SMTP_USER", "")
    password = env.get("SMTP_PASSWORD", "")
    try:
        server = smtplib.SMTP(host, port, timeout=15)
        server.starttls()
        server.login(user, password)
        server.sendmail(from_addr, to, msg.as_string())
        server.quit()
        logger.info(f"SMTP send OK (to {len(to)} recipient(s))")
        return True, None
    except Exception as e:
        logger.error(f"SMTP send failed: {e}")
        return False, str(e)


# ── Vendor-managed notify transport (default, out-of-the-box) ──

def _vendor_from_name(env: dict) -> str:
    """Display name for the vendor From — the appliance's site name."""
    return (env.get("CUSTOMER_NAME") or env.get("SITE_ID") or "").strip() or "BareNOC"


def _vendor_nonce(to: list, subject: str, text: str) -> str:
    """Deterministic idempotency nonce: stable for ~10 min so a retry of the
    same email dedupes at the notify fn, but a later identical email gets a
    fresh nonce."""
    import hashlib
    import time
    bucket = int(time.time() // 600)
    h = hashlib.sha256()
    h.update("|".join(sorted(to)).encode())
    h.update(b"|")
    h.update((subject or "").encode())
    h.update(b"|")
    h.update((text or "").encode())
    return f"{h.hexdigest()[:32]}-{bucket}"


def _send_via_vendor(env: dict, to: list, subject: str,
                     body_text: str, body_html: str) -> tuple:
    cfg = load_notify_config()
    if not cfg["token"]:
        return False, "vendor notify token not configured (Settings → Email/Notifications)"
    reply_to = (env.get("EMAIL_REPLY_TO") or "").strip()
    payload = {
        "to": to,
        "subject": subject,
        "text": body_text,
        "from_name": _vendor_from_name(env),
        "nonce": _vendor_nonce(to, subject, body_text),
    }
    if body_html:
        payload["html"] = body_html
    if reply_to:
        payload["reply_to"] = reply_to
    req = urllib.request.Request(
        cfg["url"],
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {cfg['token']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=NOTIFY_TIMEOUT, context=SSL_CONTEXT)
        data = json.loads(resp.read().decode())
        if data.get("ok"):
            logger.info("Vendor notify send OK (to %d recipient(s), id %s)",
                        len(to), data.get("id", ""))
            return True, None
        note = data.get("note") or "vendor notify rejected the send"
        logger.error(f"Vendor notify failed: {note}")
        return False, note
    except urllib.error.HTTPError as e:
        try:
            note = json.loads(e.read().decode()).get("note") or f"HTTP {e.code}"
        except Exception:
            note = f"HTTP {e.code}"
        logger.error(f"Vendor notify failed: {note}")
        return False, note
    except Exception as e:
        logger.error(f"Vendor notify failed: {e}")
        return False, str(e)


# ── public API ──────────────────────────────────────────────────

def send_email(
    to: Union[str, list],
    subject: str,
    body_html: Optional[str] = None,
    body_text: Optional[str] = None,
    overrides: Optional[dict] = None,
) -> tuple:
    """Send one email. `to` may be a string (comma-separated ok) or a list.

    Transport (per send, hot-read from .env):
      - 'smtp'  → Gmail OAuth2 when configured, else SMTP (your own domain).
      - 'vendor' (default) → the vendor-managed notify edge fn (Resend).
    `overrides` (from the settings form, e.g. test-email before saving) is
    merged over the .env config. Returns (ok: bool, error: Optional[str]).
    Never raises.
    """
    env = _read_email_env()
    if overrides:
        for k, v in overrides.items():
            if v:
                env[k] = str(v)

    if isinstance(to, str):
        to = [t.strip() for t in re.split(r"[,;\s]+", to) if t.strip()]
    if not to:
        return False, "no recipients"

    if not body_text:
        body_text = re.sub(r"<[^>]+>", " ", body_html or "")
        body_text = html.unescape(body_text)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = ", ".join(to)
    from_addr = env.get("GOOGLE_SENDER") or env.get("SMTP_USER", "")
    if from_addr:
        msg["From"] = from_addr
    if (env.get("EMAIL_REPLY_TO") or "").strip():
        msg["Reply-To"] = env["EMAIL_REPLY_TO"].strip()
    msg.attach(MIMEText(body_text, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))

    mode = transport_mode(env)
    if mode == "smtp":
        if gmail_configured() or (env.get("GOOGLE_CLIENT_ID") and env.get("GOOGLE_REFRESH_TOKEN")):
            return _send_via_gmail(env, to, msg)
        if not _smtp_creds(env):
            logger.warning("Email send skipped: 'your own SMTP' selected but not configured")
            return False, "no email transport configured ('your own SMTP' selected but SMTP/Gmail not set)"
        return _send_via_smtp(env, to, msg, from_addr or env.get("SMTP_USER", ""))
    return _send_via_vendor(env, to, subject, body_text, body_html)


def alert_html(title: str, rows: list) -> str:
    """Build a simple styled HTML body from a title + [(label, value)] rows."""
    parts = [f"<h2 style='color:#1d4ed8;margin:0 0 12px'>{html.escape(title)}</h2>", "<table>"]
    for label, value in rows:
        parts.append(
            f"<tr><td style='padding:4px 12px 4px 0;color:#555;vertical-align:top'>"
            f"{html.escape(str(label))}</td>"
            f"<td style='padding:4px 0;vertical-align:top'>{value}</td></tr>"
        )
    parts.append("</table>")
    parts.append("<p style='color:#888;font-size:12px;margin-top:16px'>— BareNOC</p>")
    return "".join(parts)
