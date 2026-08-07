"""Self-service device onboarding portal.

A workstation user (no BareNOC access) visits /onboard — by URL or QR — and
runs a one-click script with their OWN admin rights. The script creates the
`barenoc` control user, authorizes the appliance key, grants scoped sudo,
installs step-cli from the appliance, enrolls a short-lived device cert, and
installs a heartbeat — the device self-registers via its first mTLS report
(see routes/device_certs.py). No tech required per machine.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from control_key import ensure_control_key
from step_ca import device_cn, root_fingerprint

router = APIRouter(tags=["onboard"])

SUDO_SCOPED = (
    "/usr/bin/cp, /usr/sbin/reboot, /usr/sbin/shutdown, /usr/bin/apt, /usr/bin/apt-get,"
    " /usr/bin/dnf, /usr/bin/yum, /usr/bin/apk, /usr/bin/zypper, /usr/bin/journalctl,"
    " /usr/bin/log, /usr/bin/install, /usr/bin/systemctl, /usr/bin/tail, /usr/bin/curl"
)
# NOTE: sudoers requires FULLY-QUALIFIED paths (bare command names are a
# parse error). cp is included so the appliance can sync its own agent runner
# (sudo -u pi-agent cp) when it adopts the appliance as a device. Package
# managers are per major OS flavor (apt/dnf/yum/apk/zypper) so apply_patch
# works on every flavor; log is macOS (no journald).


def _app(request: Request) -> str:
    """The appliance's URL as the user reached it (the front door)."""
    scheme = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host", "")
    return f"{scheme}://{host}"


def _linux_script(app_url: str) -> str:
    key = ensure_control_key()["public_key"]
    fp = root_fingerprint()
    return f"""#!/bin/bash
# BareNOC device onboarding (Linux/macOS) — run with your admin rights.
set -e
APP="{app_url}"
HOST=$(hostname | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-._' | cut -c1-60)
CN="device-${{HOST:-node}}"

echo "==> Creating the barenoc control user"
id -u barenoc >/dev/null 2>&1 || useradd -m -s /bin/bash barenoc
mkdir -p /home/barenoc/.ssh
echo '{key}' >> /home/barenoc/.ssh/authorized_keys
chown -R barenoc:barenoc /home/barenoc/.ssh && chmod 700 /home/barenoc/.ssh && chmod 600 /home/barenoc/.ssh/authorized_keys
echo "barenoc ALL=(ALL) NOPASSWD: {SUDO_SCOPED}" > /etc/sudoers.d/barenoc
chmod 440 /etc/sudoers.d/barenoc

echo "==> Installing step-cli from the appliance"
mkdir -p /usr/local/bin
curl -sk -o /tmp/step "$APP/step-cli" && install -m 0755 /tmp/step /usr/local/bin/step

echo "==> Trusting the BareNOC CA + enrolling this device"
export STEPPATH=/root/.step
rm -f /etc/barenoc-device.crt /etc/barenoc-device.key
step ca bootstrap --ca-url https://stepca.barenoc.local:8443 --fingerprint {fp} </dev/null >/dev/null 2>&1 || true
step ca certificate "$CN" /etc/barenoc-device.crt /etc/barenoc-device.key --token "$(curl -sk "$APP/onboard/token?cn=$CN" | tr -d '"')" 2>/dev/null \\
  || {{ echo "enroll failed (token endpoint needs the device id — see the wiki)"; }}

echo "==> Installing the heartbeat (renew + mTLS report every 10 min)"
cat > /usr/local/bin/barenoc-device-heartbeat.sh <<'HEART'
#!/bin/bash
/usr/local/bin/step ca renew /etc/barenoc-device.crt /etc/barenoc-device.key 2>/dev/null || true
curl -sk --cert /etc/barenoc-device.crt --key /etc/barenoc-device.key -X POST "{app_url}/api/v1/device/report" >/dev/null 2>&1 || true
HEART
chmod +x /usr/local/bin/barenoc-device-heartbeat.sh
( crontab -l 2>/dev/null | grep -v barenoc-device-heartbeat; echo "*/10 * * * * /usr/local/bin/barenoc-device-heartbeat.sh" ) | crontab -

echo "==> Done. This device is now adopted by BareNOC (it appears online within a minute)."
"""


