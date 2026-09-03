"""Blast-radius gate for UniFi port config flips (08-19 root-cause fix).

The 08-19 outage: an Optimize rollout told the agent to re-home a switch
port's native VLAN. The write was *merge-safe* — ``set_port_vlans`` read the
full ``port_overrides`` array and preserved every OTHER port's override — so it
reviewed clean. But the port carried the appliance's own segment (.4.x), and
flipping its native VLAN removed that segment from the port, stranding every
device on it (the appliance's management path included).

The array-merge check only proves you did not clobber the OTHER ports. It says
nothing about what is BEHIND the port you just flipped. This module is the
hard blast-radius gate that plugs that gap: before a port VLAN change (or
disable) is applied, it computes what rides on the port and refuses to remove
a protected network (the appliance's own subnet, or a management VLAN) from
the port that currently carries it.

Everything here is a pure function of the controller data (no DB, no client),
so the gate is unit-testable and diffable.
"""

import ipaddress
import os

MGMT_VLAN_KEYWORDS = ("mgmt", "management", "admin")
MGMT_VLAN_ID = 1

ENV_PATH = "/opt/barenoc/.env"


# ── appliance identity ─────────────────────────────────────────────────────

def _read_env() -> dict:
    """Read /opt/barenoc/.env, then fall back to process env for APPLIANCE_IP."""
    env = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception:
        pass
    if "APPLIANCE_IP" in os.environ:
        env.setdefault("APPLIANCE_IP", os.environ["APPLIANCE_IP"])
    return env


