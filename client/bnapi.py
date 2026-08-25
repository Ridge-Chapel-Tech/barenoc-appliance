"""BareNOC Chat client — REST API wrapper (Python stdlib only).

Talks to the BareNOC queue manager's public API:

    POST /api/v1/auth/login                 account sign-on
    GET  /api/v1/auth/me                    whoami
    GET  /api/v1/tickets                    list tickets (status updates / logs)
    GET  /api/v1/tickets/{id}               single ticket detail
    POST /api/v1/tickets                    open a ticket
    POST /api/v1/tickets/{id}/notes         append a chat comment to a ticket
    GET  /api/v1/devices                    device buddy list
    GET  /api/v1/system/status              queue manager / host status
    GET  /api/v1/dashboard/stats            dashboard snapshot

The BareNOC appliance uses a self-signed cert, so SSL verification is
deliberately relaxed (LAN trust model). Plain http is also accepted.
"""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request


class APIError(Exception):
    """Raised for HTTP errors and connection failures."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class BareNOCClient:
    """Minimal JSON REST client for the BareNOC API."""

    def __init__(self, server: str, token: str = None, timeout: float = 12.0):
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ── low level ────────────────────────────────────────────────

    def _request(self, method: str, path: str, body: dict = None):
        url = self.server + path
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context()) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = "HTTP error"
            try:
                detail = json.loads(e.read().decode()).get("detail", detail)
            except Exception:
                pass
            raise APIError(e.code, detail) from e
        except urllib.error.URLError as e:
            raise APIError(0, f"Cannot reach {self.server}: {e.reason}") from e

    def _get(self, path: str, params: dict = None):
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        return self._request("GET", path)

    # ── auth ─────────────────────────────────────────────────────

    def login(self, username: str, password: str) -> dict:
        data = self._request(
            "POST", "/api/v1/auth/login",
            {"username": username, "password": password},
        )
        self.token = data["access_token"]
        return data

    def me(self) -> dict:
        return self._get("/api/v1/auth/me")

    def change_password(self, current: str, new: str) -> dict:
        return self._request(
            "POST", "/api/v1/auth/change-password",
            {"current_password": current, "new_password": new},
        )

    # ── tickets ──────────────────────────────────────────────────

    def tickets(self, status: str = None, priority: str = None,
                limit: int = 100, offset: int = 0) -> dict:
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if priority:
            params["priority"] = priority
        return self._get("/api/v1/tickets", params)

    def ticket(self, ticket_id: str) -> dict:
        return self._get(f"/api/v1/tickets/{ticket_id}")

    def create_ticket(self, title: str, description: str = None,
                      priority: str = "P3", target_device_id: int = None) -> dict:
        body = {"title": title, "description": description or "", "priority": priority}
        if target_device_id is not None:
            body["target_device_id"] = target_device_id
        return self._request("POST", "/api/v1/tickets", body)

    def add_note(self, ticket_id: str, message: str) -> dict:
        """Chat comment on an existing ticket (requires the /notes endpoint)."""
        return self._request("POST", f"/api/v1/tickets/{ticket_id}/notes",
                             {"message": message})

    def update_ticket(self, ticket_id: str, **fields) -> dict:
        """Update a ticket: status, priority, assigned_to, resolution."""
        return self._request("PATCH", f"/api/v1/tickets/{ticket_id}", fields)

    # ── devices ──────────────────────────────────────────────────

    def devices(self, limit: int = 500, offset: int = 0) -> dict:
        return self._get("/api/v1/devices", {"limit": limit, "offset": offset})

    def device(self, device_id: int) -> dict:
        return self._get(f"/api/v1/devices/{device_id}")

    # ── system / dashboard ───────────────────────────────────────

    def system_status(self) -> dict:
        return self._get("/api/v1/system/status")

    def dashboard_stats(self) -> dict:
        return self._get("/api/v1/dashboard/stats")

    # ── tech-to-tech chat ────────────────────────────────────────

    def llm_usage(self, days: int = 7) -> dict:
        """LLM token/cost usage (admin only)."""
        return self._get("/api/v1/admin/llm-usage", {"days": days})

    def networks(self) -> dict:
        """VLANs/subnets + SSIDs from the UniFi controller (read-only)."""
        return self._get("/api/v1/unifi/networks")

    def chat_users(self) -> dict:
        """Buddy list: active users I can message."""
        return self._get("/api/v1/chat/users")

    def chat_conversations(self) -> dict:
        """Threads involving me, with last message + unread count."""
        return self._get("/api/v1/chat/conversations")

    def chat_messages(self, with_username: str) -> dict:
        """Full thread with another user (read-only)."""
        return self._get("/api/v1/chat/messages", {"with_username": with_username})

    def chat_mark_read(self, with_username: str) -> dict:
        """Mark a thread from another user as read (POST — read-state is a
        write, so it's no longer a side effect of fetching messages)."""
        return self._request("POST", "/api/v1/chat/messages/read",
                             {"with_username": with_username})

    def chat_send(self, to_username: str, body: str) -> dict:
        return self._request("POST", "/api/v1/chat/messages",
                             {"to_username": to_username, "body": body})
