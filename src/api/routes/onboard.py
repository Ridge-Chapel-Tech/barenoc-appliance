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

import base64

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


@router.get("/onboard/info")
def onboard_info():
    """Self-service onboarding info — public (the /onboard portal is public).
    The device installer fetches the appliance's own IP + DNS mapping + CA
    fingerprint here, so onboarding needs NO split-horizon DNS or external
    trust: everything comes from the appliance it already reached."""
    import os as _os
    ip = (_os.getenv("APPLIANCE_IP") or "").strip()
    return {
        "appliance_ip": ip,
        "hosts": [f"{ip} stepca.barenoc.local app.barenoc.com bareNOC.local"],
        "ca_fingerprint": root_fingerprint(),
    }


@router.get("/onboard/root-ca.crt")
def onboard_root_ca():
    """The appliance CA root cert (public artifact) — the installer saves it
    so step can validate the CA without fetching it over DNS."""
    from fastapi.responses import PlainTextResponse
    try:
        with open("/opt/barenoc/volumes/step-ca/certs/root_ca.crt") as f:
            pem = f.read()
    except Exception:
        pem = ""
    return PlainTextResponse(pem, media_type="application/x-pem-file")


def _browser_trust_block(os_name: str) -> str:
    """The OPT-IN "trust the BareNOC root in this machine's browsers" step.

    Default OFF; explicit consent only; never installs silently. The returned
    shell text contains no brace characters, so it can be interpolated into an
    f-string (the surrounding script templates) without escaping.
    """
    if os_name == "mac":
        resolve_home = (
            'FF_HOME=$(dscl . -read "/Users/$SUDO_USER" NFSHomeDirectory 2>/dev/null | sed -n "s/^NFSHomeDirectory: //p")\n'
        )
        os_install = (
            '  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain /etc/barenoc-ca/root_ca.crt\n'
        )
        undo = 'sudo security delete-certificate -c "BareNOC Internal CA Root"'
        ff_glob = '"$FF_HOME/Library/Application Support/Firefox/Profiles/"*'
    else:
        resolve_home = (
            'FF_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)\n'
        )
        os_install = (
            '  install -d -m 0755 /usr/local/share/ca-certificates\n'
            '  install -m 0644 /etc/barenoc-ca/root_ca.crt /usr/local/share/ca-certificates/barenoc-root.crt\n'
            '  if command -v update-ca-certificates >/dev/null 2>&1; then\n'
            '    update-ca-certificates\n'
            '  else\n'
            '    echo "  !!! update-ca-certificates not found (non-Debian?) — root copied but not activated"\n'
            '  fi\n'
        )
        undo = 'rm /usr/local/share/ca-certificates/barenoc-root.crt && update-ca-certificates'
        ff_glob = '"$FF_HOME/.mozilla/firefox/"*'

    return (
        "# --- Optional: trust the BareNOC root CA in this machine's browsers ---\n"
        "TRUST_ROOT=0\n"
        'if [ "$1" = "--trust-root" ]; then\n'
        "  TRUST_ROOT=1\n"
        "fi\n"
        "\n"
        'if [ "$TRUST_ROOT" = "0" ] && [ -t 0 ]; then\n'
        "  echo\n"
        "  echo \"Optional: trust the BareNOC root CA so this machine's browsers stop\"\n"
        "  echo \"showing 'Not Secure' on $APP. This only affects certificates signed by\"\n"
        "  echo \"the BareNOC CA — nothing else is trusted.\"\n"
        "  printf \"Trust the BareNOC root CA for this machine's browsers? [y/N] \" > /dev/tty\n"
        "  read -r ANS < /dev/tty || ANS=N\n"
        '  case "$ANS" in\n'
        "    y|Y|yes|YES|Yes) TRUST_ROOT=1 ;;\n"
        '    *) echo "  (declined — root NOT added; re-run with --trust-root to opt in)" ;;\n'
        "  esac\n"
        'elif [ "$TRUST_ROOT" = "0" ]; then\n'
        '  echo "  (browser trust skipped — pass --trust-root to opt in non-interactively)"\n'
        "fi\n"
        "\n"
        'if [ "$TRUST_ROOT" = "1" ]; then\n'
        '  echo "==> Trusting the BareNOC root CA (opt-in) — $APP will show as secure"\n'
        '  echo "    Scope: only certificates signed by the BareNOC CA. Undo anytime with:"\n'
        "  echo '      " + undo + "'\n"
        "  FF_HOME=\"$HOME\"\n"
        '  if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then\n'
        "    " + resolve_home +
        '    [ -n "$FF_HOME" ] || FF_HOME="$HOME"\n'
        "  fi\n"
        + os_install +
        '  if command -v certutil >/dev/null 2>&1; then\n'
        "    for d in " + ff_glob + "; do\n"
        '      [ -d "$d" ] || continue\n'
        '      [ -f "$d/cert9.db" ] || [ -f "$d/cert8.db" ] || continue\n'
        '      if certutil -A -n "BareNOC Internal CA Root" -t "C,," -i /etc/barenoc-ca/root_ca.crt -d "sql:$d" >/dev/null 2>&1; then\n'
        '        echo "    Firefox: trusted in profile $d"\n'
        "      else\n"
        '        echo "    Firefox: import into $d failed (non-fatal)"\n'
        "      fi\n"
        "    done\n"
        "  else\n"
        '    echo "  !!! Firefox NOT covered: certutil missing."\n'
        '    echo "      Manual: certutil -A -n \\"BareNOC Internal CA Root\\" -t \\"C,,\\" -i /etc/barenoc-ca/root_ca.crt -d sql:<profile-dir>"\n'
        "  fi\n"
        '  echo "  Verify (should print \'HTTP/1.1 200 OK\' with no -k):"\n'
        '  curl -sI "$APP" 2>/dev/null | head -1 || echo "  (could not reach $APP over HTTPS)"\n'
        "fi\n"
    )


