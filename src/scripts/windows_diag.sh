#!/bin/bash
# windows_diag.sh <target_ip> [ssh_user] [ssh_key]
#
# F8 — Windows PC health diagnostics (READ-ONLY). Mirrors the proven dads-pc
# session (2026-09-03): volumes/disk-full, top CPU + RAM processes, startup
# items, Defender real-time status + signature age, recent 7-day
# critical/error events, boot times, and SMART counters where available.
#
# Transport: SSH into the Windows box (OpenSSH server, key-only — the same
# `barenoc` admin account the /onboard flow creates) and run an embedded
# PowerShell pass via -EncodedCommand. Emits ONE JSON object on stdout.
#
# Never writes to the endpoint. No destructive ops of any kind.

TARGET="${1:-}"
SSH_USER="${2:-barenoc}"
SSH_KEY="${3:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"

if [ -z "$TARGET" ]; then
  echo '{"success": false, "error": "No target specified"}'
  exit 1
fi
if [ ! -f "$SSH_KEY" ]; then
  echo "{\"success\": false, \"error\": \"SSH key not found: $SSH_KEY\"}"
  exit 1
fi

# ── Embedded PowerShell (read-only) ─────────────────────────────────────────
PS_SCRIPT=$(cat <<'POWERSHELL'
$ErrorActionPreference = 'SilentlyContinue'
$report = @{}

$report.hostname = $env:COMPUTERNAME
$os = Get-CimInstance Win32_OperatingSystem
$report.os = if ($os) { $os.Caption } else { 'unknown' }
$report.elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$report.collected_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')

# 1. Volumes / disk-full
$vols = @()
foreach ($d in (Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3")) {
  $size = [double]$d.Size
  $free = [double]$d.FreeSpace
  $pct = if ($size -gt 0) { [math]::Round($free / $size * 100, 1) } else { 0 }
  $vols += @{
    device = $d.DeviceID
    size_gb = [math]::Round($size / 1GB, 2)
    free_gb = [math]::Round($free / 1GB, 2)
    free_pct = $pct
    disk_full = ($pct -lt 10)
  }
}
$report.volumes = $vols

# 2. Top CPU + RAM processes
$report.top_cpu = @(Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 | ForEach-Object {
  @{ name = $_.ProcessName; pid = $_.Id; cpu_s = [math]::Round([double]$_.CPU, 1) }
})
$report.top_ram = @(Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 | ForEach-Object {
  @{ name = $_.ProcessName; pid = $_.Id; ram_mb = [math]::Round($_.WorkingSet64 / 1MB, 1) }
})

# 3. Startup items
$report.startup_items = @(Get-CimInstance Win32_StartupCommand | ForEach-Object {
  @{ name = $_.Name; command = $_.Command; location = $_.Location }
})

# 4. Defender real-time status + signature age
$def = @{ available = $false; real_time_enabled = $false; antivirus_enabled = $false;
          signature_last_updated = $null; signature_age_days = $null; signature_version = $null }
try {
  $mp = Get-MpComputerStatus
  if ($mp) {
    $def.available = $true
    $def.real_time_enabled = [bool]$mp.RealTimeProtectionEnabled
    $def.antivirus_enabled = [bool]$mp.AntivirusEnabled
    $def.signature_version = $mp.AntivirusSignatureVersion
    if ($mp.AntivirusSignatureLastUpdated) {
      $def.signature_last_updated = $mp.AntivirusSignatureLastUpdated.ToString('yyyy-MM-ddTHH:mm:ss')
    }
    if ($null -ne $mp.AntivirusSignatureAge) { $def.signature_age_days = [int]$mp.AntivirusSignatureAge }
  }
} catch { }
$report.defender = $def

# 5. Recent 7-day critical/error events (System + Application)
$since = (Get-Date).AddDays(-7)
$events = @()
foreach ($log in @('System','Application')) {
  $evs = Get-WinEvent -FilterHashtable @{ LogName = $log; Level = 1,2; StartTime = $since } -MaxEvents 25
  foreach ($e in $evs) {
    $msg = (($e.Message -replace '\s+', ' ').Trim())
    if ($msg.Length -gt 300) { $msg = $msg.Substring(0, 300) + '...' }
    $events += @{
      time = $e.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss')
      log = $log
      level = $e.LevelDisplayName
      id = $e.Id
      provider = $e.ProviderName
      message = $msg
    }
  }
}
$events = @($events | Sort-Object time -Descending | Select-Object -First 25)
$report.recent_events = $events
$report.recent_events_count = $events.Count

# 6. Boot times
$boot = @{ last_boot_time = $null; uptime_days = $null; recent_boots = @() }
if ($os -and $os.LastBootUpTime) {
  $boot.last_boot_time = $os.LastBootUpTime.ToString('yyyy-MM-ddTHH:mm:ss')
  $boot.uptime_days = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalDays, 2)
}
$recentBoots = @()
foreach ($be in (Get-WinEvent -FilterHashtable @{ LogName='System'; Id=6005 } -MaxEvents 10)) {
  $recentBoots += $be.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss')
}
$boot.recent_boots = $recentBoots
$report.boot = $boot

# 7. SMART counters where available (needs elevation + supported disks)
$smart = @{ available = $false; disks = @() }
try {
  $disks = Get-PhysicalDisk
  foreach ($pd in $disks) {
    $rc = $pd | Get-StorageReliabilityCounter
    if ($rc) {
      $smart.available = $true
      $smart.disks += @{
        device = $pd.DeviceId
        friendly_name = $pd.FriendlyName
        media_type = $pd.MediaType
        health = $pd.HealthStatus
        temperature_c = $rc.Temperature
        wear = $rc.Wear
        power_on_hours = $rc.PowerOnHours
        read_errors_total = $rc.ReadErrorsTotal
        write_errors_total = $rc.WriteErrorsTotal
      }
    } else {
      $smart.disks += @{
        device = $pd.DeviceId
        friendly_name = $pd.FriendlyName
        media_type = $pd.MediaType
        health = $pd.HealthStatus
      }
    }
  }
} catch { }
$report.smart = $smart

$report.success = $true
$report | ConvertTo-Json -Depth 6 -Compress
exit 0
POWERSHELL
)

# PowerShell -EncodedCommand expects UTF-16LE base64 (no BOM).
PS_B64=$(printf '%s' "$PS_SCRIPT" | iconv -f UTF-8 -t UTF-16LE 2>/dev/null | base64 -w0 2>/dev/null)
if [ -z "$PS_B64" ]; then
  PS_B64=$(printf '%s' "$PS_SCRIPT" | iconv -f UTF-8 -t UTF-16LE | base64)
fi

OUT=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR -o ConnectTimeout=15 "$SSH_USER@$TARGET" \
  "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $PS_B64" 2>&1)
CODE=$?

if [ $CODE -eq 0 ]; then
  echo "$OUT"
  exit 0
fi

# SSH transport failure — surface a JSON error (the runner parses stdout).
ESC=$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))' 2>/dev/null)
if [ -z "$ESC" ]; then
  ESC=$(printf '%s' "$OUT" | base64 -w0)
  echo "{\"success\": false, \"error_b64\": \"$ESC\", \"target\": \"$TARGET\"}"
else
  echo "{\"success\": false, \"error\": $ESC, \"target\": \"$TARGET\"}"
fi
exit $CODE
