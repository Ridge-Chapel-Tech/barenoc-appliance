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

import re

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
LINK_STABLE_SECONDS = 24 * 3600  # link up this long with a high cumulative count = historical (08-19)
import time
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

# ── per-port discovery + classification (08-19 dead-end/loop detection) ──
# Best-effort: FDB/LLDP are 404 on this firmware, so the port_table counters
# (mac_table_count + rx/tx packet + multicast counters) are the signal. The
# thresholds below are tests-pinnable — a dead-end port 4 flood
# (up @1G, 0 MACs, rx≈1, tx_multicast≈1.4M) must classify as dead_end.
PORT_DEAD_END_MAX_RX_PACKETS = 10      # rx below this = "no real received traffic"
PORT_DEAD_END_MIN_TX_MULTICAST = 1000  # tx_multicast above this = a multicast flood
PORT_DEAD_END_MULTICAST_RATIO = 50.0   # tx_multicast >= max(1, rx) * this = "significantly above rx"
PORT_UNUSED_MAX_RX_PACKETS = 10        # ~zero traffic both ways
PORT_UNUSED_MAX_TX_PACKETS = 10
PORT_UNUSED_MAX_TX_MULTICAST = 10
PORT_CONNECTED_MIN_PACKETS = 10        # real RX/TX above this = a device is here

# ── traffic archetype (device identification WITHOUT packet capture) ──────
# A port carrying a Google/Nest
# router's WAN was inferred as "switch" (from the switch's own device type)
# and told to move to Management — stranding the Google WiFi. The archetype
# now classifies what is ON the port from the controller's exposed counters:
#   router_ap — 1 learned MAC + heavy bidirectional traffic (+ multicast)
#   switch    — many learned MACs (a downstream switch/trunk)
#   host      — 1 learned MAC, modest traffic
#   dead_end  — 0 MACs + a multicast flood (loop/dead-end cable)
#   unused    — 0 MACs + ~zero traffic
#   down      — link down / admin-disabled
#   unknown   — anything we cannot classify confidently (NEVER guess)
PORT_SWITCH_MIN_MACS = 3
PORT_EDGE_MIN_RX_PACKETS = 1000
PORT_EDGE_MIN_TX_PACKETS = 1000
PORT_EDGE_MIN_TX_MULTICAST = 200

CONSERVATIVE_ROUTER_AP_ACTION = ("likely a router/AP — left on the default "
                                 "network; verify before any change")
CONSERVATIVE_UNKNOWN_ACTION = "unknown device — verify before any change"


