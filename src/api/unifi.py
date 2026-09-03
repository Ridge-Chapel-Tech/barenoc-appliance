"""UniFi Controller API client for auto-discovering devices.

Supports both controller generations:
  * legacy UniFi Network controllers  — `unifises` cookie, responses shaped
    as {"meta": {"rc": "ok"}, ...}
  * UniFi OS consoles (UCG/UDM/UXG)  — `TOKEN` cookie + `X-CSRF-Token`
    handshake, login response carries {"unique_id": ..., "csrfToken": ...}

Stdlib only (urllib + http.cookiejar); TLS verification relaxed because UniFi
consoles serve self-signed certs. All network calls are wrapped so a dead or
unreachable controller degrades to a logged warning instead of an exception.
"""

import hashlib
import http.cookiejar
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger("barenoc-unifi")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# Cookies that mark an authenticated controller session. UniFi OS consoles set
# TOKEN; legacy Network controllers set unifises (some builds use UISESSIONID).
_SESSION_COOKIE_NAMES = ("TOKEN", "unifises", "UISESSIONID")

# A cached session is trusted for this long before we re-authenticate. UniFi OS
# sessions outlive this window, but the conservative cap means a controller
# that has restarted (dropping the session) is re-logged-in promptly.
_SESSION_MAX_AGE_SECONDS = 23 * 3600

# Exponential backoff schedule (seconds). Repeated logins are what trip the
# controller's login throttle (B4 lockout), so every retry waits longer before
# trying again instead of hammering the controller.
_BACKOFF_SCHEDULE = (0.5, 1.0, 2.0, 4.0, 8.0)

_LOGIN_MAX_ATTEMPTS = 5        # total login POSTs, including the first
_REQUEST_MAX_ATTEMPTS = 4      # total data-request attempts, incl. the first


def _backoff_delay(attempt: int) -> float:
    """Backoff sleep for a 1-based attempt number."""
    idx = min(max(attempt - 1, 0), len(_BACKOFF_SCHEDULE) - 1)
    return _BACKOFF_SCHEDULE[idx]


def device_live_ip(d: dict) -> str:
    """The device's current management IP: ``config_network.ip`` (the static /
    configured address) preferred, falling back to stat/device's last-known
    ``ip`` (the DHCP lease / reported address). This is what the sync must
    write into the appliance record so a pre-VLAN-move IP never goes stale."""
    cn = d.get("config_network") or {}
    return (cn.get("ip") or d.get("ip") or "").strip()


def _num(v):
    """Coerce a UniFi value to float or None (safe against strings/nulls)."""
    try:
        if v is None or v == "":
            return None
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    except (TypeError, ValueError):
        return None


class _Session:
    """Shared controller session state (cookie jar + CSRF token + login time).

    Password-authenticated UniFiClients that target the same controller share
    one _Session, so repeated calls reuse the existing login instead of
    re-authenticating per call (which trips the controller's login throttle).
    The RLock serializes login attempts across threads: concurrent callers
    wait, then see the just-established session and reuse it.
    """
    __slots__ = ("jar", "csrf_token", "logged_in_at", "lock")

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.csrf_token: Optional[str] = None
        self.logged_in_at = 0.0
        self.lock = threading.RLock()


def _session_key(base_url: str, username: str, password: str,
                 api_key: Optional[str], site: str) -> tuple:
    """Cache key for one controller + credential set. The password is hashed
    so a settings change (new password/API key) naturally starts a fresh
    session instead of reusing a stale one."""
    digest = hashlib.sha256((password or "").encode("utf-8")).hexdigest()
    return (base_url.rstrip("/"), username or "", digest, api_key or "",
            site or "default")


_SESSION_CACHE: dict = {}
_SESSION_CACHE_LOCK = threading.Lock()


def _get_session(key: tuple) -> "_Session":
    with _SESSION_CACHE_LOCK:
        sess = _SESSION_CACHE.get(key)
        if sess is None:
            sess = _Session()
            _SESSION_CACHE[key] = sess
        return sess


