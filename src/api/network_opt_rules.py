"""Network Optimization — deterministic rule-based findings engine (P1).

The heart of the feature: ~40 STABLE, testable, explainable checks across four
categories (PERFORMANCE first, SECURITY always, then RELIABILITY + HYGIENE).
No LLM anywhere in the findings path — a finding is a pure function of the
scan snapshot, so it can be unit-tested and diffed across runs (stable
``finding_key`` + structured ``evidence`` JSON).

The snapshot is produced by ``network_opt.collectors`` (UniFi + SNMP +
nmap/ping). Its canonical shape:

    {
      "schema_version": 1,
      "scope": {"devices": [id...], "excluded": [ip/name...], "max_hosts": N},
      "devices": [
        {
          "device_id", "name", "ip", "mac", "device_type", "vendor", "model",
          "unifi_managed": bool,
          "ping": {"reachable": bool, "latency_ms": float|None} | None,
          "nmap": {"open_ports": [int...], "open_services": {port: svc}, "os": str} | None,
          "snmp": {
             "version": "2c"|"3", "community": str|None,
             "sysdescr", "sysname", "uptime_seconds", "cpu_load", "mem_used_pct",
             "interfaces": [{"ifdescr","iftype","oper_status","admin_status",
                             "speed_mbps","duplex","mtu","in_errors","out_errors",
                             "in_discards","out_discards","in_pkts","out_pkts"}]
          } | None,
          "unifi": {
             "version", "upgradable": bool, "uptime_seconds", "fixed_ip", "uplink_mac",
             "wan": {"status": str, "wan_count": int} | None,
             "ports": [{"port_idx","name","up","speed_mbps","max_speed_mbps",
                        "native_vlan","tagged_vlans","link_down_count",
                        "tx_errors","rx_errors","is_uplink"}]
          } | None,
        }
      ],
      "networks": [{"name","vlan","subnet","enabled","dhcp","dhcp_start","dhcp_stop"}],
      "wlans": [{"name","enabled","security","wpa_mode","wpa_enc","vlan"}],
      "meta": {"collector_errors": [...], "hosts_scanned": N, "profile": str},
    }

All rules are defensive (missing keys -> skip the rule), so a partial
collection (e.g. UniFi down, SNMP blocked) never crashes the scan — it just
yields fewer findings and the collector error is recorded in ``meta``.
"""

SCHEMA_VERSION = 1

CATEGORIES = ("performance", "security", "reliability", "hygiene")
SEVERITIES = ("critical", "warning", "info")

# Score penalty per finding severity (overall + per-category, floor 0).
# PINNED SEMANTICS (gate decision 08-18): criticals −20 stack, warnings −5
# stack with NO cap, infos −2 but CAPPED (first INFO_COUNT_CAP count, then
# free) so noise can never tank a healthy network's score.
SEVERITY_WEIGHT = {"critical": 20, "warning": 5, "info": 2}
INFO_COUNT_CAP = 5           # first 5 info findings cost −2 each; then stop
INFO_PENALTY_CAP = 10        # absolute info penalty ceiling (−10 => ≥90 even with 100 infos)

GEAR_TYPES = {"gateway", "router", "switch", "ap"}

# ── thresholds (module constants so tests can pin exact behavior) ──────────
IF_ERROR_RATIO = 0.001        # 0.1% of packets errored -> warning
IF_ERROR_ABS = 5000           # fallback absolute error count when no pkt counts
IF_DISCARD_ABS = 1000         # cumulative discards -> warning
CPU_LOAD_WARN = 90.0          # UCD-SNMP 1-min load avg (%) -> warning
MEM_USED_WARN = 90.0          # UCD-SNMP memory used (%) -> warning
DUPLEX_FULL_SPEED = 100       # half-duplex at >=100Mbps -> warning
MTU_EXPECTED = 1500
LINK_DOWN_COUNT_WARN = 3      # >2 link-down transitions on a port -> warning.
                               # A single historical flap (e.g. the 08-18
                               # PoE-cycle artifact) must not warn forever —
                               # UniFi's port_table exposes no per-flap
                               # timestamp, so "recent/repeated" is enforced
                               # as "repeated" (>2).
RECENT_REBOOT_SECONDS = 15 * 60      # uptime below this -> "recently rebooted"
EXTENDED_UPTIME_SECONDS = 365 * 24 * 3600  # uptime above this -> "never rebooted"
STALE_DEVICE_DAYS = 30
POE_CONSUMPTION_PCT = 80.0    # (not used in P1 — kept for a future PoE rule)
DEFAULT_SNMP_COMMUNITIES = {"public", "private", "snmp", "admin", "publicro"}
SNMP_LOW_SPEED_MBPS = 100
MGMT_VLAN_KEYWORDS = ("mgmt", "management", "admin")
MGMT_VLAN_ID = 1

# Legacy / weak WPA security markers (UniFi rest/wlanconf `security`).
WEAK_WPA_SECURITY = {"open", "wep", "wpaeap"}       # wpaeap=WPA Enterprise (context)
LEGACY_WPA_SECURITY = {"wep", "wpa1"}
OPEN_SECURITY = {"open"}


# ── rule registry ───────────────────────────────────────────────────────────
RULES = []           # device-level rules (evaluated per device)
SNAPSHOT_RULES = []  # whole-snapshot rules (duplicates, unused VLANs, …)


def _fmt(s: str, ctx: dict) -> str:
    """Format a template with safe missing-key fallback (empty string)."""
    return s.format_map(_SafeDict(ctx))


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def rule(key, category, severity, title):
    """Decorator: register a device-level rule."""
    def deco(fn):
        RULES.append({"key": key, "category": category, "severity": severity,
                      "title": title, "fn": fn})
        return fn
    return deco


def snapshot_rule(key, category, severity, title):
    """Decorator: register a whole-snapshot rule. The function takes the
    snapshot and returns a LIST of evidence dicts (possibly empty — one entry
    per match, e.g. one per offending SSID/VLAN)."""
    def deco(fn):
        SNAPSHOT_RULES.append({"key": key, "category": category,
                               "severity": severity, "title": title, "fn": fn})
        return fn
    return deco