# ── OUI best-effort (small embedded vendor table) ─────────────────────────
# When a port's connected MAC is learnable (port mac_table or client-list
# correlation), map its OUI (first 3 bytes) to a vendor. Best-effort: some
# firmware hides port→MAC, so this fails gracefully to None (never a guess).
OUI_VENDORS = {
    # Google / Nest
    "001a11": "Google/Nest", "089e08": "Google/Nest", "18b430": "Google/Nest",
    "3c5ab4": "Google/Nest", "546009": "Google/Nest", "5c27d4": "Google/Nest",
    "641666": "Google/Nest", "703acb": "Google/Nest", "a47733": "Google/Nest",
    "d8eb97": "Google/Nest", "f4f5d8": "Google/Nest",
    # Apple
    "000393": "Apple", "000a27": "Apple", "001b63": "Apple", "001ec2": "Apple",
    "002500": "Apple", "0026bb": "Apple", "0c3021": "Apple", "14109f": "Apple",
    "186590": "Apple", "209bcd": "Apple", "28cfe9": "Apple", "2c200b": "Apple",
    "38c986": "Apple", "403004": "Apple", "442a60": "Apple", "48d705": "Apple",
    "54e43a": "Apple", "68967b": "Apple", "703c69": "Apple", "7831c1": "Apple",
    "881fa1": "Apple", "8c8590": "Apple", "a45e60": "Apple", "a85c2c": "Apple",
    "acbc32": "Apple", "b0702d": "Apple", "bc926b": "Apple", "c0847a": "Apple",
    "c86f1d": "Apple", "dc2b2a": "Apple", "e0b55f": "Apple", "e4ce8f": "Apple",
    "f01898": "Apple", "f0dbe2": "Apple",
    # Ubiquiti
    "00156d": "Ubiquiti", "002722": "Ubiquiti", "0418d6": "Ubiquiti",
    "24a43c": "Ubiquiti", "44d9e7": "Ubiquiti", "687251": "Ubiquiti",
    "70a741": "Ubiquiti", "788a20": "Ubiquiti", "802aa8": "Ubiquiti",
    "b4fbe4": "Ubiquiti", "dc9fdb": "Ubiquiti", "e063da": "Ubiquiti",
    "f09fc2": "Ubiquiti", "fcecda": "Ubiquiti",
    # common top OUIs (best-effort — never exhaustive)
    "001c0f": "Cisco", "0023ac": "Cisco", "3c0e23": "Cisco", "4c6e6e": "Cisco",
    "5897bd": "Cisco", "7079b3": "Cisco", "e0acf1": "Cisco",
    "3c7dd9": "Intel", "5c5eab": "Intel", "889c16": "Intel", "a4bb6d": "Intel",
    "f45c89": "Intel",
    "001dd5": "Samsung", "20719e": "Samsung", "8c8b83": "Samsung",
    "a04f85": "Samsung", "e4788a": "Samsung",
    "008ab8": "Amazon", "0cb394": "Amazon", "68d0f8": "Amazon",
    "8c0ca6": "Amazon", "b0c554": "Amazon", "fc65de": "Amazon",
    "0024e4": "Microsoft", "0060dd": "Microsoft", "b826d4": "Microsoft",
    "c42f90": "Microsoft", "f02f74": "Microsoft",
    "0015db": "Sonos", "5caafd": "Sonos", "78a86a": "Sonos", "949f3e": "Sonos",
    "b8e937": "Sonos", "dcbd9c": "Sonos",
    "00a04e": "Roku", "5ce0ca": "Roku", "8c3e7c": "Roku", "d8a24e": "Roku",
    "008ecc": "LG", "a0a3c8": "LG", "c0d3b5": "LG", "e03f49": "LG",
    "00242b": "Sony", "1cf0e1": "Sony", "f0b40e": "Sony",
    "001a6c": "TP-Link", "14cc20": "TP-Link", "50c7bf": "TP-Link",
    "a0f3c1": "TP-Link", "b0be76": "TP-Link", "c46e1f": "TP-Link",
    "001e2a": "Netgear", "201331": "Netgear", "a021b7": "Netgear",
    "b07fb9": "Netgear", "cc40d0": "Netgear", "e0469a": "Netgear",
    "001fc5": "ASUS", "042e8c": "ASUS", "3871de": "ASUS", "ac9e17": "ASUS",
    "d850e6": "ASUS",
    "001b11": "D-Link", "14d64d": "D-Link", "b075d5": "D-Link",
    "c8be19": "D-Link",
    "001150": "Belkin", "944452": "Belkin",
    "b827eb": "Raspberry Pi", "dc7188": "Raspberry Pi", "e45f01": "Raspberry Pi",
    "246f28": "Espressif", "30aea4": "Espressif", "40f520": "Espressif",
    "7c9ebd": "Espressif", "84f3eb": "Espressif", "b4e62d": "Espressif",
    "0017c0": "Texas Instruments", "f0b014": "Texas Instruments",
    "00e04c": "Realtek", "1019ce": "Realtek", "8cdcd4": "Realtek",
}


def _oui_of_mac(mac) -> str:
    """First 3 bytes of a MAC, normalized to 6 lowercase hex chars ('' when
    the MAC is unlearnable/malformed)."""
    h = re.sub(r"[^0-9a-fA-F]", "", str(mac or ""))
    return h[:6].lower() if len(h) >= 6 else ""


def oui_vendor(mac) -> "str | None":
    """Vendor name for a MAC's OUI (the embedded table), or None when the OUI
    is unknown/unlearnable. Fails gracefully — never guesses."""
    return OUI_VENDORS.get(_oui_of_mac(mac))


def oui_guess(mac) -> "str | None":
    """'likely Google/Nest gear' phrasing for a learnable MAC, else None."""
    vendor = oui_vendor(mac)
    if not vendor:
        return None
    return f"likely {vendor} gear"


# ── VLAN awareness + the NO-FLAT guardrail (netopt-vlan-awareness) ────────
# The scan understands VLANs and subnetting: every port's native/tagged
# assignment carries meaning, recommendations respect the multi-VLAN design,
# and no recommendation may collapse VLANs/subnets (never flatten).

DEFAULT_NETWORK_KEY = "default"   # the untagged/corporate network (vlan_enabled=False)

# Device class -> network name keywords it belongs on (the 9-VLAN standard).
# APs belong on the WiFi segment; the management plane of gateways/routers/
# switches belongs on Management; hosts/servers belong on Production. These
# are resolved against the LIVE VLAN map by name keyword — the catch-all
# default network is NEVER the recommendation.
CLASS_NETWORK_KEYWORDS = {
    "ap": ("wifi", "wireless", "wlan"),
    "gateway": ("management", "mgmt"),
    "router": ("management", "mgmt"),
    "switch": ("management", "mgmt"),
    "host": ("production", "prod"),
    "server": ("production", "prod"),
}

GUARDRAIL_FLAG = ("design change — not recommended (would collapse VLAN/subnet "
                  "segmentation)")

