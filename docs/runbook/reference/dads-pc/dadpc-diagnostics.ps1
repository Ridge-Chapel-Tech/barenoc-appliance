<#
  dadpc-diagnostics.ps1 — READ-ONLY Windows PC diagnostic battery.
  Reference artifact from the dads-pc / PC-MINI session (2026-09-03).

  THIS FILE IS REFERENCE ONLY — it is not executed by BareNOC. The canonical,
  shipped form is src/scripts/windows_diag.sh (this same PowerShell, embedded
  and run over SSH with -EncodedCommand).

  What it reports (never writes):
    * hostname / OS / elevation (session context)
    * volumes + disk-full flag
    * top CPU and top RAM processes
    * startup items
    * Defender real-time status + signature age
    * 7-day critical/error events (System + Application)
    * boot times / uptime
    * SMART counters where available

  Safe by construction: no mutation of any kind.
#>
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
