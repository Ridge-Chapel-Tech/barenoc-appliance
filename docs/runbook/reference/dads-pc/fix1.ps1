<#
  fix1.ps1 — SAFE Windows PC cleanup + DNS-through-router override.
  Reference artifact from the dads-pc / PC-MINI session (2026-09-03).

  THIS FILE IS REFERENCE ONLY — it is not executed by BareNOC. The canonical,
  shipped forms are:
    * src/scripts/windows_cleanup.sh (the cleanup half, with the offender list
      + TEMP/recycle and honest before/after byte measurement), and
    * src/scripts/windows_netdiag.sh (the DNS-through-router detection + the
      1.1.1.1 override, gated on an elevated session).

  This file keeps the two halves together the way the original session ran
  them, annotated. Safe by construction:
    * NEVER uninstalls software, NEVER touches partitions;
    * the DNS override only runs when (a) requested, (b) elevated, and
      (c) the PC's DNS is actually pointed at the router/gateway.

  PARAMETERS (hard-coded defaults in this reference; configurable in the
  shipped actions):
    $offenders  = @('Adobe CollabSync', 'Copilot')   # autostart offenders
    $resolvers  = @('1.1.1.1', '1.0.0.1')            # non-router DNS fallback
    $applyDnsFix = $true                             # gate for the DNS override
#>
$ErrorActionPreference = 'SilentlyContinue'
$offenders = @('Adobe CollabSync', 'Copilot')
$resolvers = @('1.1.1.1', '1.0.0.1')
$applyDnsFix = $true

$report = @{}
$report.hostname = $env:COMPUTERNAME
$report.elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Norm([string]$s) { return (($s -replace '[^a-zA-Z0-9]', '').ToLower()) }
function MatchesOffender([string]$name) {
  $n = Norm $name
  foreach ($off in $offenders) {
    $o = Norm $off
    if ($o -and $n -like "*$o*") { return $true }
  }
  return $false
}

# ── Measure BEFORE (bytes recovered is honest) ──────────────────────────────
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

# ── Stop offenders + remove autostart ──────────────────────────────────────
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

# ── Clear TEMP + recycle ────────────────────────────────────────────────────
$tempLocked = @()
foreach ($tp in $tempPaths) {
  if (-not $tp -or -not (Test-Path $tp)) { continue }
  Get-ChildItem -Path $tp -Force -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item -Path $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $_.FullName) { $tempLocked += $_.FullName }
  }
}
Clear-RecycleBin -Force -ErrorAction SilentlyContinue

# ── Measure AFTER ───────────────────────────────────────────────────────────
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
$recovered = ($tempBefore - $tempAfter) + ($recycleBefore - $recycleAfter)
$report.bytes_recovered = [long]([math]::Max(0, $recovered))
$report.processes_stopped = @($processesStopped | Select-Object -Unique)
$report.autostart_removed = @($autostartRemoved)
$report.temp_locked_files = @($tempLocked | Select-Object -First 20)

# ── DNS-through-router detection + 1.1.1.1 override (GATED) ────────────────
$gw = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
      Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
$gateway = if ($gw) { $gw.NextHop } else { $null }

$dnsServers = @()
foreach ($addr in (Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
  foreach ($s in @($addr.ServerAddresses)) {
    if ($s -and ($s -notin $dnsServers)) { $dnsServers += $s }
  }
}
$viaRouter = ($gateway -and ($dnsServers -contains $gateway))

$fix = @{ requested = $applyDnsFix; applied = $false; reason = ''; changed_interfaces = @(); servers_now = @($dnsServers) }
if (-not $applyDnsFix) {
  $fix.reason = 'not requested — report-only'
} elseif (-not $report.elevated) {
  $fix.reason = 'standard (non-admin) session — DNS override needs elevation'
} elseif (-not $viaRouter) {
  $fix.reason = 'DNS-through-router weak spot not detected — no override applied'
} else {
  $changed = @()
  foreach ($a in (Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' })) {
    try {
      Set-DnsClientServerAddress -InterfaceIndex $a.ifIndex -ServerAddresses @($resolvers) -ErrorAction Stop
      $changed += $a.Name
    } catch { }
  }
  if ($changed.Count -gt 0) {
    $fix.applied = $true
    $fix.changed_interfaces = @($changed)
    $fix.servers_now = @($resolvers)
    $fix.reason = ''
  } else {
    $fix.reason = 'no active adapter accepted the override'
  }
}
$report.dns = @{ servers = @($dnsServers); via_router = $viaRouter; recommended = @($resolvers) }
$report.dns_fix = $fix

$report.success = $true
$report | ConvertTo-Json -Depth 6 -Compress