# NO-FLAT guardrail: a candidate suggested_action matching any of these would
# collapse VLANs/subnets (assign everything to one network, remove VLAN tags,
# flatten the design) — it is suppressed, never recommended.
FLATTEN_PATTERNS = (
    re.compile(r"everything\s+(on|to|onto)\s+(one|a single|the same)\s+network", re.I),
    re.compile(r"put\s+(everything|all\s+(devices|traffic))\s+(on|onto)\s+one", re.I),
    re.compile(r"(assign|move)\s+everything\s+(to|onto)\s+(one\s+)?(network|vlan)", re.I),
    re.compile(r"one\s+flat\s+network", re.I),
    re.compile(r"single\s+flat\s+network", re.I),
    re.compile(r"collapse\s+(the\s+)?(vlans?|subnets?|networks?)", re.I),
    re.compile(r"remove\s+(all\s+)?vlan\s+tags?", re.I),
    re.compile(r"untag\s+everything", re.I),
    re.compile(r"flatten(ing)?\s+(the\s+)?(network|design|vlans?|subnets?)", re.I),
    re.compile(r"simplify\s+.*(vlan|subnet|network|tag)", re.I),
)



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
    "rel.link_down_count": "Investigate the link stability — check whether recent "
                           "maintenance (PoE cycles / config changes) explains the "
                           "transitions before replacing any cable.",
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
    "hyg.dead_end_port": "Disable the port (stops the multicast flood); trace the cable "
                         "before re-enabling.",
    "hyg.unused_port_up": "Disable the unused port.",
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
    "hyg.dead_end_port",         # disabling a port drops whatever is (or isn't) behind it
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
    "hyg.dead_end_port": {
        "high_risk": True,
        "blast_radius": ("Disabling a port drops everything behind it. If the port really "
                         "is a loop/dead-end it only stops the multicast flood, but if it "
                         "actually carries a downstream switch/AP (or the management path) "
                         "those devices strand until the port is re-enabled."),
        "plan_note": ("PLAN FIRST: confirm the port has learned no MACs and is NOT an "
                      "uplink/trunk; capture the full port state, disable the port, verify "
                      "the flood stops AND the rest of the network stays up; keep the "
                      "re-enable rollback ready."),
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


def _base_action(key: str) -> str:
    """The bare suggested action for a finding key (no risk metadata)."""
    return SUGGESTED_ACTIONS.get((key or "").strip(), DEFAULT_SUGGESTED_ACTION)


def _with_risk(key: str, action: str) -> str:
    """Append the risk blast radius + plan-first note for high-risk keys."""
    risk = risk_meta(key)
    if risk["high_risk"]:
        return f"{action} {risk['blast_radius']} {risk['plan_note']}"
    return action


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
    suggested = _with_risk(key, _base_action(key))
    return {"key": key, "fixable": True, "suggested_action": suggested,
            "high_risk": risk["high_risk"], "blast_radius": risk["blast_radius"],
            "plan_note": risk["plan_note"]}


def is_flattening(text) -> bool:
    """True when a candidate suggested_action would collapse VLANs/subnets
    (the NO-FLAT guardrail): assign everything to one network, remove VLAN
    tags, flatten the design, etc. Negated mentions ('never collapse the
    VLANs…') are the anti-flatten statement, not a recommendation — they are
    NOT flagged."""
    if not text:
        return False
    text = str(text)
    negation = ("never", "do not", "don't", "dont", "avoid", "not ")
    for p in FLATTEN_PATTERNS:
        for m in p.finditer(text):
            prefix = text[max(0, m.start() - 32):m.start()].lower()
            if any(w in prefix for w in negation):
                continue
            return True
    return False


def guardrail_verdict(text) -> dict:
    """Apply the NO-FLAT guardrail to a candidate suggested_action.
    Returns {allowed, action, flag} — flattening candidates are suppressed
    (action becomes '') and explicitly flagged 'design change — not recommended'."""
    text = (text or "").strip()
    if is_flattening(text):
        return {"allowed": False, "action": "", "flag": GUARDRAIL_FLAG}
    return {"allowed": True, "action": text, "flag": ""}


def suggested_action_for(key: str, evidence=None) -> str:
    """The VLAN-aware suggested action for a finding — the single accessor
    the run-detail API + optimize ticket helper must use.

    A finding may carry a DYNAMIC suggested_action in its evidence (the
    ``hyg.port_no_profile`` rule names the CORRECT network for the device
    class). The NO-FLAT guardrail is applied on every path: a flattening
    candidate (dynamic or static) is suppressed and never recommended."""
    key = (key or "").strip()
    ev = dict(evidence or {})
    dynamic = str(ev.get("suggested_action") or "").strip()
    if dynamic:
        verdict = guardrail_verdict(dynamic)
        if not verdict["allowed"]:
            return ""
        return _with_risk(key, verdict["action"])
    static = fixability(key)["suggested_action"]
    verdict = guardrail_verdict(static)
    return verdict["action"]


def apply_no_flat_guardrail(findings) -> list:
    """The NO-FLAT guardrail as an analysis-layer rule: scan every finding's
    (dynamic or static) suggested action and suppress any that would flatten,
    stamping ``guardrail_flag`` on the evidence so the UI can show WHY.
    Non-flattening findings pass through untouched."""
    out = []
    for f in (findings or []):
        f = dict(f)
        ev = dict(f.get("evidence") or {})
        candidate = str(ev.get("suggested_action") or _base_action(f.get("finding_key", "")) or "")
        if is_flattening(candidate):
            ev["suggested_action"] = ""
            ev["guardrail_flag"] = GUARDRAIL_FLAG
            f["evidence"] = ev
        out.append(f)
    return out


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
      "{port_label}: link negotiated down to 100 Mbps")