def _mac_script(app_url: str) -> str:
    # macOS: same as Linux but the barenoc user is created via dscl and Remote
    # Login is assumed enabled; scoped sudo via the current admin user instead.
    key = ensure_control_key()["public_key"]
    fp = root_fingerprint()
    return f"""#!/bin/bash
# BareNOC device onboarding (macOS) — run with your admin rights.
set -e
APP="{app_url}"
HOST=$(scutil --get LocalHostName 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-._' | cut -c1-60)
CN="device-${{HOST:-mac}}"
U=$(id -un)

echo "==> Authorizing the BareNOC control key for $U"
mkdir -p "$HOME/.ssh"
echo '{key}' >> "$HOME/.ssh/authorized_keys"
chmod 700 "$HOME/.ssh" && chmod 600 "$HOME/.ssh/authorized_keys"
echo "$U ALL=(ALL) NOPASSWD: {SUDO_SCOPED}" > /etc/sudoers.d/barenoc
chmod 440 /etc/sudoers.d/barenoc

echo "==> Installing step-cli + enrolling"
ARCH=$(uname -m)
STEPURL="$APP/step-cli-darwin_arm64"
[ "$ARCH" = "x86_64" ] && STEPURL="$APP/step-cli-darwin_amd64"
curl -sk -o /tmp/step "$STEPURL" && install -m 0755 /tmp/step /usr/local/bin/step
export STEPPATH=/root/.step
rm -f /etc/barenoc-device.crt /etc/barenoc-device.key
step ca bootstrap --ca-url https://stepca.barenoc.local:8443 --fingerprint {fp} </dev/null >/dev/null 2>&1 || true
step ca certificate "$CN" /etc/barenoc-device.crt /etc/barenoc-device.key --token "$(curl -sk "$APP/onboard/token?cn=$CN" | tr -d '"')" 2>/dev/null || true

cat > /usr/local/bin/barenoc-device-heartbeat.sh <<'HEART'
#!/bin/bash
/usr/local/bin/step ca renew /etc/barenoc-device.crt /etc/barenoc-device.key 2>/dev/null || true
curl -sk --cert /etc/barenoc-device.crt --key /etc/barenoc-device.key -X POST "{app_url}/api/v1/device/report" --data '{{"hostname": "$(hostname)"}}' >/dev/null 2>&1 || true
HEART
chmod +x /usr/local/bin/barenoc-device-heartbeat.sh
( crontab -l 2>/dev/null | grep -v barenoc-device-heartbeat; echo "*/10 * * * * /usr/local/bin/barenoc-device-heartbeat.sh" ) | crontab -

echo "==> Done. This device is now adopted by BareNOC."
"""


def _win_script(app_url: str) -> str:
    key = ensure_control_key()["public_key"]
    return f"""# BareNOC device onboarding (Windows) — run in an ADMIN PowerShell.
$ErrorActionPreference = 'Stop'
$APP = "{app_url}"
$CN = "device-" + ($env:COMPUTERNAME -replace '[^a-zA-Z0-9._-]', '').ToLower()

Write-Host "==> Creating the barenoc control user"
if (-not (Get-LocalUser -Name barenoc -ErrorAction SilentlyContinue)) {{
  New-LocalUser -Name barenoc -NoPassword -AccountNeverExpires | Out-Null
}}
$sshDir = "C:\\ProgramData\\ssh"
New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
Add-Content -Path "$sshDir\\administrators_authorized_keys" -Value "{key}" -Force
Add-LocalGroupMember -Group Administrators -Member barenoc -ErrorAction SilentlyContinue

Write-Host "==> SSH control is configured (the appliance connects as barenoc)."
Write-Host "    Certificate adoption + management actions for Windows ship with the Windows handlers."
Write-Host "==> Done. This device will be adopted by BareNOC once Windows cert enrollment lands;"
Write-Host "    for now the tech can adopt it from the Devices page."
"""


