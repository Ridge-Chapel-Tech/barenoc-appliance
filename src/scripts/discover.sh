#!/usr/bin/env bash
# Safe network discovery — parallel, capped ping sweep (NO-HANG).
#
# Usage: discover.sh <target>
#   target: "192.0.2.0/24" (CIDR) or "192.0.2.10" (single host)
#
# Output (stdout) — one JSON object:
#   {"network": "...", "found": [{"ip": "..."}, ...], "count": N,
#    "skipped_cgnat": N, "capped": bool}
#
# Progress notes go to STDERR as "PROGRESS: <human sentence>" so the agent
# runner can relay them to the ticket as live updates.
#
# Why this exists (the 08-19 fix): whole-subnet sweeps ("ping each IP on
# x.x.x.x/24") used to run sequentially and hang the worker against the 600s
# pi timeout. Now they run ~20 pings at a time with -W 1 -c 1 short timeouts,
# a host cap, progress notes, and NEVER sweep 100.64.0.0/10 (CGNAT + Tailscale
# overlay — a CGNAT Starlink link case). A /24 finishes in a few
# seconds.
#
# Env knobs (both hot-read by this script):
#   DISCOVERY_MAX_PARALLEL   concurrent pings (default 20)
#   DISCOVERY_MAX_HOSTS      host cap per sweep (default 254)

set -u

TARGET="${1:-}"

python3 - "$TARGET" <<'PY'
import ipaddress
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


MAX_PARALLEL = max(1, min(_env_int("DISCOVERY_MAX_PARALLEL", 20), 50))
MAX_HOSTS = max(1, min(_env_int("DISCOVERY_MAX_HOSTS", 254), 1024))


def is_cgnat(ip):
    """True for 100.64.0.0/10 (RFC 6598 CGNAT + Tailscale overlay)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.version != 4:
        return False
    b = addr.packed
    return b[0] == 100 and 64 <= b[1] <= 127


def fail(msg):
    print(json.dumps({"error": msg, "found": [], "count": 0}))
    sys.exit(1)


target = sys.argv[1] if len(sys.argv) > 1 else ""
if not target:
    fail("No target specified")

try:
    if "/" in target:
        net = ipaddress.ip_network(target, strict=False)
        hosts = [str(h) for h in net.hosts()]
        network = str(net)
    else:
        hosts = [target]
        network = target
except ValueError:
    fail(f"Invalid target '{target}' — use a CIDR like 192.0.2.0/24 or a single IP")

# CGNAT/Tailscale exclusion — never a scan target.
scannable = []
skipped_cgnat = 0
for h in hosts:
    if is_cgnat(h):
        skipped_cgnat += 1
    else:
        scannable.append(h)

capped = len(scannable) > MAX_HOSTS
scannable = scannable[:MAX_HOSTS]

if len(scannable) == 1:
    found = []
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "1", scannable[0]],
                           capture_output=True, timeout=3)
        if r.returncode == 0:
            found = [{"ip": scannable[0]}]
    except Exception:
        pass
    print(json.dumps({"network": network, "found": found,
                      "count": len(found), "skipped_cgnat": skipped_cgnat,
                      "capped": capped}))
    sys.exit(0)


def ping_one(ip):
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                           capture_output=True, timeout=3)
        return ip if r.returncode == 0 else None
    except Exception:
        return None


found = []
done = 0
total = len(scannable)
with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
    futures = {ex.submit(ping_one, ip): ip for ip in scannable}
    for fut in as_completed(futures):
        ip = fut.result()
        if ip:
            found.append({"ip": ip})
        done += 1
        # Progress notes: every 25 hosts (and the final one) so the runner has
        # something to relay — never a silent long sweep.
        if done % 25 == 0 or done == total:
            print(f"PROGRESS: Scanned {done} of {total} hosts ({len(found)} up)",
                  file=sys.stderr, flush=True)

found.sort(key=lambda d: d["ip"])
print(json.dumps({"network": network, "found": found, "count": len(found),
                  "skipped_cgnat": skipped_cgnat, "capped": capped}))
PY