def _perf_link_speed_100(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if p.get("up") and p.get("speed_mbps") == 100 and (p.get("max_speed_mbps") or 0) >= 1000:
            label = port_label(dev, p)
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "port_label": label,
                    "speed_mbps": 100, "max_speed_mbps": p.get("max_speed_mbps"),
                    "detail": _fmt("{port_label} is up at 100 Mbps on a "
                                   "gigabit-capable port — check the cable/patch and the "
                                   "peer's negotiation.", {"port_label": label})}


@rule("perf.link_speed_10", "performance", "warning",
      "{port_label}: link negotiated down to 10 Mbps")
def _perf_link_speed_10(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if p.get("up") and p.get("speed_mbps") is not None and p.get("speed_mbps") <= 10:
            label = port_label(dev, p)
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "port_label": label,
                    "speed_mbps": p.get("speed_mbps"),
                    "detail": _fmt("{port_label} is up at {speed} Mbps — a "
                                   "hardware/negotiation fault that will bottleneck "
                                   "traffic.", {"port_label": label,
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
      "{port_label}: port errors")
def _perf_port_errors_unifi(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        total = (p.get("tx_errors") or 0) + (p.get("rx_errors") or 0)
        if p.get("up") and total > 0:
            label = port_label(dev, p)
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "port_label": label,
                    "tx_errors": p.get("tx_errors"), "rx_errors": p.get("rx_errors"),
                    "detail": _fmt("{port_label} reports {total} TX/RX "
                                   "errors — physical-layer problem (cable/transceiver).",
                                   {"port_label": label, "total": total})}


@rule("perf.uplink_congestion", "performance", "info",
      "{port_label}: uplink negotiated below capacity")
def _perf_uplink_congestion(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if (p.get("is_uplink") and p.get("up") and p.get("speed_mbps") is not None
                and p.get("speed_mbps") <= SNMP_LOW_SPEED_MBPS):
            label = port_label(dev, p)
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "port_label": label,
                    "speed_mbps": p.get("speed_mbps"),
                    "detail": _fmt("{port_label} is carrying a "
                                   "downlink at only {speed} Mbps — a congestion "
                                   "bottleneck for everything behind it.",
                                   {"port_label": label,
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
      "{port_label}: management VLAN on uplink")
def _sec_mgmt_vlan_on_uplink(snap, dev):
    mgmt = _mgmt_vlans(snap)
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if not p.get("is_uplink"):
            continue
        vlans = set(p.get("tagged_vlans") or [])
        native = p.get("native_vlan")
        if native in mgmt or (vlans & mgmt):
            label = port_label(dev, p)
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "port_label": label,
                    "native_vlan": native, "mgmt_vlans": sorted(vlans & mgmt) or [native],
                    "detail": _fmt("{port_label} carries the "
                                   "management VLAN — keep the management plane on a "
                                   "dedicated, restricted VLAN.", {"port_label": label})}


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
      "{port_label}: link has flapped")
def _rel_link_down_count(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if (p.get("link_down_count") or 0) >= LINK_DOWN_COUNT_WARN:
            label = port_label(dev, p)
            n = p.get("link_down_count") or 0
            ca = p.get("connected_at")
            stable_since = (time.time() - ca) if ca else None
            # Recency window (08-19): a high CUMULATIVE count with a link that has
            # been up a long while is HISTORICAL (e.g. maintenance — PoE cycles,
            # VLAN moves), not an active fault. Only the recent case warns; the
            # historical case drops to info with a "watch, don't re-cable" note.
            if stable_since is not None and stable_since > LINK_STABLE_SECONDS and p.get("up"):
                return {"port": p.get("port_idx"), "name": p.get("name"),
                        "port_label": label,
                        "severity": "info",
                        "link_down_count": n,
                        "detail": _fmt("{port_label} has recorded {n} link-down "
                                       "transition(s) but the link has been stable "
                                       "for a while — the transitions look historical "
                                       "(e.g. maintenance actions), not an active "
                                       "fault. Watch it; don't re-cable yet.",
                                       {"port_label": label, "n": n})}
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "port_label": label,
                    "link_down_count": n,
                    "detail": _fmt("{port_label} has recorded {n} link-down "
                                   "transition(s) recently — investigate; check "
                                   "whether recent maintenance (PoE cycles / config "
                                   "changes) explains them before replacing any cable.",
                                   {"port_label": label, "n": n})}


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
      "{port_label}: unnamed uplink port")
def _hyg_unnamed_uplink_port(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        nm = (p.get("name") or "").strip()
        if p.get("is_uplink") and p.get("up") and (not nm or nm.startswith("Port ")):
            label = port_label(dev, p)
            return {"port": p.get("port_idx"), "name": nm,
                    "port_label": label,
                    "detail": _fmt("{port_label} has no meaningful "
                                   "label — name it so future changes are safe.",
                                   {"port_label": label})}


@rule("hyg.port_no_profile", "hygiene", "info",
      "{port_label}: no assigned network")
def _hyg_port_no_profile(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if not (p.get("up") and not port_has_profile(p)):
            continue
        vlan_map = snapshot_vlan_map(snap)
        action = port_profile_action(dev, p, vlan_map)
        arch = classify_archetype(p)
        net = None
        if arch not in ("router_ap", "unknown"):
            net = suggested_network(_port_device_class(dev, p, arch), vlan_map)
        label = port_label(dev, p)
        ev = {"port": p.get("port_idx"), "name": p.get("name"),
              "port_label": label,
              "detail": _fmt("{port_label} is up with no network profile "
                             "assigned — traffic lands on the default network.",
                             {"port_label": label})}
        if net:
            ev["suggested_network"] = {"name": net.get("name"),
                                        "vlan": net.get("vlan"),
                                        "subnet": net.get("subnet")}
        if action:
            ev["suggested_action"] = action
        return ev


@rule("hyg.dead_end_port", "hygiene", "warning",
      "{port_label}: no devices learned + multicast flooding — looks like a loop or dead-end cable")
def _hyg_dead_end_port(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if classify_port(p) == "dead_end":
            label = port_label(dev, p)
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "port_label": label,
                    "mac_table_count": _as_int(p.get("mac_table_count")),
                    "rx_packets": _as_int(p.get("rx_packets")),
                    "tx_packets": _as_int(p.get("tx_packets")),
                    "tx_multicast": _as_int(p.get("tx_multicast")),
                    "stp_state": p.get("stp_state"),
                    "speed_mbps": p.get("speed_mbps"),
                    "uplink_devices": [str(x) for x in (p.get("uplink_devices") or []) if x],
                    "detail": _fmt("{port_label} is UP at {speed} Mbps but has learned no "
                                   "devices ({mac} MACs) and is flooding {tx_mc} multicast "
                                   "packets while receiving ~{rx} — the signature of a loop "
                                   "or a dead-end cable (STP {stp}).",
                                   {"port_label": label, "speed": p.get("speed_mbps"),
                                    "mac": _as_int(p.get("mac_table_count")),
                                    "tx_mc": _as_int(p.get("tx_multicast")),
                                    "rx": _as_int(p.get("rx_packets")),
                                    "stp": p.get("stp_state") or "unknown"})}


@rule("hyg.unused_port_up", "hygiene", "info",
      "{port_label}: link up but no devices — disable for hygiene/safety")
def _hyg_unused_port_up(snap, dev):
    for p in (dev.get("unifi") or {}).get("ports") or []:
        if classify_port(p) == "unused":
            label = port_label(dev, p)
            return {"port": p.get("port_idx"), "name": p.get("name"),
                    "port_label": label,
                    "mac_table_count": _as_int(p.get("mac_table_count")),
                    "rx_packets": _as_int(p.get("rx_packets")),
                    "tx_packets": _as_int(p.get("tx_packets")),
                    "tx_multicast": _as_int(p.get("tx_multicast")),
                    "stp_state": p.get("stp_state"),
                    "speed_mbps": p.get("speed_mbps"),
                    "uplink_devices": [str(x) for x in (p.get("uplink_devices") or []) if x],
                    "detail": _fmt("{port_label} is UP but has learned no devices and "
                                   "carries ~no traffic — disable it for hygiene/safety.",
                                   {"port_label": label})}


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



# ── VLAN map + device-class→network + story text (netopt-vlan-awareness) ──

def build_vlan_map(networks) -> dict:
    """Build the scan's NETWORK MAP from a ``rest/networkconf``-shaped
    ``networks`` list: {key: {name, vlan, subnet, purpose, enabled}}.

    Tagged networks key by ``str(vlan_id)``; the untagged/corporate network
    (vlan_enabled=False -> vlan is None) keys by DEFAULT_NETWORK_KEY so the
    map never loses the 'native Default' story."""
    m = {}
    for n in (networks or []):
        if not isinstance(n, dict):
            continue
        vlan = n.get("vlan")
        key = DEFAULT_NETWORK_KEY if vlan is None else str(vlan)
        m[key] = {
            "name": n.get("name") or ("Default" if vlan is None else f"VLAN {vlan}"),
            "vlan": vlan,
            "subnet": n.get("subnet") or "",
            "purpose": n.get("purpose") or "",
            "enabled": bool(n.get("enabled", True)),
        }
    return m


def subnet_short(subnet) -> str:
    """'10.0.5.1/24' -> '.5.1/24' (the story's compact form — the third
    octet onward, matching a 'WiFi 10.0.5.x' convention)."""
    s = str(subnet or "").strip()
    if not s:
        return ""
    octets = s.split(".")
    if len(octets) >= 3:
        return "." + ".".join(octets[2:])
    return s


def _find_map_entry(vlan_map, vlan=None, name=None) -> "dict | None":
    """Look up a VLAN map entry by vlan id, then (case-insensitive) name."""
    vlan_map = vlan_map or {}
    if vlan is not None and str(vlan) in vlan_map:
        return vlan_map[str(vlan)]
    if name:
        for e in vlan_map.values():
            if (e.get("name") or "").lower() == str(name).lower():
                return e
    if vlan is None:
        return vlan_map.get(DEFAULT_NETWORK_KEY)
    return None


def network_entry_str(entry) -> str:
    """'WiFi (10.0.5.1/24)' / 'Default (10.0.1.1/24)' — a map entry as prose."""
    entry = entry or {}
    name = entry.get("name") or "network"
    vlan = entry.get("vlan")
    short = subnet_short(entry.get("subnet"))
    if vlan is not None:
        s = f"{name} vlan{vlan}"
    else:
        s = name
    if short:
        s = f"{s} ({short})"
    return s


def network_label(vlan, vlan_map) -> str:
    """'IoT(9)' — name + vlan id for the tagged-list story."""
    entry = _find_map_entry(vlan_map, vlan=vlan)
    name = (entry or {}).get("name") or (f"VLAN {vlan}" if vlan is not None else "Default")
    if vlan is not None:
        return f"{name}({vlan})"
    return name


def vlan_story(port, vlan_map) -> str:
    """The port's VLAN context as prose — 'native WiFi (10.0.5.1/24),
    tagged IoT(9)/Video(10)'. ``port`` carries the collector's enriched
    ``native_network``/``native_vlan``/``tagged_vlans`` fields."""
    vlan_map = vlan_map or {}
    port = port or {}
    native_name = port.get("native_network")
    native_vlan = port.get("native_vlan")
    parts = []
    if native_name is None and native_vlan is None:
        parts.append("native (unassigned)")
    else:
        entry = _find_map_entry(vlan_map, vlan=native_vlan, name=native_name)
        parts.append("native " + (network_entry_str(entry) if entry
                                  else (native_name or f"vlan {native_vlan}")))
    tagged = [v for v in (port.get("tagged_vlans") or []) if v is not None]
    if tagged:
        parts.append("tagged " + "/".join(network_label(v, vlan_map) for v in tagged))
    return ", ".join(parts)


def snapshot_vlan_map(snap) -> dict:
    """The VLAN map for a snapshot — ``snap['vlan_map']`` when the collector
    pre-built it, else built on the fly from ``snap['networks']`` (so rule
    unit tests work with a bare networks list)."""
    return snap.get("vlan_map") or build_vlan_map(snap.get("networks") or [])


def _device_class_label(device_class) -> str:
    return {"ap": "access point", "switch": "switch", "gateway": "gateway",
            "router": "router", "host": "host", "server": "server"}.get(
                (device_class or "").lower(), "device")


def suggested_network(device_class, vlan_map) -> "dict | None":
    """The network a device of this class SHOULD live on (the 9-VLAN standard):
    AP -> WiFi, gateway/router/switch management -> Management, host/server ->
    Production. Resolved from the LIVE map by name keyword; NEVER the catch-all
    default network."""
    vlan_map = vlan_map or {}
    for kw in CLASS_NETWORK_KEYWORDS.get((device_class or "").lower(), ()):
        for e in vlan_map.values():
            if not e.get("enabled", True):
                continue
            nm = (e.get("name") or "").lower()
            if kw in nm and e.get("vlan") is not None:
                return e
    return None


def port_profile_action(dev, port, vlan_map) -> "str | None":
    """The suggested_action for a port with no assigned network.

    Conservative first: a port classified router/AP-like (edge device) or
    UNKNOWN is NEVER told to change networks — the action says verify before
    any change (a Google/Nest router's
    WAN was inferred as 'switch' and told to move to Management, which would
    have stranded it). Only a confidently classified host/switch gets a
    network suggestion, and an uplink always gets the full VLAN trunk."""
    vlan_map = vlan_map or {}
    dev = dev or {}
    port = port or {}
    label = f"{dev.get('name') or 'device'} Port {port.get('port_idx')}"
    desc = str(port.get("name") or "").strip()
    if desc and not desc.startswith("Port "):
        label = f"{label} ({desc})"
    arch = classify_archetype(port)
    conservative = conservative_port_action(arch)
    if conservative:
        return conservative
    if port.get("is_uplink"):
        vlans = sorted((int(k) for k, e in vlan_map.items()
                        if k != DEFAULT_NETWORK_KEY and e.get("enabled", True)
                        and e.get("vlan") is not None))
        if vlans:
            names = ", ".join(network_label(v, vlan_map) for v in vlans)
            return (f"Assign the full VLAN trunk to {label}: tag {names} (native "
                    f"Management) so every segment reaches downstream gear — never "
                    f"collapse the VLANs onto a single network.")
        return (f"Assign the appropriate VLAN trunk to {label} (all segments tagged, "
                f"native Management) — never collapse the VLANs onto a single network.")
    dclass = _port_device_class(dev, port, arch)
    net = suggested_network(dclass, vlan_map)
    if not net:
        return None
    return (f"Assign {network_entry_str(net)} as the native network for {label} — "
            f"{net['name']} is the correct segment for a {_device_class_label(dclass)}, "
            f"not the default/catch-all network.")

def _port_desc(p) -> str:
    """The port's user description (the UniFi port ``name`` field) or '' when
    the port is unnamed. UniFi auto-names an unnamed port 'Port <idx>' — that
    placeholder is NOT a description."""
    nm = str(p.get("name") or "").strip()
    if not nm or nm.startswith("Port "):
        return ""
    return nm


def port_label(dev, p) -> str:
    """Canonical port naming for findings and tickets: '<dev.name> Port <idx>'
    with the port's description in parentheses when one exists — e.g.
    'Switch-02 Port 7 (Google WAN)'. Display-only: the finding key is
    unchanged."""
    label = f"{dev.get('name') or 'unknown'} Port {p.get('port_idx')}"
    desc = _port_desc(p)
    if desc:
        label = f"{label} ({desc})"
    return label



def _as_int(v) -> int:
    """Coerce a counter/value to int, tolerating None/missing/string forms."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def port_has_profile(p) -> bool:
    """True when a port has a working network profile: an effective native
    (including the untagged/corporate Default — whose ``native_vlan`` is None
    but whose ``native_network`` is 'Default') or a tagged set.

    A switch-port regression: the override set native=Default,
    but the collector only read the port_table (native_vlan=None, no tagged)
    so the 'no profile' rule fired on a CONFIGURED port."""
    p = p or {}
    if p.get("native_vlan") is not None:
        return True
    if p.get("native_network") is not None:
        return True
    tagged = [v for v in (p.get("tagged_vlans") or []) if v is not None]
    if tagged:
        return True
    return False


def classify_port(port) -> str:
    """Best-effort classification of a UniFi per-port snapshot — one of
    'down', 'dead_end', 'unused', 'connected'.

    * ``dead_end`` — UP + no learned MACs + negligible RX + a multicast flood
      (a dead-end port loop signature).
    * ``unused``   — UP + no learned MACs + ~zero traffic both ways.
    * ``connected``— a device is here (learned MACs and/or real RX/TX), or we
      cannot rule it out (conservative default).
    * ``down``     — link down (or admin-disabled).
    """
    p = port or {}
    if not p.get("up"):
        return "down"
    mac = _as_int(p.get("mac_table_count"))
    rx = _as_int(p.get("rx_packets"))
    tx = _as_int(p.get("tx_packets"))
    tx_mc = _as_int(p.get("tx_multicast"))
    if (mac == 0 and rx <= PORT_DEAD_END_MAX_RX_PACKETS
            and tx_mc >= PORT_DEAD_END_MIN_TX_MULTICAST
            and tx_mc >= max(1, rx) * PORT_DEAD_END_MULTICAST_RATIO):
        return "dead_end"
    if (mac == 0 and rx <= PORT_UNUSED_MAX_RX_PACKETS
            and tx <= PORT_UNUSED_MAX_TX_PACKETS
            and tx_mc <= PORT_UNUSED_MAX_TX_MULTICAST):
        return "unused"
    if mac > 0 or rx >= PORT_CONNECTED_MIN_PACKETS or tx >= PORT_CONNECTED_MIN_PACKETS:
        return "connected"
    return "connected"


def classify_archetype(port) -> str:
    """Traffic archetype of what is ON a port (device identification without
    packet capture) — one of 'down', 'dead_end', 'unused', 'switch',
    'router_ap', 'host', or 'unknown'.

    * router_ap — 1 learned MAC + heavy bidirectional traffic (or a high
      multicast rate): a router/AP-like edge device (a Google/Nest WAN port).
    * switch    — many learned MACs (a downstream switch/trunk).
    * host      — 1 learned MAC, modest traffic.
    * unknown   — anything we cannot classify confidently. NEVER guessed.
    """
    base = classify_port(port)
    if base in ("down", "dead_end", "unused"):
        return base
    mac = _as_int((port or {}).get("mac_table_count"))
    rx = _as_int((port or {}).get("rx_packets"))
    tx = _as_int((port or {}).get("tx_packets"))
    tx_mc = _as_int((port or {}).get("tx_multicast"))
    if mac >= PORT_SWITCH_MIN_MACS:
        return "switch"
    if mac == 1:
        heavy_bidirectional = (rx >= PORT_EDGE_MIN_RX_PACKETS
                               and tx >= PORT_EDGE_MIN_TX_PACKETS)
        if heavy_bidirectional or tx_mc >= PORT_EDGE_MIN_TX_MULTICAST:
            return "router_ap"
        return "host"
    return "unknown"


def conservative_port_action(archetype) -> "str | None":
    """The conservative 'never suggest a network change' action for a
    router/AP-like or unknown port. None for confidently classified ports
    (their change guidance, if any, comes from the port-profile rule)."""
    if archetype == "router_ap":
        return CONSERVATIVE_ROUTER_AP_ACTION
    if archetype == "unknown":
        return CONSERVATIVE_UNKNOWN_ACTION
    return None


def _port_device_class(dev, port, archetype) -> str:
    """The effective device class for a port's connected device: the traffic
    archetype when it names a class (switch/host), else the scanned device's
    own type (covers an AP's own port with no traffic counters)."""
    cls = {"switch": "switch", "host": "host"}.get(archetype)
    if cls:
        return cls
    return (dev.get("device_type") or "unknown").lower()


def _port_connected_mac(p) -> str:
    """The port's best single connected MAC (client-list correlation first,
    then the port's learned-MAC table) — '' when not learnable. The firmware
    hides port→MAC on some builds; this fails soft."""
    p = p or {}
    for field in ("connected_mac",):
        v = str(p.get(field) or "").strip()
        if v:
            return v
    for field in ("client_macs", "learned_macs"):
        seq = p.get(field) or []
        if isinstance(seq, (list, tuple)) and seq:
            v = str(seq[0]).strip()
            if v:
                return v
    return ""


def port_discovery(dev, p) -> dict:
    """The per-port discovery story: classification + traffic archetype +
    counters + device identity (OUI/DHCP, best-effort) + the conservative
    no-change action for router/AP-like and unknown ports."""
    cls = classify_port(p)
    arch = classify_archetype(p)
    mac = _as_int(p.get("mac_table_count"))
    uplinks = [str(x) for x in (p.get("uplink_devices") or []) if x]
    connected_mac = _port_connected_mac(p)
    device_guess = oui_guess(connected_mac) if connected_mac else None
    dhcp_hostname = str(p.get("dhcp_hostname") or "").strip()
    if cls == "connected":
        if uplinks:
            what = "known device: " + ", ".join(uplinks)
        elif mac:
            what = f"{mac} MAC(s) learned"
        else:
            what = "traffic present, no MACs learned"
    elif cls == "dead_end":
        what = "no devices learned + multicast flooding"
    elif cls == "unused":
        what = "link up, no devices, no traffic"
    elif cls == "down":
        what = "link down" + (" (admin-disabled)" if p.get("disabled") else "")
    else:
        what = cls
    return {
        "device": dev.get("name"),
        "port_idx": p.get("port_idx"),
        "label": port_label(dev, p),
        "description": _port_desc(p),
        "up": bool(p.get("up")),
        "disabled": bool(p.get("disabled")),
        "speed_mbps": p.get("speed_mbps"),
        "stp_state": p.get("stp_state"),
        "mac_table_count": mac,
        "rx_packets": _as_int(p.get("rx_packets")),
        "tx_packets": _as_int(p.get("tx_packets")),
        "tx_multicast": _as_int(p.get("tx_multicast")),
        "classification": cls,
        "archetype": arch,
        "uplink_devices": uplinks,
        "connected_mac": connected_mac or None,
        "device_guess": device_guess,
        "dhcp_hostname": dhcp_hostname or None,
        "suggested_action": conservative_port_action(arch) or "",
        "what": what,
    }


def build_port_discovery(snapshot) -> list:
    """Per-port discovery for every UniFi-managed device in a snapshot (the
    run detail shows this alongside the findings so the optimize/report tells
    the story, not just the score)."""
    out = []
    for dev in snapshot.get("devices") or []:
        uni = dev.get("unifi") or {}
        for p in uni.get("ports") or []:
            out.append(port_discovery(dev, p))
    return out


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
    it must not crash the scan). The NO-FLAT guardrail runs as the final
    analysis-layer pass (any flattening recommendation is suppressed)."""
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
    return apply_no_flat_guardrail(findings)


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