# ── fixability + suggested action (fix-ticket rollout) ─────────────────────
# Each rule key maps to whether the finding is actionable (``fixable``) and,
# if so, what fixing it means (``suggested_action``). Non-fixable findings
# (informational observations with no in-controller action) disable the
# Optimize checkbox — there is no meaningful ticket for them. ``fixability()``
# is the stable public accessor used by the run-detail API + ticket helper.

NON_FIXABLE_RULES = frozenset({
    "rel.single_wan",     # a second ISP is outside the appliance's control
    "rel.single_uplink",  # needs added hardware / redundant path
    "hyg.disabled_ssid",  # an observation — remove it or leave it; nothing to fix
    "hyg.unused_vlan",    # cleanup candidate — informational
})
NON_FIXABLE_LABEL = "informational — not actionable"
DEFAULT_SUGGESTED_ACTION = "Resolve the underlying condition described in the finding."

SUGGESTED_ACTIONS = {
    "perf.duplex_half": "Reconfigure the interface and its peer for full-duplex "
                        "(fix autonegotiation, or hard-set both ends to full duplex).",
    "perf.link_speed_100": "Check/replace the cable and patch cord, and verify the "
                           "peer's negotiation to restore the gigabit link.",
    "perf.link_speed_10": "Replace the cable and verify the port/hardware — a 10 Mbps "
                          "negotiation indicates a hardware fault.",
    "perf.interface_errors": "Replace the failing cable/transceiver and investigate the "
                             "CRC/alignment errors on the interface.",
    "perf.interface_discards": "Relieve congestion on the interface (rebalance traffic, "
                               "check the egress queue).",
    "perf.mtu_mismatch": "Correct the interface MTU to 1500 (or match both ends if "
                         "jumbo frames are intended).",
    "perf.high_cpu": "Investigate the sustained CPU load (traffic storm, misconfig, or "
                     "over-subscription).",
    "perf.high_memory": "Investigate memory pressure (leak or under-provisioning) and "
                        "reboot/upgrade if needed.",
    "perf.port_errors_unifi": "Replace the cable/transceiver on the port reporting "
                              "TX/RX errors.",
    "perf.uplink_congestion": "Re-cable and renegotiate the uplink port to full capacity.",
    "sec.telnet_exposed": "Disable Telnet and use SSH for management.",
    "sec.ssh_exposed": "Disable or rotate the admin SSH, and restrict it to a management "
                       "VLAN/ACL with key-only auth.",
    "sec.http_mgmt_plaintext": "Disable HTTP management (port 80) and use HTTPS only.",
    "sec.default_snmp_community": "Change the default SNMP community and prefer SNMPv3 "
                                  "with auth+priv.",
    "sec.snmp_v2c": "Move the device to SNMPv3 (auth+priv) where supported.",
    "sec.firmware_outdated": "Run the firmware upgrade.",
    "sec.mgmt_vlan_on_uplink": "Move the management VLAN off the uplink port onto a "
                              "dedicated, restricted VLAN.",
    "rel.dhcp_no_reservation": "Add a DHCP reservation/static lease for the device so "
                               "its address can't drift.",
    "rel.link_down_count": "Replace the flapping cable and re-seat both ends of the link.",
    "rel.uptime_recent_reboot": "Verify the reboot cause (crash, power, or manual) and "
                                "confirm the device is stable.",
    "rel.uptime_extended": "Schedule a maintenance reboot to apply pending updates and "
                           "clear memory leaks.",
    "rel.offline_gear": "Bring the device back online (power/network) or remove it from "
                        "inventory if retired.",
    "rel.oper_down_admin_up": "Reconnect/restore the far end of the interface, or shut "
                              "the interface if unused.",
    "rel.ap_uplink_missing": "Verify the AP is meshed intentionally, or wire it for a "
                             "dedicated uplink.",
    "rel.wan_degraded": "Investigate the WAN health degradation and contact the ISP if "
                        "needed.",
    "hyg.stale_device": "Reconnect or remove the stale device from inventory.",
    "hyg.unnamed_uplink_port": "Name the port in the controller so future changes are safe.",
    "hyg.port_no_profile": "Assign a network profile to the port so traffic doesn't land "
                           "on the default network.",
    "sec.open_ssid": "Enable WPA2/WPA3 encryption (or disable the SSID).",
    "sec.legacy_wpa": "Upgrade the SSID to WPA2/WPA3.",
    "sec.wpa2_tkip": "Switch the SSID to AES/CCMP encryption.",
    "hyg.default_vlan1": "Move traffic off VLAN 1 onto dedicated VLANs.",
    "hyg.duplicate_ip": "Resolve the IP conflict (re-address one of the conflicting "
                         "devices).",
    "hyg.duplicate_mac": "Resolve the duplicate MAC (check for a mis-cloned identity).",
    "hyg.disabled_network": "Remove the disabled network if unused.",
    "hyg.vlan_without_name": "Give the VLAN a descriptive name.",
}


# ── risk metadata (agent-foresight — foresight BEFORE recommending) ──────
# The 08-19 incident: a batched Optimize turned port/VLAN findings into a
# ticket, autonomous Lily applied the port/VLAN writes, pi timed out
# mid-execution, and half-applied port_overrides stranded the .4.x segment.
# The fix is a smarter agent — the recommendation itself now carries the BLAST
# RADIUS + a plan-first note, and every port/VLAN/uplink-changing rule is
# flagged high_risk so the optimize ticket arrives PRE-THOUGHT.

HIGH_RISK_KEYS = frozenset({
    "hyg.port_no_profile",       # assigning a native network moves connected devices
    "sec.mgmt_vlan_on_uplink",   # re-homing the management VLAN on an uplink
    "perf.uplink_congestion",    # re-cable/renegotiate the uplink path
    "hyg.unnamed_uplink_port",   # the uplink port — rename only, never re-assign
    "hyg.default_vlan1",         # moving traffic off VLAN 1 (port re-assignment)
    "hyg.disabled_network",      # deleting a network (VLAN) definition
})