def _linux_script(app_url: str) -> str:
    key = ensure_control_key()["public_key"]
    fp = root_fingerprint()
    trust = _browser_trust_block("linux")
    return f"""#!/bin/bash
# BareNOC device onboarding (Linux/macOS) — run with your admin rights.
set -e
APP="{app_url}"
HOST=$(hostname | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-._' | cut -c1-60)
CN="device-${{HOST:-node}}"

# Fetch trust + the appliance's DNS mapping from the appliance itself — this
# script must not depend on split-horizon DNS (stepca.barenoc.local only
# resolves via the appliance's CoreDNS) or on external trust. Everything
# comes from the URL it already reached.
mkdir -p /etc/barenoc-ca
APP_IP=$(curl -sk "$APP/onboard/info" | grep -o '"appliance_ip": *"[^"]*"' | head -1 | cut -d'"' -f4)
curl -sk "$APP/onboard/root-ca.crt" -o /etc/barenoc-ca/root_ca.crt
if [ -n "$APP_IP" ] && echo "$APP_IP" | grep -qE '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$'; then
  # REPLACE any existing entry — a laptop previously onboarded to another
  # appliance (or a changed DHCP address) can carry a stale IP here, which
  # silently breaks enrollment.
  sed -i '/stepca\\.barenoc\\.local/d' /etc/hosts
  echo "$APP_IP stepca.barenoc.local app.barenoc.com bareNOC.local" >> /etc/hosts
fi

echo "==> Enabling SSH so BareNOC can reach this device"
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now sshd 2>/dev/null || systemctl enable --now ssh 2>/dev/null || true
elif [ -x /etc/init.d/ssh ]; then
  /etc/init.d/ssh start 2>/dev/null || true
fi
# open the firewall if one is active (Fedora/RHEL firewalld, Ubuntu ufw) —
# sshd running behind a closed port is still unreachable.
if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active firewalld >/dev/null 2>&1; then
  firewall-cmd --permanent --add-service=ssh >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
fi
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow ssh >/dev/null 2>&1 || true
fi

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

# Everything below logs to /var/log/barenoc-onboard.log too (diagnostics).
exec > >(tee /var/log/barenoc-onboard.log) 2>&1

echo "==> Checking appliance DNS + CA reachability"
if getent hosts stepca.barenoc.local >/dev/null 2>&1; then
  echo "  stepca.barenoc.local resolves -> $(getent hosts stepca.barenoc.local | head -1)"
else
  echo "  !!! stepca.barenoc.local does NOT resolve (the /etc/hosts entry may not have been written)"
fi
curl -sk -o /dev/null -w '  CA health (https://stepca.barenoc.local:8443): HTTP %{{http_code}}\\n' \
  https://stepca.barenoc.local:8443/health || echo "  !!! CA not reachable — check routing/DNS"

echo "==> Trusting the BareNOC CA + enrolling this device"
export STEPPATH=/root/.step
rm -f /etc/barenoc-device.crt /etc/barenoc-device.key
if step ca bootstrap --ca-url https://stepca.barenoc.local:8443 --fingerprint {fp} --force </dev/null >/dev/null 2>&1; then
  echo "  CA bootstrap OK (root pinned by fingerprint)"
else
  echo "  !!! CA bootstrap failed — see above"
fi
if step ca certificate "$CN" /etc/barenoc-device.crt /etc/barenoc-device.key --token "$(curl -sk "$APP/onboard/token?cn=$CN" | tr -d '"')" --root /etc/barenoc-ca/root_ca.crt 2>/dev/null; then
  echo "  certificate enrolled -> /etc/barenoc-device.crt"
else
  echo "  !!! certificate enrollment FAILED"
fi
cat > /usr/local/bin/barenoc-device-heartbeat.sh <<'HEART'
#!/bin/bash
/usr/local/bin/step ca renew /etc/barenoc-device.crt /etc/barenoc-device.key 2>/dev/null || true
curl -sk --cert /etc/barenoc-device.crt --key /etc/barenoc-device.key -X POST "{app_url}/api/v1/device/report" >/dev/null 2>&1 || true
HEART
chmod +x /usr/local/bin/barenoc-device-heartbeat.sh
( crontab -l 2>/dev/null | grep -v barenoc-device-heartbeat; echo "*/10 * * * * /usr/local/bin/barenoc-device-heartbeat.sh" ) | crontab -

echo "==> Verifying the handshake — talking back to BareNOC over mTLS"
sleep 2
printf '{{"hostname": "%s"}}' "$(hostname)" > /tmp/barenoc-report.json
REPORT=$(curl -sk --cert /etc/barenoc-device.crt --key /etc/barenoc-device.key -X POST "$APP/api/v1/device/report" -H "Content-Type: application/json" --data @/tmp/barenoc-report.json 2>/dev/null)
rm -f /tmp/barenoc-report.json
if echo "$REPORT" | grep -q '"ok": *true'; then
  DEV=$(echo "$REPORT" | grep -o '"device": *"[^"]*"' | head -1 | cut -d'"' -f4)
  ADOPT=$(echo "$REPORT" | grep -o '"adopted": *"[^"]*"' | head -1 | cut -d'"' -f4)
  echo "✅ Handshake verified — BareNOC adopted this device as '$DEV' (adoption: $ADOPT, online)."
  if command -v zenity >/dev/null 2>&1; then
    zenity --info --title="BareNOC" --text="✅ This device is now onboarded to BareNOC as $DEV" 2>/dev/null || true
  elif command -v kdialog >/dev/null 2>&1; then
    kdialog --title "BareNOC" --msgbox "✅ This device is now onboarded to BareNOC as $DEV" >/dev/null 2>&1 || true
  fi
else
  echo "❌ Handshake test failed. Check that this device can reach $APP and that"
  echo "   stepca.barenoc.local resolves (same LAN = automatic via the appliance DNS)."
  echo "   Raw reply: $REPORT"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="BareNOC" --text="❌ The BareNOC handshake failed — see the terminal output." 2>/dev/null || true
  elif command -v kdialog >/dev/null 2>&1; then
    kdialog --title "BareNOC" --error "❌ The BareNOC handshake failed — see the terminal output." >/dev/null 2>&1 || true
  fi
fi

{trust}
echo "==> Done. This device is now adopted by BareNOC (it appears online within a minute)."
"""


