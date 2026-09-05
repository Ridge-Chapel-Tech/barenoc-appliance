#!/bin/bash
# windows_netdiag.sh <target_ip> [ssh_user] [ssh_key] [apply_dns_fix] [resolvers_csv]
#
# F8 expansion — Windows PC NETWORK/DNS health + hardening (SSH transport).
# Builds ON TOP of windows_diag (PC health) and windows_cleanup (safe
# cleanup); this action covers the network half of the dads-pc battery:
#
#   * NIC link rate (every physical adapter: name, status, link speed, media);
#   * latency probes (default gateway + public resolvers, 4 pings each,
#     min/avg/max + loss);
#   * the DNS-through-router weak spot — when the PC's configured DNS server
#     is the router/gateway IP (single point of failure, and the router's
#     forwarder is what actually answers queries);
#   * the 1.1.1.1 fix — override the router-as-resolver with a non-router
#     resolver (default 1.1.1.1 / 1.0.0.1), gated by the admin-vs-standard
#     session pattern (see below).
#
# ADMIN-vs-STANDARD SSH PATTERN:
#   The report ALWAYS captures the session context ($report.elevated). The
#   ONLY elevated (write) operation is Set-DnsClientServerAddress, and it is
#   gated on BOTH (a) apply_dns_fix=true AND (b) an elevated session. A
#   standard (non-admin) SSH session can only REPORT the weak spot + the
#   recommended fix — never change it. The result reports which gate held.
#
# SAFE BY CONSTRUCTION: read-only unless apply_dns_fix=true AND elevated. No
# partition ops, no uninstalls, no adapter/registry surgery — only the DNS
# server list on the active adapters. The override is trivially reversible
# (reset the NIC's DNS or re-run with no override).
#
# args:
#   apply_dns_fix  "1"/"true"/"yes" to apply the resolver override (default 0)
#   resolvers_csv  comma-separated resolver IPs (default 1.1.1.1,1.0.0.1)

TARGET="${1:-}"
SSH_USER="${2:-barenoc}"
SSH_KEY="${3:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"
APPLY_DNS_FIX="${4:-0}"
RESOLVERS_CSV="${5:-}"

if [ -z "$TARGET" ]; then
  echo '{"success": false, "error": "No target specified"}'
  exit 1
fi
if [ ! -f "$SSH_KEY" ]; then
  echo "{\"success\": false, \"error\": \"SSH key not found: $SSH_KEY\"}"
  exit 1
fi

# Normalize the apply flag to a PowerShell boolean literal.
case "$(printf '%s' "$APPLY_DNS_FIX" | tr 'A-Z' 'a-z')" in
  1|true|yes|on) APPLY_PS="\$true" ;;
  *) APPLY_PS="\$false" ;;
esac

# JSON-encode the resolver list (injection-safe: only base64 reaches the
# endpoint, decoded via ConvertFrom-Json — same pattern as windows_cleanup).
if [ -n "$RESOLVERS_CSV" ]; then
  RESOLVERS_JSON=$(printf '%s' "$RESOLVERS_CSV" | python3 -c 'import sys,json; print(json.dumps([r.strip() for r in sys.stdin.read().split(",") if r.strip()]))' 2>/dev/null)
fi
if [ -z "$RESOLVERS_JSON" ]; then
  RESOLVERS_JSON=''
fi
RESOLVERS_B64=$(printf '%s' "$RESOLVERS_JSON" | base64 -w0 2>/dev/null || printf '%s' "$RESOLVERS_JSON" | base64)