RISK_META = {
    "hyg.port_no_profile": {
        "high_risk": True,
        "blast_radius": ("Assigning a native network to an un-profiled port moves every "
                         "device on that port (PCs, downstream switches/APs, cameras) off "
                         "the default network — any of them, and any management traffic "
                         "riding that port, can lose connectivity until the change is verified."),
        "plan_note": ("PLAN FIRST: enumerate what is connected to this port and confirm it "
                      "is not an uplink and does not carry the appliance or management. "
                      "Capture the full before state, assign the native network, then verify "
                      "the port still forwards before the next change."),
    },
    "sec.mgmt_vlan_on_uplink": {
        "high_risk": True,
        "blast_radius": ("Re-homing the management VLAN off an uplink touches the port that "
                         "carries the management plane — get it wrong and you lose management "
                         "access to the gear (including the appliance's own path)."),
        "plan_note": ("PLAN FIRST: confirm which VLANs are management, capture the uplink's "
                      "full before state, move the management VLAN in one step, then verify "
                      "management reachability before anything else."),
    },
    "perf.uplink_congestion": {
        "high_risk": True,
        "blast_radius": ("Re-cabling/renegotiating an uplink disrupts every device behind it "
                         "(downstream switches, APs, and all their clients)."),
        "plan_note": ("PLAN FIRST: identify everything downstream of the uplink, capture the "
                      "before state, change one side, verify the link comes back at full "
                      "capacity, and have the rollback ready."),
    },
    "hyg.unnamed_uplink_port": {
        "high_risk": True,
        "blast_radius": ("This is an UPLINK — the trunk carrying downstream devices and "
                         "possibly the management plane. Do NOT change its VLAN assignment; "
                         "only its label is being fixed."),
        "plan_note": ("PLAN FIRST / DO NOT CHANGE THE UPLINK: rename the port only. Never "
                      "re-assign the native/tagged VLANs on an uplink port as part of a "
                      "hygiene fix."),
    },
    "hyg.default_vlan1": {
        "high_risk": True,
        "blast_radius": ("Moving traffic off VLAN 1 re-assigns port memberships network-wide — "
                         "every affected port's devices can drop until the change is verified."),
        "plan_note": ("PLAN FIRST: capture the full port_overrides before starting, move one "
                      "network at a time, verify each, and roll back to the captured state on "
                      "any failure."),
    },
    "hyg.disabled_network": {
        "high_risk": True,
        "blast_radius": ("Deleting a network (VLAN) definition removes it from every port/SSID "
                         "that references it — if the 'disabled' network is actually referenced, "
                         "those ports lose their profile."),
        "plan_note": ("PLAN FIRST: confirm no port or SSID references the network before "
                      "removing it, and capture the before state so the network can be re-created."),
    },
    # ssh/http/telnet fixes are SAFE — they change no port/VLAN/uplink assignment.
    "sec.ssh_exposed": {
        "high_risk": False,
        "blast_radius": "Safe: this changes only the management channel (SSH), not any port/VLAN/uplink.",
        "plan_note": "Safe fix — no connectivity blast radius.",
    },
    "sec.http_mgmt_plaintext": {
        "high_risk": False,
        "blast_radius": "Safe: this changes only the management channel (HTTP), not any port/VLAN/uplink.",
        "plan_note": "Safe fix — no connectivity blast radius.",
    },
    "sec.telnet_exposed": {
        "high_risk": False,
        "blast_radius": "Safe: this changes only the management channel (Telnet), not any port/VLAN/uplink.",
        "plan_note": "Safe fix — no connectivity blast radius.",
    },
}

DEFAULT_BLAST_RADIUS = ("This fix does not change any port/VLAN/uplink assignment; "
                        "it touches the device's own configuration only.")
DEFAULT_PLAN_NOTE = ("Plan the change, capture the current state, apply it, and verify "
                     "the device is still reachable.")


def risk_meta(key: str) -> dict:
    """{high_risk, blast_radius, plan_note} for a finding key — the stable
    foresight metadata the ticket helper embeds into the change plan."""
    key = (key or "").strip()
    m = RISK_META.get(key)
    if m is not None:
        out = dict(m)
        out.setdefault("high_risk", key in HIGH_RISK_KEYS)
        return out
    return {"high_risk": key in HIGH_RISK_KEYS,
            "blast_radius": DEFAULT_BLAST_RADIUS,
            "plan_note": DEFAULT_PLAN_NOTE}


def fixability(key: str) -> dict:
    """{fixable, suggested_action, high_risk, blast_radius, plan_note} for a
    finding key — the stable public shape the run-detail API and the ticket
    helper consume. Port/VLAN/uplink-changing rules are high_risk and their
    suggested_action carries the blast radius + a plan-first note."""
    key = (key or "").strip()
    if key in NON_FIXABLE_RULES:
        return {"key": key, "fixable": False, "suggested_action": NON_FIXABLE_LABEL,
                "high_risk": False, "blast_radius": "", "plan_note": ""}
    risk = risk_meta(key)
    suggested = SUGGESTED_ACTIONS.get(key, DEFAULT_SUGGESTED_ACTION)
    if risk["high_risk"]:
        suggested = f"{suggested} {risk['blast_radius']} {risk['plan_note']}"
    return {"key": key, "fixable": True, "suggested_action": suggested,
            "high_risk": risk["high_risk"], "blast_radius": risk["blast_radius"],
            "plan_note": risk["plan_note"]}


def _annotate_fixability():
    """Attach fixable + suggested_action + risk metadata to every registered
    rule so the registry itself carries the annotation (each rule *gains* the
    fields)."""
    for coll in (RULES, SNAPSHOT_RULES):
        for r in coll:
            fx = fixability(r["key"])
            r["fixable"] = fx["fixable"]
            r["suggested_action"] = fx["suggested_action"]
            r["high_risk"] = fx["high_risk"]
            r["blast_radius"] = fx["blast_radius"]
            r["plan_note"] = fx["plan_note"]