def _mac_script(app_url: str) -> str:
    # macOS: same as Linux but the barenoc user is created via dscl and Remote
    # Login is assumed enabled; scoped sudo via the current admin user instead.
    key = ensure_control_key()["public_key"]
    fp = root_fingerprint()
    trust = _browser_trust_block("mac")
    return f"""#!/bin/bash
# BareNOC device onboarding (macOS) — run with your admin rights.
set -e
APP="{app_url}"
HOST=$(scutil --get LocalHostName 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-._' | cut -c1-60)
CN="device-${{HOST:-mac}}"
U=$(id -un)

# Fetch trust + the appliance's DNS mapping from the appliance itself (see
# the Linux script — no split-horizon DNS or external trust needed).
mkdir -p /etc/barenoc-ca
APP_IP=$(curl -sk "$APP/onboard/info" | grep -o '"appliance_ip": *"[^"]*"' | head -1 | cut -d'"' -f4)
curl -sk "$APP/onboard/root-ca.crt" -o /etc/barenoc-ca/root_ca.crt
if [ -n "$APP_IP" ] && echo "$APP_IP" | grep -qE '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$'; then
  # REPLACE any existing entry — a laptop previously onboarded to another
  # appliance (or a changed DHCP address) can carry a stale IP here, which
  # silently breaks enrollment.
  sed -i '/stepca\\.barenoc\\.local/d' /etc/hosts
  echo "$APP_IP stepca.barenoc.local app.barenoc.com bareNOC.local" >> /etc/hosts
fi

echo "==> Enabling Remote Login (SSH) so BareNOC can reach this Mac"
systemsetup -setremotelogin on >/dev/null 2>&1 || true

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
step ca bootstrap --ca-url https://stepca.barenoc.local:8443 --fingerprint {fp} --force </dev/null >/dev/null 2>&1 || true
step ca certificate "$CN" /etc/barenoc-device.crt /etc/barenoc-device.key --token "$(curl -sk "$APP/onboard/token?cn=$CN" | tr -d '"')" --root /etc/barenoc-ca/root_ca.crt 2>/dev/null || true

cat > /usr/local/bin/barenoc-device-heartbeat.sh <<'HEART'
#!/bin/bash
/usr/local/bin/step ca renew /etc/barenoc-device.crt /etc/barenoc-device.key 2>/dev/null || true
curl -sk --cert /etc/barenoc-device.crt --key /etc/barenoc-device.key -X POST "{app_url}/api/v1/device/report" >/dev/null 2>&1 || true
HEART
chmod +x /usr/local/bin/barenoc-device-heartbeat.sh
( crontab -l 2>/dev/null | grep -v barenoc-device-heartbeat; echo "*/10 * * * * /usr/local/bin/barenoc-device-heartbeat.sh" ) | crontab -

echo "==> Verifying the handshake — talking back to BareNOC over mTLS"
sleep 2
printf '{{"hostname": "%s"}}' "$(hostname)" > /tmp/barenoc-report.json
REPORT=$(curl -sk --cert /etc/barenoc-device.crt --key /etc/barenoc-device.key -X POST "$APP/api/v1/device/report" -H "Content-Type: application/json" --data @/tmp/barenoc-report.json 2>/dev/null)
rm -f /tmp/barenoc-report.json
if echo "$REPORT" | grep -q '"ok": *true'; then
  DEV=$(echo "$REPORT" | grep -o '"device": *"[^"]*"' | head -1 | cut -d'"' -f4)
  ADOPT=$(echo "$REPORT" | grep -o '"adopted": *"[^"]*"' | head -1 | cut -d'"' -f4)
  echo "✅ Handshake verified — BareNOC adopted this device as '$DEV' (adoption: $ADOPT, online)."
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display dialog \"✅ This Mac is now onboarded to BareNOC as '$DEV'\" with title \"BareNOC\" buttons {{\"OK\"}} with icon note" >/dev/null 2>&1 || true
  fi
else
  echo "❌ Handshake test failed. Check that this device can reach $APP and that"
  echo "   stepca.barenoc.local resolves (same LAN = automatic via the appliance DNS)."
  echo "   Raw reply: $REPORT"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display dialog \"❌ The BareNOC handshake failed — see the terminal output for details.\" with title \"BareNOC\" buttons {{\"OK\"}} with icon stop" >/dev/null 2>&1 || true
  fi
fi

{trust}
echo "==> Done. This device is now adopted by BareNOC."
"""