# ── Embedded PowerShell (read-only + gated DNS override) ─────────────────────
PS_SCRIPT=$(cat <<'POWERSHELL'
$ErrorActionPreference = 'SilentlyContinue'
$report = @{}

$report.hostname = $env:COMPUTERNAME
$os = Get-CimInstance Win32_OperatingSystem
$report.os = if ($os) { $os.Caption } else { 'unknown' }
$report.collected_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')

# ── Session context (admin-vs-standard pattern) ─────────────────────────────
$report.elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# ── 1. NIC link rate ────────────────────────────────────────────────────────
$adapters = @()
foreach ($a in (Get-NetAdapter -Physical -ErrorAction SilentlyContinue)) {
  $adapters += @{
    name = $a.Name
    interface = $a.InterfaceDescription
    status = $a.Status
    link_speed = $a.LinkSpeed
    media_type = $a.MediaType
    mac = $a.MacAddress
  }
}
$report.adapters = $adapters
$report.link_warning = $null
foreach ($a in $adapters) {
  if (($a.status -eq 'Up') -and $a.link_speed -and ($a.link_speed -notmatch 'Gbps')) {
    $report.link_warning = "Non-gigabit link on '$($a.name)' ($($a.link_speed))"
  }
}

# ── 2. Default gateway (best route to 0.0.0.0/0) ────────────────────────────
$gw = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
      Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
$gateway = if ($gw) { $gw.NextHop } else { $null }
$report.gateway = $gateway

# ── 3. DNS-through-router weak spot ─────────────────────────────────────────
$dnsServers = @()
foreach ($addr in (Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
  foreach ($s in @($addr.ServerAddresses)) {
    if ($s -and ($s -notin $dnsServers)) { $dnsServers += $s }
  }
}
$resolversJsonB64 = '__RESOLVERS_B64__'
# Recommended non-router resolvers — keep in sync with the action docs +
# action_validator windows_netdiag validate_params branch.
$resolvers = @('1.1.1.1', '1.0.0.1')
if ($resolversJsonB64 -and $resolversJsonB64 -ne '__RESOLVERS_B64__') {
  try {
    $json = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($resolversJsonB64))
    $parsed = @($json | ConvertFrom-Json)
    if ($parsed.Count -gt 0) { $resolvers = $parsed }
  } catch { }
}

$viaRouter = ($gateway -and ($dnsServers -contains $gateway))
$report.dns = @{
  servers = @($dnsServers)
  via_router = $viaRouter
  router_is_only_resolver = ($viaRouter -and @($dnsServers).Count -eq 1)
  recommended = @($resolvers)
}

# ── 4. Latency probes (gateway + public resolvers) ─────────────────────────
function Probe([string]$addr) {
  $times = @()
  for ($i = 0; $i -lt 4; $i++) {
    $p = Get-CimInstance Win32_PingStatus -Filter "Address='$addr'" -ErrorAction SilentlyContinue
    if ($p -and $p.StatusCode -eq 0) { $times += [int]$p.ResponseTime }
    else { $times += -1 }
  }
  $ok = @($times | Where-Object { $_ -ge 0 })
  $sent = @($times).Count
  $loss = if ($sent -gt 0) { [math]::Round(($sent - $ok.Count) / $sent * 100, 1) } else { 100 }
  return @{
    target = $addr
    reachable = ($ok.Count -gt 0)
    sent = $sent
    received = $ok.Count
    loss_pct = $loss
    avg_ms = if ($ok.Count) { [math]::Round(($ok | Measure-Object -Average).Average, 1) } else { $null }
    min_ms = if ($ok.Count) { [int]($ok | Measure-Object -Minimum).Minimum } else { $null }
    max_ms = if ($ok.Count) { [int]($ok | Measure-Object -Maximum).Maximum } else { $null }
  }
}
$probeTargets = @()
if ($gateway) { $probeTargets += $gateway }
foreach ($r in $resolvers) { if ($r -notin $probeTargets) { $probeTargets += $r } }
if ('8.8.8.8' -notin $probeTargets) { $probeTargets += '8.8.8.8' }
$report.latency = @{ targets = @() }
$report.latency.targets = @($probeTargets | ForEach-Object { Probe $_ })

# ── 5. The 1.1.1.1 fix (GATED: apply_dns_fix AND elevated) ─────────────────
$applyFix = __APPLY_DNS_FIX__
$fix = @{
  requested = $applyFix
  applied = $false
  reason = ''
  changed_interfaces = @()
  servers_now = @($dnsServers)
}
if (-not $applyFix) {
  $fix.reason = 'not requested (apply_dns_fix=false) — report-only'
} elseif (-not $report.elevated) {
  $fix.reason = 'standard (non-admin) SSH session — DNS override needs elevation; re-run elevated or apply manually'
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
    $fix.reason = 'no active adapter accepted the override (Set-DnsClientServerAddress failed)'
  }
}
$report.dns_fix = $fix

$report.success = $true
$report | ConvertTo-Json -Depth 6 -Compress
exit 0
POWERSHELL
)

# Inject the gated fix flag + resolver list at the placeholders.
PS_SCRIPT="${PS_SCRIPT/__APPLY_DNS_FIX__/$APPLY_PS}"
PS_SCRIPT="${PS_SCRIPT/__RESOLVERS_B64__/$RESOLVERS_B64}"

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