# ══════════════════════════════ PERFORMANCE ═══════════════════════════════

@rule("perf.duplex_half", "performance", "warning",
      "Half-duplex link on {name}")
def _perf_duplex_half(snap, dev):
    for iface in (dev.get("snmp") or {}).get("interfaces") or []:
        speed = iface.get("speed_mbps")
        if (iface.get("duplex") == "half" and speed is not None
                and speed >= DUPLEX_FULL_SPEED and iface.get("oper_status") == "up"):
            return {"interface": iface.get("ifdescr"),
                    "speed_mbps": speed, "duplex": "half",
                    "detail": _fmt("Interface {interface} is running half-duplex at "
                                   "{speed_mbps} Mbps — a classic duplex mismatch that "
                                   "causes collisions and packet loss.",
                                   {"interface": iface.get("ifdescr"), "speed_mbps": speed})}


@rule("perf.link_speed_100", "performance", "warning",
      "Link negotiated down to 100 Mbps on {name}")
def _perf_link_speed_100(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if p.get("up") and p.get("speed_mbps") == 100 and (p.get("max_speed_mbps") or 0) >= 1000:
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "speed_mbps": 100, "max_speed_mbps": p.get("max_speed_mbps"),
                    "detail": _fmt("Port {name} (idx {port}) is up at 100 Mbps on a "
                                   "gigabit-capable port — check the cable/patch and the "
                                   "peer's negotiation.",
                                   {"name": p.get("name"), "port": p.get("port_idx")})}


@rule("perf.link_speed_10", "performance", "warning",
      "Link negotiated down to 10 Mbps on {name}")
def _perf_link_speed_10(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if p.get("up") and p.get("speed_mbps") is not None and p.get("speed_mbps") <= 10:
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "speed_mbps": p.get("speed_mbps"),
                    "detail": _fmt("Port {name} (idx {port}) is up at {speed} Mbps — a "
                                   "hardware/negotiation fault that will bottleneck "
                                   "traffic.", {"name": p.get("name"), "port": p.get("port_idx"),
                                                "speed": p.get("speed_mbps")})}


@rule("perf.interface_errors", "performance", "warning",
      "Interface errors on {name}")
def _perf_interface_errors(snap, dev):
    for iface in (dev.get("snmp") or {}).get("interfaces") or []:
        in_e = iface.get("in_errors") or 0
        out_e = iface.get("out_errors") or 0
        total = in_e + out_e
        if total <= 0:
            continue
        pkts = (iface.get("in_pkts") or 0) + (iface.get("out_pkts") or 0)
        if pkts > 0:
            if total / pkts < IF_ERROR_RATIO:
                continue
        elif total < IF_ERROR_ABS:
            continue
        return {"interface": iface.get("ifdescr"), "in_errors": in_e,
                "out_errors": out_e, "packets": pkts,
                "detail": _fmt("Interface {interface} has {in_e}/{out_e} in/out errors "
                               "({packets} packets) — CRC/alignment errors or a failing "
                               "transceiver/cable.",
                               {"interface": iface.get("ifdescr"), "in_e": in_e,
                                "out_e": out_e, "packets": pkts})}


@rule("perf.interface_discards", "performance", "warning",
      "Interface discards on {name}")
def _perf_interface_discards(snap, dev):
    for iface in (dev.get("snmp") or {}).get("interfaces") or []:
        total = (iface.get("in_discards") or 0) + (iface.get("out_discards") or 0)
        if total >= IF_DISCARD_ABS:
            return {"interface": iface.get("ifdescr"),
                    "in_discards": iface.get("in_discards"),
                    "out_discards": iface.get("out_discards"),
                    "detail": _fmt("Interface {interface} has dropped {total} packets "
                                   "(discards) — output-queue overflow / congestion on "
                                   "the egress side.",
                                   {"interface": iface.get("ifdescr"), "total": total})}


@rule("perf.mtu_mismatch", "performance", "info",
      "Non-standard MTU on {name}")
def _perf_mtu_mismatch(snap, dev):
    for iface in (dev.get("snmp") or {}).get("interfaces") or []:
        mtu = iface.get("mtu")
        # ifType 6 = ethernetCsmacd, 117 = gigabitEthernet, 161 = ieee8023adLag
        if mtu and mtu != MTU_EXPECTED and iface.get("iftype") in (6, 117, 161):
            return {"interface": iface.get("ifdescr"), "mtu": mtu,
                    "detail": _fmt("Interface {interface} has MTU {mtu} (standard is "
                                   "{expected}) — jumbo-frame mismatches cause silent "
                                   "fragmentation/drops.", {"interface": iface.get("ifdescr"),
                                                            "mtu": mtu, "expected": MTU_EXPECTED})}


@rule("perf.high_cpu", "performance", "warning",
      "High CPU load on {name}")
def _perf_high_cpu(snap, dev):
    cpu = (dev.get("snmp") or {}).get("cpu_load")
    if cpu is not None and cpu >= CPU_LOAD_WARN:
        return {"cpu_load_pct": cpu,
                "detail": _fmt("Device reports a 1-minute CPU load of {cpu}% — sustained "
                               "high CPU can drop management-plane traffic.", {"cpu": cpu})}


@rule("perf.high_memory", "performance", "warning",
      "High memory utilization on {name}")
def _perf_high_memory(snap, dev):
    mem = (dev.get("snmp") or {}).get("mem_used_pct")
    if mem is not None and mem >= MEM_USED_WARN:
        return {"mem_used_pct": mem,
                "detail": _fmt("Memory utilization is {mem}% — near exhaustion can "
                               "degrade forwarding/control plane.", {"mem": mem})}


@rule("perf.port_errors_unifi", "performance", "warning",
      "Port errors on {name}")