def appliance_ips(env: dict = None) -> list:
    """The appliance's own IPs (APPLIANCE_IP, comma-separated) as strings."""
    env = env if env is not None else _read_env()
    raw = str(env.get("APPLIANCE_IP") or "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()]


# ── helpers ────────────────────────────────────────────────────────────────

def _addr(s):
    try:
        return ipaddress.ip_address(str(s).strip())
    except ValueError:
        return None


def _addrs(ips):
    out = []
    for s in (ips or []):
        a = _addr(s)
        if a is not None:
            out.append(a)
    return out


def _parse_subnet(subnet):
    s = str(subnet or "").strip()
    if not s:
        return None
    try:
        return ipaddress.ip_network(s, strict=False)
    except ValueError:
        return None


def _iter_networks(networks):
    """Yield (network_id, record) from either a {id: rec} map
    (``get_networks_map``) or a list of dicts carrying ``id``/``_id`` (tests)."""
    if isinstance(networks, dict):
        for nid, rec in networks.items():
            yield str(nid), rec or {}
    else:
        for n in (networks or []):
            if isinstance(n, dict):
                yield str(n.get("id") or n.get("_id") or ""), n


def _sid(value):
    """Normalize a network id to a str ('' when empty/None)."""
    if value is None:
        return ""
    s = str(value).strip()
    return s


def _norm_ids(seq):
    out = []
    for x in (seq or []):
        s = _sid(x)
        if s:
            out.append(s)
    return out


def _port_eq(a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return _sid(a) == _sid(b)


def _is_mgmt_network(rec) -> bool:
    rec = rec or {}
    if rec.get("vlan") == MGMT_VLAN_ID:
        return True
    name = str(rec.get("name") or "").lower()
    return any(k in name for k in MGMT_VLAN_KEYWORDS)


def protected_network_ids(networks, appliance_ips=()) -> set:
    """Network ids whose subnet contains the appliance's own IP, plus the
    management networks (VLAN 1 / mgmt-named). These are the segments a port
    flip must never silently remove."""
    addrs = _addrs(appliance_ips)
    protected = set()
    for nid, rec in _iter_networks(networks):
        if not nid:
            continue
        subnet = _parse_subnet(rec.get("subnet"))
        if subnet is not None and any(a in subnet for a in addrs):
            protected.add(nid)
        elif _is_mgmt_network(rec):
            protected.add(nid)
    return protected


def downstream_devices_for_port(raw_devices, switch_mac, port_idx) -> list:
    """Names of managed devices whose uplink is (switch_mac, port_idx)."""
    out = []
    sm = _sid(switch_mac).lower()
    for d in (raw_devices or []):
        if not isinstance(d, dict):
            continue
        up = d.get("uplink") or {}
        if _sid(up.get("uplink_mac")).lower() == sm and _port_eq(up.get("uplink_remote_port"), port_idx):
            name = str(d.get("name") or d.get("mac") or "unknown")
            if name not in out:
                out.append(name)
    return out


def clients_for_port(clients, switch_mac, port_idx) -> list:
    """Client records attached to (switch_mac, port_idx)."""
    out = []
    sm = _sid(switch_mac).lower()
    for c in (clients or []):
        if not isinstance(c, dict):
            continue
        if _sid(c.get("sw_mac")).lower() == sm and _port_eq(c.get("sw_port"), port_idx):
            out.append(c)
    return out


def effective_port(raw_device, port) -> dict:
    """The EFFECTIVE native/tagged a port currently carries: the controller's
    ``port_overrides`` entry wins over the ``port_table`` (the same semantics
    as the NetOpt collector's ``_effective_port_profile``). The raw
    ``get_switch_ports`` view reads only the port_table, so an override-set
    native (e.g. the appliance's .4.x segment) would otherwise look unassigned
    and the gate could under-block the exact 08-19 'overrides trimmed' state."""
    port = dict(port or {})
    if not isinstance(raw_device, dict):
        return port
    overrides = {o.get("port_idx"): o for o in (raw_device.get("port_overrides") or [])
                 if isinstance(o, dict)}
    ov = overrides.get(port.get("port_idx"))
    if not ov:
        return port
    native = ov.get("native_networkconf_id") or ov.get("native_network_id")
    if native:
        port["native_network_id"] = _sid(native)
    tagged = ov.get("tagged_networkconf_id") or ov.get("tagged_network_id")
    if isinstance(tagged, str):
        port["tagged_network_ids"] = [x for x in tagged.split(",") if x]
    elif isinstance(tagged, (list, tuple)):
        port["tagged_network_ids"] = [_sid(x) for x in tagged if _sid(x)]
    return port


def _exact_appliance_clients(clients, appliance_ips) -> list:
    """IPs of clients on a port that ARE the appliance (exact APPLIANCE_IP)."""
    want = {str(a) for a in _addrs(appliance_ips)}
    out = []
    for c in (clients or []):
        if not isinstance(c, dict):
            continue
        a = _addr(c.get("ip"))
        if a is not None and str(a) in want:
            out.append(str(a))
    return sorted(set(out))


def _network_names(networks) -> dict:
    return {nid: (rec.get("name") or nid or "unknown")
            for nid, rec in _iter_networks(networks) if nid}


# ── the gate ───────────────────────────────────────────────────────────────

def check_port_change(networks, port, proposed_native_id=None,
                      proposed_tagged_ids=None, appliance_ips=(),
                      downstream_devices=(), connected_clients=()):
    """Evaluate a port VLAN flip BEFORE it is applied.

    Returns {allowed, blocked, reason, blast_radius}. ``blocked`` is True when
    the flip would remove a protected network (the appliance's own subnet or a
    management VLAN) from the port that currently carries it — the exact
    failure that stranded the .4.x segment — or when the appliance itself is
    attached to the port and the change alters its VLAN membership (the
    no-subnet-data fallback).
    """
    port = port or {}
    current_native = _sid(port.get("native_network_id"))
    current_tagged = _norm_ids(port.get("tagged_network_ids"))
    eff_native = _sid(proposed_native_id) if proposed_native_id is not None else current_native
    proposed_tagged = _norm_ids(proposed_tagged_ids)

    current = set(current_tagged)
    if current_native:
        current.add(current_native)
    proposed = set(proposed_tagged)
    if eff_native:
        proposed.add(eff_native)

    removed = current - proposed
    protected = protected_network_ids(networks, appliance_ips)
    removed_protected = [nid for nid in removed if nid in protected]

    names = _network_names(networks)
    downstream = [str(x) for x in (downstream_devices or []) if x]
    appliance_on_port = _exact_appliance_clients(connected_clients, appliance_ips)

    native_changed = eff_native != current_native
    tagged_changed = set(proposed_tagged) != set(current_tagged)
    membership_changed = native_changed or tagged_changed

    removed_protected_names = [names.get(nid, nid) for nid in removed_protected]

    blocked = False
    reason = ""
    if removed_protected:
        blocked = True
        reason = (
            "Blast-radius gate: this port currently carries protected network(s) "
            f"{', '.join(removed_protected_names)} — the appliance's segment / "
            "management path — and this change would remove them, stranding "
            f"reachability. Downstream devices: {', '.join(downstream) or 'none'}. "
            f"Appliance clients on this port: {', '.join(appliance_on_port) or 'none'}."
        )
    elif appliance_on_port and membership_changed:
        blocked = True
        reason = (
            "Blast-radius gate: the appliance itself is attached to this port "
            f"({', '.join(appliance_on_port)}) and this change alters its VLAN "
            "membership. Refusing to flip the port without an explicit admin confirmation."
        )

    blast = {
        "port_idx": port.get("port_idx"),
        "port_name": port.get("name"),
        "current": {"native": current_native or None, "tagged": current_tagged},
        "proposed": {"native": eff_native or None, "tagged": proposed_tagged},
        "removed": [names.get(nid, nid) for nid in sorted(removed)],
        "protected_networks": [names.get(nid, nid) for nid in sorted(protected)],
        "removed_protected": removed_protected_names,
        "downstream_devices": downstream,
        "appliance_clients": appliance_on_port,
    }
    return {"allowed": not blocked, "blocked": blocked, "reason": reason,
            "blast_radius": blast}


def check_port_disable(networks, port, appliance_ips=(),
                       downstream_devices=(), connected_clients=()):
    """Evaluate a port disable before it is applied.

    Blocks when the port carries the appliance itself, downstream managed
    devices (its uplink role), or a protected network that has evidence of
    devices behind it (connected clients or a tagged/trunk set). A truly
    dead-end/unused access port — no appliance, no downstream, no clients —
    is still allowed to be disabled even if its native happens to be the
    default network, so the dead-end/loop fix keeps working."""
    port = port or {}
    downstream = [str(x) for x in (downstream_devices or []) if x]
    appliance_on_port = _exact_appliance_clients(connected_clients, appliance_ips)
    appliance_addrs = {str(a) for a in _addrs(appliance_ips)}
    other_clients = []
    for c in (connected_clients or []):
        if not isinstance(c, dict):
            continue
        a = _addr(c.get("ip"))
        if a is None or str(a) in appliance_addrs:
            continue
        other_clients.append(c)

    tagged = _norm_ids(port.get("tagged_network_ids"))
    protected = protected_network_ids(networks, appliance_ips)
    current = set(tagged)
    native = _sid(port.get("native_network_id"))
    if native:
        current.add(native)
    carries = [nid for nid in current if nid in protected]

    names = _network_names(networks)
    carries_names = [names.get(nid, nid) for nid in carries]

    blockers = []
    if appliance_on_port:
        blockers.append(f"the appliance itself is attached ({', '.join(appliance_on_port)})")
    if downstream:
        blockers.append(f"it is the uplink for downstream device(s) {', '.join(downstream)}")
    elif carries and (other_clients or tagged):
        blockers.append(f"it carries protected network(s) {', '.join(carries_names)} "
                        f"with {len(other_clients)} connected client(s)")

    blocked = bool(blockers)
    reason = ("Blast-radius gate: refusing to disable this port — "
              + "; ".join(blockers)) if blocked else ""
    blast = {
        "port_idx": port.get("port_idx"),
        "port_name": port.get("name"),
        "downstream_devices": downstream,
        "appliance_clients": appliance_on_port,
        "connected_clients": len(other_clients),
        "protected_networks_carried": carries_names,
    }
    return {"allowed": not blocked, "blocked": blocked, "reason": reason,
            "blast_radius": blast}