class UniFiClient:
    """Client for the UniFi Controller REST API (legacy + UniFi OS)."""

    def __init__(self, base_url: str = "https://192.0.2.1:443",
                 username: str = "admin", password: str = "",
                 api_key: Optional[str] = None,
                 site: str = "default", timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.api_key = api_key or None   # UniFi OS local API key (Method A)
        self.site = site
        self.timeout = timeout
        self.last_error: Optional[str] = None  # human-readable reason for the last failure
        if self.api_key:
            # Local API key is stateless — no session to share or cache.
            self._session = _Session()
        else:
            # Password auth shares one session per controller+creds, so the
            # scheduler, link monitor, telemetry and every route handler reuse
            # a single login instead of each POSTing /api/auth/login.
            self._session = _get_session(
                _session_key(self.base_url, self.username, self.password,
                             self.api_key, self.site))

    def _request(self, method: str, path: str,
                 data: Optional[dict] = None,
                 headers: Optional[dict] = None,
                 _attempt: int = 1,
                 _retry: bool = True,
                 _relogin: bool = True) -> Optional[dict]:
        """Make an HTTP request to the controller. Returns parsed JSON or None.

        Retries HTTP 429 (the login/rate-limit throttle) and — for safe GET
        reads — transient connection errors, with exponential backoff so a
        throttled controller is never hammered. On 401/403 (session expired or
        revoked) it re-authenticates ONCE and retries.
        """
        url = f"{self.base_url}{path}"
        req_headers = {"Content-Type": "application/json"}
        if self._session.csrf_token:
            req_headers["X-CSRF-Token"] = self._session.csrf_token
        if self.api_key:
            # UniFi OS local API key — stateless, no session needed
            req_headers["X-API-KEY"] = self.api_key
        if headers:
            req_headers.update(headers)

        body = json.dumps(data).encode() if data is not None else None
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=SSL_CTX),
            urllib.request.HTTPCookieProcessor(self._session.jar))
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)

        try:
            # Serialize the cookie-processing part of the call — the shared
            # CookieJar is mutated by HTTPCookieProcessor inside open(), so
            # concurrent threads must not race it.
            with self._session.lock:
                resp = opener.open(req, timeout=self.timeout)
            payload = resp.read().decode()
            self._capture_csrf(resp.headers)
            self.last_error = None
            return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            # UniFi OS can hand out the CSRF token on error responses too
            self._capture_csrf(e.headers)
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            self.last_error = f"HTTP {e.code} {e.reason} {detail}".strip()
            # Rate-limited (login throttle / 429): back off and retry instead
            # of extending the lockout with more immediate calls.
            if _retry and e.code == 429 and _attempt < _REQUEST_MAX_ATTEMPTS:
                delay = _backoff_delay(_attempt)
                logger.warning(
                    f"UniFi rate-limited (HTTP 429) on {method} {path} — "
                    f"retrying in {delay:.1f}s (attempt {_attempt})")
                time.sleep(delay)
                return self._request(method, path, data, headers,
                                     _attempt=_attempt + 1, _retry=_retry,
                                     _relogin=_relogin)
            # Session expired / revoked: re-authenticate once and retry.
            if (_relogin and e.code in (401, 403) and not self.api_key
                    and self._had_session()):
                self._invalidate_session()
                if self.login():
                    return self._request(method, path, data, headers,
                                         _attempt=1, _retry=_retry,
                                         _relogin=False)
            logger.warning(f"UniFi API error: {self.last_error}")
            return None
        except Exception as e:
            self.last_error = str(e)
            # Retry connection errors only for idempotent reads — a write may
            # have reached the controller before the connection dropped.
            if _retry and method == "GET" and _attempt < _REQUEST_MAX_ATTEMPTS:
                delay = _backoff_delay(_attempt)
                logger.warning(
                    f"UniFi connection error on {method} {path} — "
                    f"retrying in {delay:.1f}s (attempt {_attempt})")
                time.sleep(delay)
                return self._request(method, path, data, headers,
                                     _attempt=_attempt + 1, _retry=_retry,
                                     _relogin=_relogin)
            logger.warning(f"UniFi connection failed: {e}")
            return None

    def _capture_csrf(self, headers) -> None:
        token = headers.get("X-CSRF-Token") if headers else None
        if token:
            self._session.csrf_token = token

    def _had_session(self) -> bool:
        """True when the shared cookie jar already holds a session cookie."""
        try:
            return any(c.name in _SESSION_COOKIE_NAMES
                       for c in self._session.jar)
        except Exception:
            return False

    def _session_active(self) -> bool:
        """True when the cached session is still usable (cookie present,
        not expired, and not older than the reuse window)."""
        s = self._session
        if not s.logged_in_at:
            return False
        if time.time() - s.logged_in_at > _SESSION_MAX_AGE_SECONDS:
            return False
        try:
            for c in s.jar:
                if c.name in _SESSION_COOKIE_NAMES and not c.is_expired():
                    return True
        except Exception:
            pass
        return False

    def _invalidate_session(self) -> None:
        with self._session.lock:
            self._session.jar.clear()
            self._session.csrf_token = None
            self._session.logged_in_at = 0.0

    def _mark_logged_in(self) -> None:
        self._session.logged_in_at = time.time()

    def _note_csrf(self, result) -> None:
        """Some UniFi OS builds return the CSRF token only in the login body
        (``csrfToken``) rather than the response header. Capture either so the
        shared session keeps a usable token for subsequent requests."""
        if isinstance(result, dict) and result.get("csrfToken"):
            self._session.csrf_token = result["csrfToken"]

    def _sleep_backoff(self, attempt: int, why: str) -> None:
        delay = _backoff_delay(attempt)
        logger.warning(f"{why} — retrying in {delay:.1f}s (attempt {attempt})")
        time.sleep(delay)

    # ── auth ──────────────────────────────────────────────────────

    def login(self) -> bool:
        """Authenticate (or reuse an existing session). With a local API key
        there is nothing to do — the key rides on every request. Otherwise:
        UniFi OS 4.x uses /api/auth/login (the legacy /api/login is disabled
        on UCG/UDM); older controllers use /api/login. Returns True on
        success, sets self.last_error on failure.

        Session reuse + backoff (B4): the login is performed at most once per
        controller session — subsequent calls reuse the cached cookie jar —
        and retries are spaced with exponential backoff so a throttled
        controller is never hammered into a longer lockout.
        """
        if self.api_key:
            logger.info("UniFi auth: local API key (no session login)")
            return True
        with self._session.lock:
            if self._session_active():
                logger.debug("UniFi session reuse — reusing existing login")
                self.last_error = None
                return True
            return self._login_locked()

    def _login_locked(self) -> bool:
        """Perform the credential login. Caller must hold self._session.lock."""
        body = {"username": self.username, "password": self.password}
        last = "unknown"
        for attempt in range(1, _LOGIN_MAX_ATTEMPTS + 1):
            if attempt > 1:
                self._sleep_backoff(attempt - 1, "UniFi login failed")
            # UniFi OS (UCG/UDM/UXG): returns a user object with unique_id,
            # sets a TOKEN cookie + x-csrf-token response header
            result = self._request("POST", "/api/auth/login", body,
                                   _retry=False, _relogin=False)
            if result is not None and (
                "unique_id" in result or result.get("meta", {}).get("rc") == "ok"
            ):
                self._note_csrf(result)
                self._mark_logged_in()
                logger.info("UniFi login successful (UniFi OS /api/auth/login)")
                return True
            # Legacy controllers: {"meta": {"rc": "ok"}}
            result = self._request("POST", "/api/login", body,
                                   _retry=False, _relogin=False)
            if result is not None and result.get("meta", {}).get("rc") == "ok":
                self._note_csrf(result)
                self._mark_logged_in()
                logger.info("UniFi login successful (legacy /api/login)")
                return True
            last = self.last_error or "unknown"
        self.last_error = last
        logger.warning(f"UniFi login failed after {_LOGIN_MAX_ATTEMPTS} attempts: {last}")
        return False

    def _stat(self, endpoint: str) -> Optional[dict]:
        """GET a Network stat endpoint. UniFi OS serves it behind the proxy
        prefix; standalone controllers serve it at the API root."""
        result = self._request("GET", f"/proxy/network/api/s/{self.site}/{endpoint}")
        if result is None or "data" not in result:
            result = self._request("GET", f"/api/s/{self.site}/{endpoint}")
        return result

    def _cmd(self, endpoint: str, payload: dict) -> Optional[dict]:
        """POST a Network command endpoint (proxy prefix on UniFi OS, root on
        standalone controllers)."""
        result = self._request("POST", f"/proxy/network/api/s/{self.site}/{endpoint}", payload)
        if result is None:
            result = self._request("POST", f"/api/s/{self.site}/{endpoint}", payload)
        return result

    # ── inventory ────────────────────────────────────────────────

    def get_devices(self) -> list:
        """Get all managed UniFi network devices (gateways, switches, APs).

        Includes firmware fields the upgrade engine needs: current version,
        previous version, available (upgrade-to) version, and upgradeable flag.
        UniFi OS reports these under a few spellings across firmware lines, so
        each is resolved defensively.
        """
        result = self._stat("stat/device")
        if result and "data" in result:
            devices = []
            for d in result["data"]:
                up = d.get("uplink") or {}
                devices.append({
                    "name": d.get("name") or d.get("mac") or "unknown",
                    "hostname": d.get("hostname") or d.get("name") or "",
                    "ip": device_live_ip(d),
                    "mac": d.get("mac", ""),
                    "model": d.get("model", ""),
                    "type": self._map_type(d.get("type", "")),
                    "status": "online" if d.get("state", 0) == 1 else "offline",
                    "state": d.get("state", 0),
                    "version": d.get("version", ""),
                    "previous_version": (d.get("previous_version")
                                         or d.get("previous_firmware_version") or ""),
                    "available_version": (d.get("upgrade_to_firmware")
                                          or d.get("upgrade_version")
                                          or d.get("upgrade_to_version") or ""),
                    "upgradeable": bool(d.get("upgradeable")),
                    "uptime": d.get("uptime", 0),
                    "site": d.get("site_id", self.site),
                    "uplink_mac": up.get("uplink_mac", ""),
                    "uplink_device_name": up.get("uplink_device_name", ""),
                    "uplink_remote_port": up.get("uplink_remote_port"),
                })
            return devices
        return []

    def get_device(self, mac: str) -> Optional[dict]:
        """One managed device by MAC (the shape get_devices returns), or None."""
        for d in self.get_devices():
            if (d.get("mac") or "").lower() == (mac or "").lower():
                return d
        return None

    def get_raw_devices(self) -> Optional[list]:
        """Raw stat/device list (includes per-device port_table with up/media/
        name) or None when the controller is unreachable / the call failed.
        The link monitor uses this as its per-port data channel."""
        rd = self._request("GET", f"/proxy/network/api/s/{self.site}/stat/device")
        if rd is None or "data" not in rd:
            return None
        return rd.get("data") or []

    def get_wan_health(self, mac: str) -> Optional[dict]:
        """WAN subsystem health for a gateway (stat/health). Returns
        {'status': ..., 'wan_ip': ...} or None when unavailable. UniFi OS
        serves it per-device at stat/health/{mac}; some builds return the
        whole health map under stat/health keyed by MAC."""
        result = self._stat(f"stat/health/{mac}")
        subsystems = (result or {}).get("data")
        if not isinstance(subsystems, list):
            result = self._stat("stat/health")
            data = (result or {}).get("data")
            if isinstance(data, dict):
                subsystems = data.get(mac) or data.get((mac or "").lower()) or []
            else:
                subsystems = data if isinstance(data, list) else []
        for s in (subsystems or []):
            if isinstance(s, dict) and s.get("subsystem") == "wan":
                # Rich shape (superset of the link-monitor's {status, wan_ip}):
                # the uplink card shows latency/throughput/uptime + ISP hints
                # when this firmware build exposes them. Missing keys read None
                # and the UI degrades to what it has.
                return {
                    "status": str(s.get("status") or ""),
                    "wan_ip": s.get("wan_ip", ""),
                    "gateway_ip": s.get("gateway_ip") or s.get("gateway") or "",
                    "latency": _num(s.get("latency")),
                    "uptime": _num(s.get("uptime")),
                    "up": s.get("up"),
                    "internet_ok": s.get("internet_ok"),
                    "dns_ok": s.get("dns_ok"),
                    "nameservers": s.get("nameservers") or s.get("wan_dns") or [],
                    "isp_name": s.get("isp_name") or s.get("isp") or "",
                    "speedtest_download": _num(s.get("speedtest_download")
                                               or s.get("speedtest_down")),
                    "speedtest_upload": _num(s.get("speedtest_upload")
                                             or s.get("speedtest_up")),
                    "speedtest_latency": _num(s.get("speedtest_latency")
                                               or s.get("speedtest_ping")),
                }
        return None

    def get_clients(self) -> list:
        """All known clients (rest/user, incl. offline) merged with active
        sessions (stat/sta) so each entry has a usable IP + online status.
        UniFi keeps the live IP only on the active-session record."""
        known = self._stat("rest/user")
        active = self._stat("stat/sta")
        by_mac = {}
        if known and "data" in known:
            for c in known["data"]:
                mac = c.get("mac", "")
                if not mac:
                    continue
                by_mac[mac] = {
                    "mac": mac,
                    "hostname": c.get("hostname") or c.get("name") or "",
                    "oui": c.get("oui") or "",
                    "wired": c.get("is_wired", True),
                    "last_seen": c.get("last_seen"),
                    "ip": c.get("last_ip") or c.get("fixed_ip") or "",
                }
        active_macs = set()
        if active and "data" in active:
            for c in active["data"]:
                mac = c.get("mac", "")
                if not mac:
                    continue
                active_macs.add(mac)
                rec = by_mac.setdefault(mac, {"mac": mac, "hostname": "", "oui": "",
                                               "wired": True, "last_seen": None, "ip": ""})
                if c.get("ip"):
                    rec["ip"] = c["ip"]
                if c.get("hostname"):
                    rec["hostname"] = c["hostname"]
                if c.get("oui") and not rec["oui"]:
                    rec["oui"] = c["oui"]
                if c.get("is_wired") is not None:
                    rec["wired"] = c["is_wired"]
                rec["last_seen"] = c.get("last_seen") or rec["last_seen"]
                if c.get("sw_mac"):
                    rec["sw_mac"] = c["sw_mac"]
                if c.get("sw_port"):
                    rec["sw_port"] = c["sw_port"]
                if c.get("ap_mac"):
                    rec["ap_mac"] = c["ap_mac"]
        out = []
        for mac, rec in by_mac.items():
            hostname = rec["hostname"]
            oui = rec["oui"]
            out.append({
                "mac": mac,
                "hostname": hostname,
                "name": hostname or oui or "unknown",
                "ip": rec["ip"],
                "vendor": oui,
                "wired": bool(rec["wired"]) if rec["wired"] is not None else True,
                "last_seen": rec["last_seen"],
                "online": mac in active_macs,
                "sw_mac": rec.get("sw_mac"),
                "sw_port": rec.get("sw_port"),
                "ap_mac": rec.get("ap_mac"),
            })
        return out

    def get_dhcp_leases(self) -> list:
        """DHCP leases (stat/dhcpd_lease) — best-effort. UniFi OS 4.x on the
        UCG-Max 404s this endpoint, so this returns [] on this build; the
        client list (get_clients) is the fallback path for lease hostnames.
        Never raises."""
        result = self._stat("stat/dhcpd_lease")
        leases = (result or {}).get("data", [])
        if not isinstance(leases, list):
            return []
        out = []
        for l in leases:
            if not isinstance(l, dict) or not l.get("mac"):
                continue
            out.append({
                "mac": str(l.get("mac")).strip().lower(),
                "ip": l.get("ip") or "",
                "hostname": l.get("hostname") or l.get("name") or "",
            })
        return out

    def get_networks(self) -> list:
        """Get active local network configs (VLANs/subnets)."""
        result = self._stat("rest/networkconf")
        if result and "data" in result:
            return [{
                "name": n.get("name", ""),
                "vlan": n.get("vlan_enabled") and n.get("vlan") or None,
                "subnet": n.get("ip_subnet", ""),
                "purpose": n.get("purpose", ""),
                "enabled": n.get("enabled", True),
                "dhcp": n.get("dhcpd_enabled", False),
                "dhcp_start": n.get("dhcpd_start", ""),
                "dhcp_stop": n.get("dhcpd_stop", ""),
            } for n in result["data"]]
        return []

    def get_wlans(self) -> list:
        """Get active Wi-Fi SSIDs and security profiles (incl. the network id
        the SSID binds to, so wireless VLANs can be resolved)."""
        result = self._stat("rest/wlanconf")
        if result and "data" in result:
            return [{
                "name": w.get("name", ""),
                "ssid": w.get("name", ""),
                "enabled": w.get("enabled", False),
                "networkconf_id": w.get("networkconf_id", ""),
                "security": "open" if w.get("security") in ("open", None, "") else w.get("security", ""),
                "wpa_mode": w.get("wpa_mode", ""),
            } for w in result["data"]]
        return []

    def restart_device(self, mac: str) -> bool:
        """Reboot a UniFi device (cmd/devmgr restart). Best-effort."""
        result = self._cmd("cmd/devmgr",
                           {"cmd": "restart", "mac": mac, "reboot_type": "soft"})
        return bool(result and result.get("meta", {}).get("rc") == "ok")

    # ── firmware upgrades (autonomy-aware engine) ────────────────────

    def cache_firmware(self, mac: str) -> bool:
        """PRE-STAGE: make the device download its available firmware WITHOUT
        applying it (cmd/devmgr 'cache'). The upgrade engine calls this ahead of
        the window so the in-window apply is fast."""
        result = self._cmd("cmd/devmgr", {"cmd": "cache", "mac": mac})
        return bool(result and result.get("meta", {}).get("rc") == "ok")

    def upgrade_device(self, mac: str, version: Optional[str] = None) -> bool:
        """Apply a firmware upgrade to one device (cmd/devmgr 'upgrade').

        ``version`` pins a specific firmware (used for rollback to the previous
        version); when None the controller applies the staged/latest firmware.
        """
        payload = {"cmd": "upgrade", "mac": mac}
        if version:
            payload["version"] = version
        result = self._cmd("cmd/devmgr", payload)
        return bool(result and result.get("meta", {}).get("rc") == "ok")

    def rollback_device(self, mac: str, previous_version: str) -> bool:
        """Roll a device back to its previous firmware (best-effort; the engine
        verifies recovery afterward and escalates physically if it fails too)."""
        if not previous_version:
            return False
        return self.upgrade_device(mac, version=previous_version)

    # ── port profiles / VLAN assignment (approved-agent-action support) ──

    def get_networks_map(self) -> dict:
        """network_id -> {name, vlan} for resolving VLAN names to IDs."""
        out = {}
        result = self._stat("rest/networkconf")
        for n in (result or {}).get("data", []):
            out[n.get("_id", "")] = {
                "name": n.get("name", ""),
                "vlan": n.get("vlan_enabled") and n.get("vlan") or None,
                "subnet": n.get("ip_subnet", ""),
                "purpose": n.get("purpose", ""),
            }
        return out

    def get_firewall_rules(self) -> list:
        """List ALL firewall rules (custom + predefined) via the v2 API that
        the modern UI uses. Custom rules have origin_type == 'traffic_rule'."""
        result = self._request(
            "GET",
            f"/proxy/network/v2/api/site/default/firewall-rules/combined-traffic-firewall-rules",
        )
        return result or []

    def get_custom_firewall_rules(self) -> list:
        return [r for r in self.get_firewall_rules()
                if r.get("origin_type") == "traffic_rule"]

    def create_network(self, name: str, vlan: int, subnet: str = None,
                       dhcp: bool = True) -> Optional[str]:
        """Create a corporate VLAN network. Returns the new network _id or None.
        subnet defaults to 192.168.<vlan>.1/24 (third-octet == VLAN convention)."""
        if subnet is None:
            subnet = f"192.168.{vlan}.1/24"
        payload = {"name": name, "purpose": "corporate", "ip_subnet": subnet,
                   "vlan_enabled": True, "vlan": vlan, "dhcpd_enabled": dhcp,
                   "dhcpd_start": subnet.rsplit(".", 1)[0] + ".6",
                   "dhcpd_stop": subnet.rsplit(".", 1)[0] + ".254"}
        res = self._request("POST", f"/proxy/network/api/s/{self.site}/rest/networkconf", payload)
        if res and res.get("meta", {}).get("rc") == "ok":
            d = res.get("data")
            return (d[0] if isinstance(d, list) and d else d or {}).get("_id")
        return None

    # ── firewall groups (NOTE: only address-group is accepted on UniFi OS 4.x;)
    # ── mac-group returns api.err.InvalidValue ───────────────────────────────

    def list_firewall_groups(self) -> list:
        result = self._request("GET", f"/proxy/network/api/s/{self.site}/rest/firewallgroup")
        return (result or {}).get("data", []) or []

    def create_address_group(self, name: str, members: list = None) -> Optional[str]:
        """Create an address-group (IP/CIDR). Returns group _id or None."""
        payload = {"name": name, "group_type": "address-group",
                   "group_members": members or []}
        res = self._request("POST", f"/proxy/network/api/s/{self.site}/rest/firewallgroup", payload)
        if res and res.get("meta", {}).get("rc") == "ok":
            d = res.get("data")
            return (d[0] if isinstance(d, list) and d else d or {}).get("_id")
        return None

    def update_address_group(self, group_id: str, members: list) -> bool:
        """Replace the members of an address-group."""
        payload = {"group_type": "address-group", "group_members": members}
        res = self._request("PUT",
                            f"/proxy/network/api/s/{self.site}/rest/firewallgroup/{group_id}",
                            payload)
        return bool(res and res.get("meta", {}).get("rc") == "ok")

    def get_port_profiles(self) -> list:
        """Port profiles (rest/portconf): id, name, native + tagged networks."""
        result = self._stat("rest/portconf")
        if result and "data" in result:
            return [{
                "id": p.get("_id", ""),
                "name": p.get("name", ""),
                "native_network_id": p.get("native_networkconf_id", ""),
                "tagged_network_ids": [x for x in (p.get("tagged_networkconf_id") or "").split(",") if x],
            } for p in result["data"]]
        return []

    def get_switch_ports(self, mac: str) -> list:
        """Port table for one switch (native/tagged networks per port, plus
        the effective ``disabled`` state from port_overrides)."""
        rd = self._request("GET", f"/proxy/network/api/s/{self.site}/stat/device")
        dev = next((d for d in (rd or {}).get("data", [])
                    if (d.get("mac") or "").lower() == mac.lower()), None)
        if not dev:
            return []
        overrides = {o.get("port_idx"): o for o in (dev.get("port_overrides") or [])}
        ports = []
        for pt in (dev.get("port_table") or []):
            ov = overrides.get(pt.get("port_idx")) or {}
            ports.append({
                "port_idx": pt.get("port_idx"),
                "name": ov.get("name") or pt.get("name") or f"Port {pt.get('port_idx')}",
                "native_network_id": pt.get("native_networkconf_id", ""),
                "tagged_network_ids": [x for x in (pt.get("tagged_networkconf_id") or "").split(",") if x],
                "up": bool(pt.get("up")),
                "disabled": bool(ov.get("disabled", False)),
            })
        return ports

    def set_port_name(self, switch_mac: str, port_idx: int, name: str) -> dict:
        """Rename a switch port via port_overrides (PUT /rest/device/{mac})."""
        current = self.get_switch_ports(switch_mac)
        if not any(pt["port_idx"] == port_idx for pt in current):
            return {"applied": False,
                    "error": f"port {port_idx} not found on {switch_mac}"}
        path = self._device_path(switch_mac)
        if not path:
            return {"applied": False,
                    "error": f"device {switch_mac} not found on the controller"}
        result = self._request(
            "PUT", path,
            {"port_overrides": [{"port_idx": port_idx, "name": name}]})
        if result and result.get("meta", {}).get("rc") == "ok":
            return {"applied": True, "port_idx": port_idx, "name": name}
        return {"applied": False,
                "error": f"device update failed: {self.last_error or result}"}

    def bounce_port(self, switch_mac: str, port_idx: int, hold: float = 2.0) -> dict:
        """Cycle a switch port: disable -> hold -> enable (drops the link / PoE).
        The mechanism UniFi's own 'power cycle' uses (port_overrides disabled)."""
        import time
        current = self.get_switch_ports(switch_mac)
        if not any(pt["port_idx"] == port_idx for pt in current):
            return {"applied": False,
                    "error": f"port {port_idx} not found on {switch_mac}"}
        path = self._device_path(switch_mac)
        if not path:
            return {"applied": False,
                    "error": f"device {switch_mac} not found on the controller"}
        for disabled in (True, False):
            result = self._request(
                "PUT", path,
                {"port_overrides": [{"port_idx": port_idx, "disabled": disabled}]})
            if not (result and result.get("meta", {}).get("rc") == "ok"):
                return {"applied": False,
                        "error": f"set disabled={disabled} failed: {self.last_error or result}"}
            if disabled:
                time.sleep(hold)
        return {"applied": True, "port_idx": port_idx}

    def ensure_wireless_uplinks(self, dry_run: bool = False) -> dict:
        """Ensure every ENABLED wireless SSID VLAN is available on every AP's
        uplink port (native or tagged), preserving all other port settings and
        exclusions. Merge-safe: always writes the FULL port_overrides array.

        Returns a summary of changes per switch/port. dry_run=True computes and
        reports without writing."""
        from collections import defaultdict

        nets = self.get_networks_map()  # id -> {name, vlan}
        wlans = self.get_wlans()
        enabled = [w for w in wlans if w.get("enabled")]
        wireless_ids = [w["networkconf_id"] for w in enabled if w.get("networkconf_id")]
        if not wireless_ids:
            return {"status": "ok", "message": "no enabled wireless SSIDs", "changed": []}

        rd = self._request("GET", f"/proxy/network/api/s/{self.site}/stat/device")
        devices = (rd or {}).get("data", [])
        by_mac = {d.get("mac", "").lower(): d for d in devices if d.get("mac")}

        # group APs by their uplink switch+port
        uplinks = defaultdict(list)   # switch_mac -> [(port_idx, ap_name)]
        for d in devices:
            up = d.get("uplink") or {}
            if d.get("type") == "uap" and up.get("uplink_mac"):
                uplinks[up["uplink_mac"].lower()].append(
                    (up.get("uplink_remote_port"), d.get("name") or d.get("mac")))

        changed = []
        for sw_mac, ports in uplinks.items():
            dev = by_mac.get(sw_mac)
            if not dev:
                continue
            path = self._device_path(dev.get("mac"))
            if not path:
                continue
            overrides = [dict(o) for o in (dev.get("port_overrides") or [])]
            port_table = {p.get("port_idx"): p for p in (dev.get("port_table") or [])}
            touched = False
            for port_idx, ap_name in ports:
                cur = next((o for o in overrides if o.get("port_idx") == port_idx),
                           dict(port_table.get(port_idx) or {}))
                cur["port_idx"] = port_idx
                cur.setdefault("name", f"Port {port_idx}")
                native = cur.get("native_networkconf_id") or (port_table.get(port_idx) or {}).get("native_networkconf_id") or ""
                tagged_now = [x for x in str(cur.get("tagged_networkconf_id") or "").split(",") if x]
                excluded = list(cur.get("excluded_networkconf_ids") or [])
                fwd = cur.get("forward")
                if fwd == "all":
                    continue  # already trunks everything
                add = [nid for nid in wireless_ids
                       if nid and nid != native and nid not in tagged_now]
                if not add:
                    continue
                # keep the port's existing exclusions except the wireless VLANs
                # we are now explicitly tagging
                cur["excluded_networkconf_ids"] = [x for x in excluded if x not in add]
                cur["tagged_networkconf_id"] = ",".join(tagged_now + add)
                cur["tagged_vlan_mgmt"] = "custom"
                cur["forward"] = "customize"
                # ensure the changed override is in the array (replace by port_idx)
                overrides = [o for o in overrides if o.get("port_idx") != port_idx] + [cur]
                touched = True
                for nid in add:
                    changed.append({
                        "switch": dev.get("name") or sw_mac,
                        "switch_mac": dev.get("mac"),
                        "port": port_idx,
                        "ap": ap_name,
                        "tagged_vlan": nets.get(nid, {}).get("name") or nid,
                        "vlan": nets.get(nid, {}).get("vlan"),
                    })
            if touched and not dry_run:
                res = self._request("PUT", path, {"port_overrides": overrides})
                if not (res and res.get("meta", {}).get("rc") == "ok"):
                    return {"status": "error", "message": f"update failed on {dev.get('name')}: {self.last_error or res}", "changed": changed}
        return {"status": "ok", "dry_run": dry_run,
                "message": f"wireless VLANs ensured across {len(uplinks)} switch(es)",
                "changed": changed}

    def firmware_status(self) -> dict:
        """Current firmware versions of the controller + managed devices
        (read-only) — surfaced on the System page so firmware levels are
        visible without the UniFi UI."""
        rd = self._request("GET", f"/proxy/network/api/s/{self.site}/stat/device")
        devices = []
        gateway = None
        for d in (rd or {}).get("data", []):
            rec = {"name": d.get("name") or d.get("mac"),
                   "model": d.get("model", ""),
                   "version": d.get("version", ""),
                   "type": d.get("type", "")}
            devices.append(rec)
            if d.get("type") in ("udm", "ugw", "ucg"):
                gateway = rec
        return {"gateway": gateway, "devices": devices}

    def set_ssid_password(self, ssid: str, passphrase: str) -> dict:
        """Change a Wi-Fi SSID's passphrase (PUT rest/wlanconf/{id} x_passphrase).
        The mechanism the Pi Coding Agent discovered when no action existed."""
        if not (8 <= len(passphrase) <= 63):
            return {"applied": False, "error": "passphrase must be 8-63 characters"}
        result = self._stat("rest/wlanconf")
        wlan = None
        for w in (result or {}).get("data", []):
            if (w.get("name") or "").strip().lower() == (ssid or "").strip().lower():
                wlan = w
                break
        if not wlan:
            return {"applied": False,
                    "error": f"SSID '{ssid}' not found on the controller"}
        wid = wlan.get("_id")
        if not wid:
            return {"applied": False, "error": "WLAN record has no id"}
        res = self._request(
            "PUT", f"/proxy/network/api/s/{self.site}/rest/wlanconf/{wid}",
            {"x_passphrase": passphrase})
        if res and res.get("meta", {}).get("rc") == "ok":
            return {"applied": True, "ssid": ssid,
                    "security": wlan.get("security", "wpapsk")}
        return {"applied": False,
                "error": f"controller rejected update: {self.last_error or res}"}

    def find_client_port(self, client_ip: str) -> dict:
        """Which switch + port a wired client is connected to (stat/sta)."""
        result = self._stat("stat/sta")
        for s in (result or {}).get("data", []):
            if s.get("ip") == client_ip and s.get("sw_mac"):
                return {
                    "switch_mac": s["sw_mac"],
                    "switch_name": self._switch_name(s["sw_mac"]),
                    "port_idx": s.get("sw_port"),
                    "network_id": s.get("network_id", ""),
                    "vlan": s.get("vlan"),
                }
        return {}

    def _switch_name(self, mac: str) -> str:
        rd = self._request("GET", f"/proxy/network/api/s/{self.site}/stat/device")
        for d in (rd or {}).get("data", []):
            if (d.get("mac") or "").lower() == mac.lower():
                return d.get("name") or mac
        return mac

    def _device_id(self, mac: str) -> Optional[str]:
        """Resolve a device MAC to its database _id (stat/device). The legacy
        rest/device/{id} PUT rejects MACs with api.err.IdInvalid."""
        rd = self._request("GET", f"/proxy/network/api/s/{self.site}/stat/device")
        for d in (rd or {}).get("data", []):
            if (d.get("mac") or "").lower() == (mac or "").lower():
                return d.get("_id")
        return None

    def _device_path(self, mac: str) -> Optional[str]:
        did = self._device_id(mac)
        return f"/proxy/network/api/s/{self.site}/rest/device/{did}" if did else None

    def set_port_vlans(self, switch_mac: str, port_idx: int,
                       tagged_network_ids: list,
                       native_network_id: Optional[str] = None) -> dict:
        """Set native + tagged VLAN networks on a switch port.

        Applies the override directly on the device (port_overrides with the
        network ids embedded — UniFi OS 4.x rejects portconf_id references
        with api.err.IdInvalid) via PUT /rest/device/{device_id}.
        Returns {applied: True, port_idx, native, tagged} or an error dict.
        """
        tagged = [x for x in (tagged_network_ids or []) if x]
        path = self._device_path(switch_mac)
        if not path:
            return {"applied": False,
                    "error": f"device {switch_mac} not found on the controller"}
        # merge with the FULL current overrides — PUT /rest/device REPLACES the
        # whole array, so sending only the changed port would wipe the others
        # (e.g. the VM's port). Preserve every other override verbatim.
        current = self.get_switch_ports(switch_mac)
        cur = next((p for p in current if p["port_idx"] == port_idx), None)
        rd = self._request("GET", f"/proxy/network/api/s/{self.site}/stat/device")
        dev = next((d for d in (rd or {}).get("data", [])
                    if (d.get("mac") or "").lower() == (switch_mac or "").lower()), None)
        existing = [dict(o) for o in (dev or {}).get("port_overrides") or []
                     if o.get("port_idx") != port_idx]
        override = {
            "port_idx": port_idx,
            "native_networkconf_id": native_network_id or cur.get("native_network_id") or "",
            "tagged_networkconf_id": ",".join(tagged),
            "forward": "customize",
            "name": cur.get("name") or f"Port {port_idx}",
        }
        result = self._request("PUT", path, {"port_overrides": existing + [override]})
        if result and result.get("meta", {}).get("rc") == "ok":
            return {"applied": True, "port_idx": port_idx,
                    "native": native_network_id, "tagged": tagged}
        return {"applied": False,
                "error": f"device update failed: {self.last_error or result}"}

    def set_port_disabled(self, switch_mac: str, port_idx: int,
                          disabled: bool = True) -> dict:
        """Enable/disable a switch port (the dead-end/loop fix path).

        Merge-safe: PUT /rest/device REPLACES the whole port_overrides array,
        so this reads the FULL current array and only adds the ``disabled``
        flag to the target port — every other port's override is preserved
        verbatim (STANDING PROCEDURE #4). Returns {applied, port_idx,
        disabled} or an error dict.
        """
        path = self._device_path(switch_mac)
        if not path:
            return {"applied": False,
                    "error": f"device {switch_mac} not found on the controller"}
        rd = self._request("GET", f"/proxy/network/api/s/{self.site}/stat/device")
        dev = next((d for d in (rd or {}).get("data", [])
                    if (d.get("mac") or "").lower() == (switch_mac or "").lower()), None)
        if not dev:
            return {"applied": False,
                    "error": f"device {switch_mac} not found on the controller"}
        overrides = [dict(o) for o in (dev.get("port_overrides") or [])]
        port_table = {p.get("port_idx"): p for p in (dev.get("port_table") or [])}
        if port_idx not in port_table and not any(o.get("port_idx") == port_idx for o in overrides):
            return {"applied": False,
                    "error": f"port {port_idx} not found on {switch_mac}"}
        cur = next((o for o in overrides if o.get("port_idx") == port_idx),
                   dict(port_table.get(port_idx) or {}))
        cur["port_idx"] = port_idx
        cur["disabled"] = bool(disabled)
        cur.setdefault("name", (port_table.get(port_idx) or {}).get("name")
                       or f"Port {port_idx}")
        overrides = [o for o in overrides if o.get("port_idx") != port_idx] + [cur]
        result = self._request("PUT", path, {"port_overrides": overrides})
        if result and result.get("meta", {}).get("rc") == "ok":
            return {"applied": True, "port_idx": port_idx,
                    "disabled": bool(disabled)}
        return {"applied": False,
                "error": f"device update failed: {self.last_error or result}"}

    def _map_type(self, unifi_type: str) -> str:
        """Map UniFi device type to BareNOC type."""
        mapping = {
            "ugw": "gateway",
            "ucg": "gateway",
            "udm": "gateway",  # UCG/UDM/UXG all report type "udm"
            "usg": "gateway",
            "usw": "switch",
            "uap": "ap",
            "ucxg": "gateway",
            "uap-ac": "ap",
            "uap-xg": "ap",
        }
        return mapping.get(unifi_type.lower(), "unknown")


def discover_from_unifi(
    base_url: str = "https://192.0.2.1:443",
    username: str = "admin",
    password: str = "",
) -> list:
    """Discover devices from a UniFi controller. Returns list of device dicts."""
    client = UniFiClient(base_url, username, password)
    if not client.login():
        logger.warning("Could not log in to UniFi Controller")
        return []
    devices = client.get_devices()
    logger.info(f"Discovered {len(devices)} UniFi devices")
    return devices