def _perf_port_errors_unifi(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        total = (p.get("tx_errors") or 0) + (p.get("rx_errors") or 0)
        if p.get("up") and total > 0:
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "tx_errors": p.get("tx_errors"), "rx_errors": p.get("rx_errors"),
                    "detail": _fmt("Port {name} (idx {port}) reports {total} TX/RX "
                                   "errors — physical-layer problem (cable/transceiver).",
                                   {"name": p.get("name"), "port": p.get("port_idx"),
                                    "total": total})}


@rule("perf.uplink_congestion", "performance", "info",
      "Uplink negotiated below capacity on {name}")
def _perf_uplink_congestion(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if (p.get("is_uplink") and p.get("up") and p.get("speed_mbps") is not None
                and p.get("speed_mbps") <= SNMP_LOW_SPEED_MBPS):
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "speed_mbps": p.get("speed_mbps"),
                    "detail": _fmt("Uplink port {name} (idx {port}) is carrying a "
                                   "downlink at only {speed} Mbps — a congestion "
                                   "bottleneck for everything behind it.",
                                   {"name": p.get("name"), "port": p.get("port_idx"),
                                    "speed": p.get("speed_mbps")})}


# ══════════════════════════════ SECURITY ══════════════════════════════════

@rule("sec.telnet_exposed", "security", "critical",
      "Telnet exposed on {name}")
def _sec_telnet_exposed(snap, dev):
    ports = (dev.get("nmap") or {}).get("open_ports") or []
    if 23 in ports:
        return {"port": 23,
                "detail": _fmt("Telnet (port 23) is open on {name} — plaintext "
                               "management of network gear, including passwords, on "
                               "the wire. Disable it in favor of SSH.", {"name": dev.get("name")})}


@rule("sec.ssh_exposed", "security", "warning",
      "SSH management exposed on {name}")
def _sec_ssh_exposed(snap, dev):
    ports = (dev.get("nmap") or {}).get("open_ports") or []
    if 22 not in ports:
        return None
    ev = {"port": 22,
          "detail": _fmt("SSH (port 22) is open on {name}. Fine for management, "
                         "but restrict it to a management VLAN/ACL and use "
                         "key-only auth.", {"name": dev.get("name")})}
    # Context-aware severity: Ubiquiti gear ships with SSH as its stock
    # management channel — an open SSH port there is the vendor default, not a
    # misconfiguration, so it's informational (never a warning).
    if _is_ubiquiti(dev):
        ev["severity"] = "info"
    return ev


@rule("sec.http_mgmt_plaintext", "security", "warning",
      "Plaintext HTTP management on {name}")
def _sec_http_mgmt_plaintext(snap, dev):
    ports = (dev.get("nmap") or {}).get("open_ports") or []
    if 80 in ports:
        return {"port": 80,
                "detail": _fmt("{name} serves its management UI over plaintext HTTP "
                               "(port 80) — credentials are sent in the clear.",
                               {"name": dev.get("name")})}


@rule("sec.default_snmp_community", "security", "critical",
      "Default SNMP community on {name}")
def _sec_default_snmp_community(snap, dev):
    snmp = dev.get("snmp") or {}
    community = (snmp.get("community") or "").strip().lower()
    if community and community in DEFAULT_SNMP_COMMUNITIES:
        return {"community": community,
                "detail": _fmt("SNMP answers with the default community '{community}' "
                               "on {name} — anyone on the LAN can read (and, if RW, "
                               "write) device state. Change it and prefer SNMPv3.",
                               {"community": community, "name": dev.get("name")})}


@rule("sec.snmp_v2c", "security", "warning",
      "SNMPv2c (plaintext) in use on {name}")
def _sec_snmp_v2c(snap, dev):
    snmp = dev.get("snmp") or {}
    if snmp.get("version") == "2c":
        return {"version": "2c",
                "detail": _fmt("{name} is polled over SNMPv2c — community strings "
                               "travel in plaintext. Move to SNMPv3 auth+priv where "
                               "the device supports it.", {"name": dev.get("name")})}


@rule("sec.firmware_outdated", "security", "warning",
      "Firmware update available for {name}")
def _sec_firmware_outdated(snap, dev):
    uni = dev.get("unifi") or {}
    if uni.get("upgradable"):
        return {"version": uni.get("version"), "model": uni.get("model"),
                "detail": _fmt("{name} (v{version}) has a firmware update available — "
                               "stale firmware leaves known CVEs unpatched.",
                               {"name": dev.get("name"), "version": uni.get("version")})}


@rule("sec.mgmt_vlan_on_uplink", "security", "warning",
      "Management VLAN on an uplink port of {name}")
def _sec_mgmt_vlan_on_uplink(snap, dev):
    mgmt = _mgmt_vlans(snap)
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if not p.get("is_uplink"):
            continue
        vlans = set(p.get("tagged_vlans") or [])
        native = p.get("native_vlan")
        if native in mgmt or (vlans & mgmt):
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "native_vlan": native, "mgmt_vlans": sorted(vlans & mgmt) or [native],
                    "detail": _fmt("Uplink port {name} (idx {port}) carries the "
                                   "management VLAN — keep the management plane on a "
                                   "dedicated, restricted VLAN.", {"name": p.get("name"),
                                                                   "port": p.get("port_idx")})}


# ── SSID security (snapshot-level — one finding per offending SSID) ───────

@snapshot_rule("sec.open_ssid", "security", "critical",
               "Open (unencrypted) SSID: {ssid}")
def _sec_open_ssid(snap):
    out = []
    for w in snap.get("wlans") or []:
        if not w.get("enabled"):
            continue
        if (w.get("security") or "open").lower() in OPEN_SECURITY:
            out.append({"ssid": w.get("name"), "security": w.get("security"),
                        "detail": _fmt("SSID '{ssid}' broadcasts with NO encryption — "
                                       "anyone can join and sniff traffic.",
                                       {"ssid": w.get("name")})})
    return out


@snapshot_rule("sec.legacy_wpa", "security", "critical",
               "Legacy WPA/WEP on SSID: {ssid}")