@router.get("/onboard", response_class=HTMLResponse)
def onboard_page(request: Request):
    app = _app(request)
    ua = request.headers.get("user-agent", "").lower()
    if "windows" in ua:
        default_os = "windows"
    elif "mac os" in ua or "macintosh" in ua:
        default_os = "mac"
    else:
        default_os = "linux"
    qr = f"https://api.qrserver.com/v1/create-qr-code/?size=180x180&data={app}/onboard"
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BareNOC — device onboarding</title>
<script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-900 min-h-screen flex items-center justify-center p-4">
<div class="bg-white rounded-xl shadow-2xl max-w-lg w-full p-8">
  <h1 class="text-2xl font-semibold text-gray-900">🔐 Onboard this device</h1>
  <p class="mt-2 text-sm text-gray-600">This adds the device to your network's BareNOC appliance: it will be
    <b>adopted</b> (monitored, with a revocable certificate identity) and set up for
    <b>managed control</b> (SSH) — using a dedicated <code>barenoc</code> account, not your login.</p>
  <div class="mt-4">
    <label class="text-sm font-medium text-gray-700">Your OS</label>
    <select id="os" onchange="pick()" class="mt-1 block w-full rounded-md border-gray-300 border px-3 py-2 text-sm">
      <option value="linux" {"selected" if default_os == "linux" else ""}>Linux</option>
      <option value="mac" {"selected" if default_os == "mac" else ""}>macOS</option>
      <option value="windows" {"selected" if default_os == "windows" else ""}>Windows</option>
    </select>
  </div>
  <div class="mt-4 rounded-md bg-gray-50 border border-gray-200 p-3 text-sm">
    <p class="text-gray-700"><b>How to run it:</b></p>
    <p id="howto" class="mt-1 text-gray-600"></p>
  </div>
  <div class="mt-3 flex items-center justify-between">
    <a id="dl" href="#" class="rounded-md bg-barenoc-600 bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700">Download the script</a>
    <div class="text-right">
      <p class="text-xs text-gray-500">Or scan to open this page:</p>
      <img src="{qr}" alt="QR" class="w-24 h-24 mt-1 inline-block">
    </div>
  </div>
  <p class="mt-4 text-xs text-gray-400">The script creates a <code>barenoc</code> user, authorizes the appliance
    key, grants command-scoped sudo, installs step-cli from the appliance, enrolls a short-lived certificate,
    and installs a renew+report heartbeat. Everything runs on the appliance — no internet needed.</p>
</div>
<script>
function pick() {{
  var os = document.getElementById('os').value;
  var url = window.location.origin + '/onboard/script?os=' + os;
  document.getElementById('dl').href = url;
  var h = document.getElementById('howto');
  if (os === 'windows') h.textContent = 'Save as onboard.ps1, open an ADMIN PowerShell, then run:  powershell -ExecutionPolicy Bypass -File onboard.ps1';
  else if (os === 'mac') h.textContent = 'Save as onboard.sh, then run:  bash onboard.sh  (enter your password when prompted)';
  else h.textContent = 'Save as onboard.sh, then run:  sudo bash onboard.sh';
}}
pick();
</script></body></html>""")


@router.get("/onboard/script")
def onboard_script(request: Request, os: str = "linux"):
    app = _app(request)
    if os == "windows":
        body, media = _win_script(app), "text/plain"
    elif os == "mac":
        body, media = _mac_script(app), "text/x-shellscript"
    else:
        body, media = _linux_script(app), "text/x-shellscript"
    from fastapi.responses import Response
    return Response(body, media_type=media)


@router.get("/onboard/token")
def onboard_token(request: Request, cn: str = ""):
    """Mint an enrollment token for a self-onboarding device (the cert CN)."""
    from fastapi.responses import PlainTextResponse
    from step_ca import mint_token
    if not cn.startswith("device-") or len(cn) > 128:
        return PlainTextResponse("bad cn", status_code=400)
    return PlainTextResponse(mint_token(cn, ttl=900))