def _win_script(app_url: str) -> str:
    key = ensure_control_key()["public_key"]
    return f"""# BareNOC device onboarding (Windows) — GUI dialog, run as admin.
$ErrorActionPreference = 'Stop'
$APP = "{app_url}"
$CN = "device-" + ($env:COMPUTERNAME -replace '[^a-zA-Z0-9._-]', '').ToLower()

# --- small native progress dialog (WinForms; no console window) ---
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$f = New-Object System.Windows.Forms.Form
$f.Text = 'BareNOC — connecting this device'
$f.ClientSize = New-Object System.Drawing.Size(480, 120)
$f.FormBorderStyle = 'FixedSingle'
$f.StartPosition = 'CenterScreen'
$f.TopMost = $true
$lbl = New-Object System.Windows.Forms.Label
$lbl.Location = New-Object System.Drawing.Point(18, 16)
$lbl.Size = New-Object System.Drawing.Size(444, 84)
$lbl.Font = New-Object System.Drawing.Font('Segoe UI', 10)
$f.Controls.Add($lbl)
function Set-Status([string]$t, [string]$color = 'Black') {{
  $script:lbl.Text = $t
  $script:lbl.ForeColor = [System.Drawing.Color]::FromName($color)
  [System.Windows.Forms.Application]::DoEvents()
}}
$f.Show()
Set-Status 'Creating the barenoc control account...'

try {{
  if (-not (Get-LocalUser -Name barenoc -ErrorAction SilentlyContinue)) {{
    New-LocalUser -Name barenoc -NoPassword -AccountNeverExpires | Out-Null
  }}
  $sshDir = 'C:/ProgramData/ssh'
  New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
  Add-Content -Path "$sshDir/administrators_authorized_keys" -Value "{key}" -Force
  Add-LocalGroupMember -Group Administrators -Member barenoc -ErrorAction SilentlyContinue
  Set-Status 'Admin control is set up — verifying the authorized key...'
  $raw = Get-Content "$sshDir/administrators_authorized_keys" -Raw
  if (-not ($raw -and $raw.Contains('ssh-ed25519'))) {{ throw 'the authorized key file was not written correctly' }}
  Set-Status "Done — BareNOC can control this PC as '$CN'." 'Green'
  Start-Sleep -Seconds 2
  $f.Close()
  [System.Windows.Forms.MessageBox]::Show("This PC is now onboarded to BareNOC as`n$CN`n`nSSH admin control is configured. Certificate adoption is the tracked Windows milestone.", 'BareNOC — done', 'OK', 'Information')
}} catch {{
  Set-Status ('Failed: ' + $_.Exception.Message) 'Red'
  $f.Close()
  [System.Windows.Forms.MessageBox]::Show("Onboarding failed:`n" + $_.Exception.Message, 'BareNOC', 'OK', 'Error')
  exit 1
}}
"""