def _sec_legacy_wpa(snap):
    out = []
    for w in snap.get("wlans") or []:
        if not w.get("enabled"):
            continue
        sec = (w.get("security") or "").lower()
        mode = (w.get("wpa_mode") or "").lower()
        if sec in LEGACY_WPA_SECURITY or mode == "wpa1":
            out.append({"ssid": w.get("name"), "security": w.get("security"),
                        "wpa_mode": w.get("wpa_mode"),
                        "detail": _fmt("SSID '{ssid}' uses legacy WPA/WEP — trivially "
                                       "crackable. Upgrade to WPA2/WPA3.",
                                       {"ssid": w.get("name")})})
    return out


@snapshot_rule("sec.wpa2_tkip", "security", "warning",
               "WPA2 with TKIP on SSID: {ssid}")
def _sec_wpa2_tkip(snap):
    out = []
    for w in snap.get("wlans") or []:
        if not w.get("enabled"):
            continue
        enc = (w.get("wpa_enc") or "").lower()
        if "tkip" in enc:
            out.append({"ssid": w.get("name"), "wpa_enc": w.get("wpa_enc"),
                        "detail": _fmt("SSID '{ssid}' uses TKIP encryption (a known "
                                       "weakness) — switch to AES/CCMP.",
                                       {"ssid": w.get("name")})})
    return out


# ═════════════════════════════ RELIABILITY ═════════════════════════════════

@rule("rel.dhcp_no_reservation", "reliability", "info",
      "No DHCP reservation for {name}")
def _rel_dhcp_no_reservation(snap, dev):
    uni = dev.get("unifi") or {}
    if dev.get("device_type") in GEAR_TYPES and uni.get("fixed_ip") is False:
        return {"detail": _fmt("{name} (fixed network gear) has no DHCP "
                               "reservation/static lease — its address can drift on "
                               "renewal.", {"name": dev.get("name")})}


@rule("rel.single_wan", "reliability", "info",
      "Single WAN (no failover) on {name}")
def _rel_single_wan(snap, dev):
    uni = dev.get("unifi") or {}
    wan = uni.get("wan") or {}
    if dev.get("device_type") in ("gateway", "router") and wan.get("wan_count") == 1:
        return {"wan_count": 1,
                "detail": _fmt("{name} has a single WAN link — a single ISP failure "
                               "takes the site offline. Consider a secondary/failover "
                               "WAN.", {"name": dev.get("name")})}


@rule("rel.link_down_count", "reliability", "warning",
      "Link has flapped on {name}")
def _rel_link_down_count(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if (p.get("link_down_count") or 0) >= LINK_DOWN_COUNT_WARN:
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "link_down_count": p.get("link_down_count"),
                    "detail": _fmt("Port {name} (idx {port}) has recorded {n} link-down "
                                   "transition(s) — repeated flapping / intermittent cable.",
                                   {"name": p.get("name"), "port": p.get("port_idx"),
                                    "n": p.get("link_down_count")})}


@rule("rel.uptime_recent_reboot", "reliability", "info",
      "Recently rebooted: {name}")
def _rel_uptime_recent_reboot(snap, dev):
    up = _uptime_seconds(dev)
    if up is not None and 0 <= up < RECENT_REBOOT_SECONDS:
        return {"uptime_seconds": up,
                "detail": _fmt("{name} has been up only {up}s — recently rebooted "
                               "(crash, power cycle, or manual restart).",
                               {"name": dev.get("name"), "up": int(up)})}


@rule("rel.uptime_extended", "reliability", "info",
      "Extended uptime on {name}")
