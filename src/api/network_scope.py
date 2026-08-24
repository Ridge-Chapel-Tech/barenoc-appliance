"""Network-scope guards — ranges that must NEVER be scanned, discovered,
claimed, or adopted.

The 2026-08-19 incident: a remote box discovered a Starlink WAN
address inside 100.64.0.0/10 because the discovery ping
found it reachable over a tailnet link, and it ended up in the device
inventory. 100.64.0.0/10 is RFC 6598 CGNAT space — the SAME range Tailscale
uses for its overlay addresses — so it is never a customer LAN address and
must be excluded everywhere a device identity is learned or acted on.

Kept intentionally small and explicit (a new range is a deliberate, reviewed
addition, not a regex tweak). Only 100.64.0.0/10 is hard-excluded today:
10.0.0.0/8, 172.16.0.0/12 and 192.168.0.0/16 are real customer LAN space and
must keep working.
"""

import ipaddress

# RFC 6598 shared address space — CGNAT *and* the Tailscale overlay. A device
# whose only address lives here is reachable through a tunnel, not on the
# customer's LAN, so it must never be scanned/claimed/adopted.
_TUNNEL_CGNAT_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
)

# Defensive extra ranges to add deliberately when a new tunnel/CGNAT class is
# observed (documented, pinned tests required). Left empty on purpose.


def is_tunnel_or_cgnat(ip) -> bool:
    """True when `ip` is in a tunnel/CGNAT range (or is not a valid IPv4/IPv6).

    Unparseable input is treated as excluded (True) so a malformed address can
    never slip through as a scannable/claimable device.
    """
    try:
        addr = ipaddress.ip_address(str(ip or "").strip().split("%")[0])
    except ValueError:
        return True
    return any(addr in net for net in _TUNNEL_CGNAT_NETWORKS)


def subnet_overlaps_tunnel(subnet) -> bool:
    """True when a subnet string/CIDR overlaps any tunnel/CGNAT range.

    Used to reject an explicit scan request for e.g. 100.64.0.0/24 up front
    rather than scanning it host-by-host. Unparseable input returns True.
    """
    try:
        net = ipaddress.ip_network(str(subnet or "").strip(), strict=False)
    except ValueError:
        return True
    return any(net.overlaps(t) for t in _TUNNEL_CGNAT_NETWORKS)


def filter_valid_hosts(ips) -> list:
    """Return the subset of `ips` that are scannable (not tunnel/CGNAT)."""
    return [ip for ip in (ips or []) if not is_tunnel_or_cgnat(ip)]


def excluded_reason(ip) -> "str | None":
    """Human-readable exclusion reason for a device/scan target, else None."""
    if is_tunnel_or_cgnat(ip):
        return "cgnat/tunnel (100.64.0.0/10)"
    return None