def _win_bat(app_url: str) -> str:
    """Double-click .bat wrapper: self-elevates via a UAC prompt, then runs the
    onboarding PowerShell base64-encoded with all windows hidden — the customer
    sees a native progress dialog, then a result box. No console at all."""
    ps = _win_script(app_url)
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        ":: BareNOC device onboarding — double-click, accept the UAC prompt.\r\n"
        "net session >nul 2>&1\r\n"
        "if %errorlevel% neq 0 (\r\n"
        "  powershell -NoProfile -WindowStyle Hidden -Command \"Start-Process -FilePath '%~f0' -Verb RunAs -WindowStyle Hidden\"\r\n"
        "  exit /b\r\n"
        ")\r\n"
        "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand " + encoded + "\r\n"
    )


@router.get("/onboard/script")
def onboard_script(request: Request, os: str = "linux"):
    app = _app(request)
    if os == "windows":
        # double-click .bat (self-elevating); the ps1 is embedded base64
        body, media, filename = _win_bat(app), "application/x-msdos-program", "bareNOC-onboard.bat"
    elif os == "mac":
        body, media, filename = _mac_script(app), "text/x-shellscript", "bareNOC-onboard-mac.sh"
    else:
        body, media, filename = _linux_script(app), "text/x-shellscript", "bareNOC-onboard.sh"
    from fastapi.responses import Response
    headers = {"Content-Disposition": f'attachment; filename="{filename}"',
               "Cache-Control": "no-store"}  # never serve a stale (inline-viewed) copy
    return Response(body, media_type=media, headers=headers)


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
  <p class="mt-2 text-xs text-gray-500">🔒 <b>Optional (default No):</b> the script asks whether to trust the BareNOC
    root CA in this machine's browsers so <code>https://&lt;appliance-ip&gt;</code> and <code>app.barenoc.com</code>
    stop showing &ldquo;Not Secure&rdquo;. It <b>only</b> affects certificates signed by the BareNOC CA — nothing
    else. Undo: <code>rm /usr/local/share/ca-certificates/barenoc-root.crt && update-ca-certificates</code>
    (Linux) / <code>sudo security delete-certificate -c "BareNOC Internal CA Root"</code> (macOS).</p>
</div>
<script>
function pick() {{
  var os = document.getElementById('os').value;
  var url = window.location.origin + '/onboard/script?os=' + os;
  var dl = document.getElementById('dl');
  dl.href = url;
  dl.download = os === 'windows' ? 'bareNOC-onboard.bat' : (os === 'mac' ? 'bareNOC-onboard-mac.sh' : 'bareNOC-onboard.sh');
  var h = document.getElementById('howto');
  if (os === 'windows') h.textContent = 'Download the file, then double-click it and accept the User Account Control (UAC) prompt.';
  else if (os === 'mac') h.textContent = 'Download the file, then run:  bash bareNOC-onboard-mac.sh  (enter your password when prompted)';
  else h.textContent = 'Download the file, then run:  sudo bash bareNOC-onboard.sh';
}}
pick();
</script></body></html>""")





@router.get("/onboard/token")
def onboard_token(request: Request, cn: str = ""):
    """Mint an enrollment token for a self-onboarding device (the cert CN)."""
    from fastapi.responses import PlainTextResponse
    from step_ca import mint_token
    if not cn.startswith("device-") or len(cn) > 128:
        return PlainTextResponse("bad cn", status_code=400)
    return PlainTextResponse(mint_token(cn, ttl=900))