def _rel_uptime_extended(snap, dev):
    up = _uptime_seconds(dev)
    if up is not None and up >= EXTENDED_UPTIME_SECONDS:
        return {"uptime_seconds": up, "days": int(up // 86400),
                "detail": _fmt("{name} has been up {days} days — long uptime can hide "
                               "memory leaks and means pending updates/reboots are "
                               "unapplied.", {"name": dev.get("name"),
                                              "days": int(up // 86400)})}


@rule("rel.offline_gear", "reliability", "critical",
      "Network gear unreachable: {name}")
def _rel_offline_gear(snap, dev):
    if dev.get("device_type") in GEAR_TYPES:
        ping = dev.get("ping") or {}
        if ping.get("reachable") is False:
            return {"detail": _fmt("{name} ({ip}) did not respond to ping during the "
                                   "scan — the gear is down or unreachable from the "
                                   "appliance.", {"name": dev.get("name"),
                                                  "ip": dev.get("ip")})}


@rule("rel.single_uplink", "reliability", "info",
      "Single uplink path on {name}")
def _rel_single_uplink(snap, dev):
    uni = dev.get("unifi") or {}
    if dev.get("device_type") in ("switch", "ap") and uni.get("uplink_mac"):
        return {"uplink_mac": uni.get("uplink_mac"),
                "detail": _fmt("{name} has a single uplink — no redundant path (LAG / "
                               "STP). A single upstream failure isolates it.",
                               {"name": dev.get("name")})}


@rule("rel.oper_down_admin_up", "reliability", "warning",
      "Link down on a provisioned interface of {name}")
def _rel_oper_down_admin_up(snap, dev):
    for iface in (dev.get("snmp") or {}).get("interfaces") or []:
        if (iface.get("oper_status") == "down"
                and iface.get("admin_status") == "up"
                and iface.get("iftype") in (6, 117, 161)):
            return {"interface": iface.get("ifdescr"),
                    "detail": _fmt("Interface {interface} on {name} is operationally "
                                   "DOWN while administratively UP — the far end is "
                                   "unplugged/down.", {"interface": iface.get("ifdescr"),
                                                       "name": dev.get("name")})}


@rule("rel.ap_uplink_missing", "reliability", "info",
      "AP without a wired uplink: {name}")
def _rel_ap_uplink_missing(snap, dev):
    uni = dev.get("unifi") or {}
    if dev.get("device_type") == "ap" and not uni.get("uplink_mac"):
        return {"detail": _fmt("{name} has no wired uplink (wireless mesh or isolated) "
                               "— verify it's meshed intentionally.", {"name": dev.get("name")})}


@rule("rel.wan_degraded", "reliability", "warning",
      "WAN health degraded on {name}")
def _rel_wan_degraded(snap, dev):
    uni = dev.get("unifi") or {}
    wan = uni.get("wan") or {}
    status = (wan.get("status") or "").lower()
    if dev.get("device_type") in ("gateway", "router") and status and status != "ok":
        return {"wan_status": wan.get("status"),
                "detail": _fmt("{name} reports WAN health '{status}' (not ok).",
                               {"name": dev.get("name"), "status": wan.get("status")})}


# ══════════════════════════════ HYGIENE ═══════════════════════════════════

@rule("hyg.stale_device", "hygiene", "info",
      "Stale device in scope: {name}")
def _hyg_stale_device(snap, dev):
    last = dev.get("last_seen")
    if last is not None:
        import datetime as _dt
        try:
            ts = _dt.datetime.fromisoformat(str(last).replace("Z", ""))
            age_days = (_dt.datetime.utcnow() - ts).total_seconds() / 86400
        except (ValueError, TypeError):
            return None
        if age_days >= STALE_DEVICE_DAYS:
            return {"last_seen": last, "age_days": int(age_days),
                    "detail": _fmt("{name} was last seen {age} days ago — remove or "
                                   "reconnect it.", {"name": dev.get("name"),
                                                     "age": int(age_days)})}


@rule("hyg.unnamed_uplink_port", "hygiene", "info",
      "Unnamed uplink port on {name}")
def _hyg_unnamed_uplink_port(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        nm = (p.get("name") or "").strip()
        if p.get("is_uplink") and p.get("up") and (not nm or nm.startswith("Port ")):
            return {"port": p.get("port_idx"), "name": nm,
                    "detail": _fmt("Uplink port idx {port} on {name} has no meaningful "
                                   "label — name it so future changes are safe.",
                                   {"port": p.get("port_idx"), "name": dev.get("name")})}


@rule("hyg.port_no_profile", "hygiene", "info",
      "Port with no assigned network on {name}")
def _hyg_port_no_profile(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if (p.get("up") and p.get("native_vlan") is None
                and not (p.get("tagged_vlans") or [])):
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "detail": _fmt("Port {name} (idx {port}) on {name} is up with no "
                                   "network profile assigned — traffic lands on the "
                                   "default network.",
                                   {"name": p.get("name"), "port": p.get("port_idx"),
                                    "dev": dev.get("name")})}


# ── snapshot-level rules ───────────────────────────────────────────────────

@snapshot_rule("hyg.default_vlan1", "hygiene", "info",
               "Corporate network on VLAN 1: {network}")
def _hyg_default_vlan1(snap):
    out = []
    for n in snap.get("networks") or []:
        if n.get("vlan") == MGMT_VLAN_ID and n.get("enabled"):
            out.append({"network": n.get("name"), "vlan": n.get("vlan"),
                        "detail": _fmt("Network '{net}' uses VLAN 1 — the default/untagged "
                                       "VLAN. Segregate traffic onto dedicated VLANs.",
                                       {"net": n.get("name")})})
    return out


@snapshot_rule("hyg.duplicate_ip", "hygiene", "critical",
               "Duplicate IP address in scope")
def _hyg_duplicate_ip(snap):
    out = []
    by_ip = {}
    for d in snap.get("devices") or []:
        ip = (d.get("ip") or "").strip()
        if ip:
            by_ip.setdefault(ip, []).append(d.get("name") or ip)
    for ip, names in by_ip.items():
        if len(names) > 1:
            out.append({"ip": ip, "devices": names,
                        "detail": _fmt("Multiple devices share IP {ip}: {names} — an IP "
                                       "conflict will cause intermittent reachability.",
                                       {"ip": ip, "names": ", ".join(names)})})
    return out


@snapshot_rule("hyg.duplicate_mac", "hygiene", "warning",
               "Duplicate MAC address in scope")
def _hyg_duplicate_mac(snap):
    out = []
    by_mac = {}
    for d in snap.get("devices") or []:
        mac = (d.get("mac") or "").strip().lower()
        if mac:
            by_mac.setdefault(mac, []).append(d.get("name") or mac)
    for mac, names in by_mac.items():
        if len(names) > 1:
            out.append({"mac": mac, "devices": names,
                        "detail": _fmt("Multiple devices share MAC {mac}: {names} — check "
                                       "for a duplicate/mis-cloned identity.",
                                       {"mac": mac, "names": ", ".join(names)})})
    return out


@snapshot_rule("hyg.unused_vlan", "hygiene", "info",
               "Unused VLAN: {network}")
def _hyg_unused_vlan(snap):
    out = []
    referenced = set()
    for d in snap.get("devices") or []:
        for p in (d.get("unifi") or {}).get("ports") or []:
            if p.get("native_vlan") is not None:
                referenced.add(p["native_vlan"])
            referenced.update(p.get("tagged_vlans") or [])
    for w in snap.get("wlans") or []:
        if w.get("enabled") and w.get("vlan") is not None:
            referenced.add(w["vlan"])
    for n in snap.get("networks") or []:
        vlan = n.get("vlan")
        if vlan is not None and vlan not in referenced:
            out.append({"network": n.get("name"), "vlan": vlan,
                        "detail": _fmt("VLAN {vlan} ('{net}') is defined but not referenced "
                                       "by any port or enabled SSID — candidate for "
                                       "cleanup.", {"vlan": vlan, "net": n.get("name")})})
    return out


@snapshot_rule("hyg.disabled_ssid", "hygiene", "info",
               "Disabled SSID: {ssid}")
def _hyg_disabled_ssid(snap):
    out = []
    for w in snap.get("wlans") or []:
        if not w.get("enabled"):
            out.append({"ssid": w.get("name"),
                        "detail": _fmt("SSID '{ssid}' is configured but disabled — remove "
                                       "it if it's no longer needed.", {"ssid": w.get("name")})})
    return out


@snapshot_rule("hyg.disabled_network", "hygiene", "info",
               "Disabled network: {network}")
def _hyg_disabled_network(snap):
    out = []
    for n in snap.get("networks") or []:
        if n.get("enabled") is False:
            out.append({"network": n.get("name"), "vlan": n.get("vlan"),
                        "detail": _fmt("Network '{net}' is disabled — remove it if unused.",
                                       {"net": n.get("name")})})
    return out


@snapshot_rule("hyg.vlan_without_name", "hygiene", "info",
               "VLAN with default name: {network}")
def _hyg_vlan_without_name(snap):
    out = []
    for n in snap.get("networks") or []:
        nm = (n.get("name") or "").strip().lower()
        if nm in ("network", "vlan only", "default", "") and n.get("vlan") is not None:
            out.append({"network": n.get("name"), "vlan": n.get("vlan"),
                        "detail": _fmt("VLAN {vlan} has the default name '{net}' — give it "
                                       "a descriptive name.", {"vlan": n.get("vlan"),
                                                           "net": n.get("name")})})
    return out


# ── helpers ────────────────────────────────────────────────────────────────

def _uptime_seconds(dev) -> "int | None":
    """Uptime in seconds from SNMP (preferred) or UniFi."""
    snmp = dev.get("snmp") or {}
    if snmp.get("uptime_seconds") is not None:
        return snmp["uptime_seconds"]
    uni = dev.get("unifi") or {}
    return uni.get("uptime_seconds")


def _mgmt_vlans(snap) -> set:
    """VLAN ids that look like management VLANs (id 1 or a mgmt-ish name)."""
    out = {MGMT_VLAN_ID}
    for n in snap.get("networks") or []:
        nm = (n.get("name") or "").lower()
        if any(k in nm for k in MGMT_VLAN_KEYWORDS) and n.get("vlan") is not None:
            out.add(n["vlan"])
    return out


def _is_ubiquiti(dev) -> bool:
    """True when the device is Ubiquiti gear (UniFi-managed or Ubiquiti
    vendor). Used to downgrade UniFi-default SSH to info."""
    if dev.get("unifi_managed"):
        return True
    blob = f"{dev.get('vendor') or ''} {dev.get('model') or ''}".lower()
    return "ubiquiti" in blob or "unifi" in blob


# ── evaluation + scoring ───────────────────────────────────────────────────

def _mk_finding(rule, dev, evidence) -> dict:
    evidence = dict(evidence or {})
    severity = evidence.pop("severity", None) or rule["severity"]
    detail = evidence.pop("detail", "")
    iface = evidence.get("interface") or evidence.get("port")
    return {
        "finding_key": rule["key"],
        "category": rule["category"],
        "severity": severity,
        "device_id": dev.get("device_id"),
        "interface": str(iface) if iface is not None else None,
        "title": _fmt(rule["title"], {**dev, **evidence}),
        "detail": detail,
        "evidence": evidence,
    }


def _mk_snapshot_finding(rule, evidence) -> dict:
    evidence = dict(evidence or {})
    severity = evidence.pop("severity", None) or rule["severity"]
    detail = evidence.pop("detail", "")
    return {
        "finding_key": rule["key"],
        "category": rule["category"],
        "severity": severity,
        "device_id": None,
        "interface": None,
        "title": _fmt(rule["title"], evidence),
        "detail": detail,
        "evidence": evidence,
    }


def evaluate(snapshot) -> list:
    """Run every rule over a scan snapshot. Returns a list of finding dicts.

    Never raises: a buggy rule is caught and skipped (a finding is advisory —
    it must not crash the scan)."""
    snapshot = snapshot or {}
    findings = []
    devices = snapshot.get("devices") or []
    for dev in devices:
        for rule in RULES:
            try:
                ev = rule["fn"](snapshot, dev)
            except Exception:
                ev = None
            if ev:
                findings.append(_mk_finding(rule, dev, ev))
    for rule in SNAPSHOT_RULES:
        try:
            evs = rule["fn"](snapshot)
        except Exception:
            evs = []
        for ev in (evs or []):
            if ev:
                findings.append(_mk_snapshot_finding(rule, ev))
    return findings


def score(findings) -> dict:
    """Overall + per-category scores (0-100, floor 0) and severity/category
    counts. The overall score is stored on scan_runs.score; the rest lands in
    the structured summary (scan_runs.summary JSON).

    PINNED SEMANTICS (gate decision 08-18):
      * criticals −20 each, stack, floor 0
      * warnings −5 each, stack, NO cap (genuine multi-warning networks score low)
      * infos −2 each but CAPPED — only the first INFO_COUNT_CAP count, then
        free (noise must never tank the score; 100 infos ⇒ ≈90)
    """
    findings = findings or []
    counts = {"critical": 0, "warning": 0, "info": 0}
    cat_counts = {c: 0 for c in CATEGORIES}
    cat_penalty = {c: 0 for c in CATEGORIES}
    total_penalty = 0
    info_counted = 0
    for f in findings:
        sev = f.get("severity", "info")
        cat = f.get("category", "hygiene")
        if sev not in counts:
            sev = "info"
        counts[sev] = counts.get(sev, 0) + 1
        w = SEVERITY_WEIGHT.get(sev, 0)
        if sev == "info":
            if info_counted >= INFO_COUNT_CAP:
                w = 0   # beyond the cap, info findings are free
            else:
                info_counted += 1
        total_penalty += w
        if cat in cat_counts:
            cat_counts[cat] += 1
            cat_penalty[cat] += w
    overall = max(0, 100 - total_penalty)
    categories = {
        c: max(0, 100 - cat_penalty[c]) for c in CATEGORIES
    }
    return {
        "overall": overall,
        "categories": categories,
        "counts": counts,
        "category_counts": cat_counts,
        "total": len(findings),
    }


def count_rules() -> int:
    return len(RULES) + len(SNAPSHOT_RULES)


_annotate_fixability()
