#!/bin/bash
# windows_cleanup.sh <target_ip> [ssh_user] [ssh_key] [offenders_csv]
#
# F8 — Windows PC SAFE cleanup. Mirrors the proven dads-pc session
# (2026-09-03) / fix1.ps1:
#   * stops + removes autostart for known offenders (configurable list —
#     default: Adobe CollabSync, Copilot);
#   * clears TEMP + empties the recycle bin;
#   * reports bytes recovered, and measures BEFORE any removal so the
#     recovered amount is honest.
#
# SAFE BY CONSTRUCTION: this script never runs partition operations and never
# uninstalls software. Dangerous ops (partition ops, uninstalls) have no code
# path here — they require an explicit per-device owner confirmation outside
# this action.
#
# offenders_csv: comma-separated offender names (defaults to the embedded
# list, kept in sync with action_validator.DEFAULT_WINDOWS_CLEANUP_OFFENDERS).

TARGET="${1:-}"
SSH_USER="${2:-barenoc}"
SSH_KEY="${3:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"
OFFENDERS_CSV="${4:-}"

if [ -z "$TARGET" ]; then
  echo '{"success": false, "error": "No target specified"}'
  exit 1
fi
if [ ! -f "$SSH_KEY" ]; then
  echo "{\"success\": false, \"error\": \"SSH key not found: $SSH_KEY\"}"
  exit 1
fi

# JSON-encode the offender list (injection-safe: only the base64 reaches the
# endpoint, and PowerShell decodes it via ConvertFrom-Json).
if [ -n "$OFFENDERS_CSV" ]; then
  OFFENDERS_JSON=$(printf '%s' "$OFFENDERS_CSV" | python3 -c 'import sys,json; print(json.dumps([o.strip() for o in sys.stdin.read().split(",") if o.strip()]))' 2>/dev/null)
fi
if [ -z "$OFFENDERS_JSON" ]; then
  OFFENDERS_JSON=''
fi
OFFENDERS_B64=$(printf '%s' "$OFFENDERS_JSON" | base64 -w0 2>/dev/null || printf '%s' "$OFFENDERS_JSON" | base64)

PS_SCRIPT=$(cat <<'POWERSHELL'
$ErrorActionPreference = 'SilentlyContinue'
$offendersJsonB64 = '__OFFENDERS_B64__'
# Default offender list — keep in sync with action_validator.DEFAULT_WINDOWS_CLEANUP_OFFENDERS.
$offenders = @('Adobe CollabSync', 'Copilot')
if ($offendersJsonB64 -and $offendersJsonB64 -ne '__OFFENDERS_B64__') {
  try {
    $json = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($offendersJsonB64))
    $parsed = @($json | ConvertFrom-Json)
    if ($parsed.Count -gt 0) { $offenders = $parsed }
  } catch { }
}

$report = @{}
$report.hostname = $env:COMPUTERNAME
$report.offenders = @($offenders)

function Norm([string]$s) { return (($s -replace '[^a-zA-Z0-9]', '').ToLower()) }
function MatchesOffender([string]$name) {
  $n = Norm $name
  foreach ($off in $offenders) {
    $o = Norm $off
    if ($o -and $n -like "*$o*") { return $true }
  }
  return $false
}

# ── Measure BEFORE (bytes recovered is measured before any removal) ────────
$tempPaths = @($env:TEMP, "$env:WINDIR\Temp", "C:\Windows\Temp")
$tempBefore = 0
foreach ($tp in $tempPaths) {
  if ($tp -and (Test-Path $tp)) {
    $s = (Get-ChildItem -Path $tp -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    if ($s) { $tempBefore += [double]$s }
  }
}
$recycleBefore = 0
$rb = Get-ChildItem -Path 'C:\$Recycle.Bin' -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum
if ($rb.Sum) { $recycleBefore = [double]$rb.Sum }
$report.temp_before_bytes = [long]$tempBefore
$report.recycle_before_bytes = [long]$recycleBefore
$report.before_bytes = [long]($tempBefore + $recycleBefore)

# ── Stop offender processes + remove autostart entries ─────────────────────
$processesStopped = @()
$autostartRemoved = @()

foreach ($off in $offenders) {
  $o = Norm $off
  if (-not $o) { continue }
  Get-Process | Where-Object { (Norm $_.ProcessName) -like "*$o*" } | ForEach-Object {
    $processesStopped += $_.ProcessName
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
  }
}

$runKeys = @(
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
  'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
  'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run'
)
foreach ($rk in $runKeys) {
  if (-not (Test-Path $rk)) { continue }
  $props = Get-ItemProperty -Path $rk
  if (-not $props) { continue }
  $props.PSObject.Properties | Where-Object { $_.Name -notmatch '^PS' } | ForEach-Object {
    $name = $_.Name
    if (MatchesOffender $name) {
      Remove-ItemProperty -Path $rk -Name $name -ErrorAction SilentlyContinue
      $autostartRemoved += "$rk`:$name"
    }
  }
}

$startupFolders = @(
  "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup",
  "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Startup"
)
foreach ($sf in $startupFolders) {
  if (-not (Test-Path $sf)) { continue }
  Get-ChildItem -Path $sf -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {
    if (MatchesOffender $_.BaseName) {
      Remove-Item -Path $_.FullName -Force -ErrorAction SilentlyContinue
      $autostartRemoved += $_.FullName
    }
  }
}

# ── Clear TEMP + recycle ───────────────────────────────────────────────────
$tempLocked = @()
foreach ($tp in $tempPaths) {
  if (-not $tp -or -not (Test-Path $tp)) { continue }
  Get-ChildItem -Path $tp -Force -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item -Path $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $_.FullName) { $tempLocked += $_.FullName }
  }
}
Clear-RecycleBin -Force -ErrorAction SilentlyContinue

# ── Measure AFTER ──────────────────────────────────────────────────────────
$tempAfter = 0
foreach ($tp in $tempPaths) {
  if ($tp -and (Test-Path $tp)) {
    $s = (Get-ChildItem -Path $tp -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    if ($s) { $tempAfter += [double]$s }
  }
}
$recycleAfter = 0
$ra = Get-ChildItem -Path 'C:\$Recycle.Bin' -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum
if ($ra.Sum) { $recycleAfter = [double]$ra.Sum }

$report.temp_after_bytes = [long]$tempAfter
$report.recycle_after_bytes = [long]$recycleAfter
$recovered = ($tempBefore - $tempAfter) + ($recycleBefore - $recycleAfter)
$report.bytes_recovered = [long]([math]::Max(0, $recovered))
$report.processes_stopped = @($processesStopped | Select-Object -Unique)
$report.autostart_removed = @($autostartRemoved)
$report.temp_locked_files = @($tempLocked | Select-Object -First 20)
$report.success = $true
$report | ConvertTo-Json -Depth 6 -Compress
exit 0
POWERSHELL
)

# Inject the (base64) offender list into the script at the placeholder.
PS_SCRIPT="${PS_SCRIPT/__OFFENDERS_B64__/$OFFENDERS_B64}"

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

ESC=$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))' 2>/dev/null)
if [ -z "$ESC" ]; then
  ESC=$(printf '%s' "$OUT" | base64 -w0)
  echo "{\"success\": false, \"error_b64\": \"$ESC\", \"target\": \"$TARGET\"}"
else
  echo "{\"success\": false, \"error\": $ESC, \"target\": \"$TARGET\"}"
fi
exit $CODE
