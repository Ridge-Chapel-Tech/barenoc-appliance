# UniFi Controller API — BareNOC Runbook (captured 2026-08-02)

Everything we learned the hard way, verified against the **UCG-Max (UniFi OS 4.x,
Network app 9.x)** at `https://192.0.2.1`. Use this to avoid re-deriving it.
The `UniFiClient` in `src/api/unifi.py` implements most of this; the notes below
document the wire-level behavior for new work.

## 1. Auth (UniFi OS 4.x)

- **`POST /api/auth/login`** with `{"username","password"}` — success sets a
  `TOKEN` cookie + returns `x-csrf-token` **header** (also `x-updated-csrf-token`).
- The legacy **`POST /api/login` is DISABLED** on UCG/UDM (returns 401 even with
  correct creds). Older standalone controllers still use it (fallback in
  `unifi.py::login`).
- Every subsequent request needs the session cookie **and** the `X-CSRF-Token`
  header. Keep a cookie jar; capture the token from login response headers.
- UI.com/SSO accounts CANNOT authenticate — a **Local Access Account** (or API
  key) is required.
- **API key (recommended):** UniFi Network → Settings → Integrations → local API
  key; send `X-API-KEY` on every request — no session, no CSRF. `UniFiClient`
  supports it via `api_key=`.

## 2. Path layout

- UniFi OS proxies the Network app under **`/proxy/network/api/s/{site}/...`**.
  The legacy root `/api/s/{site}/...` **404s** on UCG/UDM.
- `unifi.py` `_stat()`/`_cmd()` try the proxied path first, fall back to legacy.
- Site id is `default` for a single-site console.

## 3. Endpoints (verified working)

| Purpose | Endpoint (under `/proxy/network/api/s/default/`) | Notes |
|---|---|---|
| Networks/VLANs list | `GET rest/networkconf` | `_id`, `name`, `vlan`, `ip_subnet`, `dhcpd_*` |
| Create network | `POST rest/networkconf` | payload below |
| WLAN (SSID) list | `GET rest/wlanconf` | `networkconf_id` = binding; **`wpa_psk` NOT returned** |
| WLAN create | `POST rest/wlanconf` | **requires `ap_group_ids`** (else `api.err.ApGroupMissing`) |
| WLAN rename/move | `PUT rest/wlanconf/{id}` | send the **full record** so the stored PSK survives |
| Port profiles | `GET rest/portconf` | native/tagged network ids |
| Switch ports | `GET stat/device` → `port_table` | `port_idx`, `native_networkconf_id`, `tagged_networkconf_id` |
| Client → port | `GET stat/sta` → `sw_mac` + `sw_port` | how we resolved 192.0.2.64 → Mini Rack Switch port 7 |
| Known clients | `GET rest/user` | **no `ip` field** — uses `last_ip` / `fixed_ip`; live IP only in `stat/sta` |
| Firewall groups | `GET/POST/PUT rest/firewallgroup` | only **`address-group`** accepted (see §5) |
| Firewall rules | `POST rest/firewallrule` | **BLOCKED** on this firmware — see §6 |
| Reboot device | `POST cmd/devmgr` `{"cmd":"restart","mac":…,"reboot_type":"soft"}` | |

### Verified payloads

Create network (works):
```json
{"name":"Management","purpose":"corporate","ip_subnet":"192.168.8.1/24",
 "vlan_enabled":true,"vlan":8,"dhcpd_enabled":true,
 "dhcpd_start":"192.168.8.6","dhcpd_stop":"192.168.8.254"}
```

Create WLAN (needs `ap_group_ids` from any existing WLAN):
```json
{"name":"RCTF-Guest","enabled":false,"security":"wpapsk","wpa_mode":"wpa2",
 "networkconf_id":"<guest-net-id>","is_guest":true,
 "ap_group_ids":["<existing-ap-group-id>"]}
```

Rename WLAN safely (keeps PSK): GET `/rest/wlanconf/{id}`, change `name`/
`networkconf_id` in the returned record, PUT the whole record back.

Create address group (works):
```json
{"name":"bareno-admin-terminals","group_type":"address-group","group_members":[]}
```

## 4. Client → switch port resolution

`stat/sta` entries for wired clients carry `sw_mac` (switch MAC) + `sw_port`
(port index) + `network_id`/`vlan`. This is how BareNOC answered "which port
faces 192.0.2.64" → Mini Rack Switch (`aa:bb:cc:dd:ee:ff`) **port 7**.

## 5. Firewall groups — type gotcha

- `group_type:"mac-group"` → **`api.err.InvalidValue`** (not supported on this
  firmware). Only **`address-group`** (IP/CIDR) is accepted.
- Consequence: per-device (roaming-admin) allowlisting should use the firewall
  **rule's `src_mac_addresses` field** (MAC source on the rule), not a MAC group.
  The UniFi UI supports Source → Advanced → MAC address.

## 6. ⚠️ Firewall rule creation — KNOWN BLOCKER (UniFi OS 4.x / Network 9.x)

`POST /rest/firewallrule` (v1) rejects creates. What we learned (verified 2026-08-02):

- **Schema (confirmed working past field validation)** — singular names + types +
  STRING rule_index:
  ```json
  {"name":"…","enabled":true,"action":"accept","ruleset":"LAN_IN",
   "rule_index":"2001","protocol":"all","protocol_match_excepted":false,
   "logging":false,"state_established":false,"state_invalid":false,
   "state_new":true,"state_related":false,
   "src_networkconf_id":"<net-id>","src_networkconf_type":"NETv4",
   "dst_networkconf_id":"<net-id>","dst_networkconf_type":"NETv4",
   "src_firewallgroup_ids":[],"dst_firewallgroup_ids":[],
   "src_mac_address":"","src_address":"","dst_address":""}
  ```
  The plural arrays (`src_networkconf_ids`) are NOT recognized →
  `api.err.FirewallRuleFieldsRequired`. `src_mac_address` is a single MAC string.
- **`rule_index`:** required; must be a STRING in "2000"–"2999" or "4000"–"4999"
  (int → `api.err.InvalidValue`; out-of-format → InvalidValue). **BUT every value
  in those bands returns `api.err.FirewallRuleIndexOutOfRange` on this controller
  (0 custom rules, all rulesets)** — the free-slot computation is opaque.
- **v2:** `GET /proxy/network/v2/api/site/default/firewall-policies` → 200 `[]`
  (live, modern Zone-Based Policy API); `GET …/firewall/zone` → 200 `[]`.
  POST schema not yet verified (zones are empty).
- **✅ READ path (verified):** `GET …/firewall-rules/combined-traffic-firewall-rules`
  returns ALL rules (custom + predefined). Custom rules: `origin_type:
  "traffic_rule"`, fields `target_devices` (source networks), `network_ids`
  (destinations), `traffic_direction:"TO"`, `traffic_rule_action:"BLOCK"/…`,
  `matching_target:"LOCAL_NETWORK"`, auto-assigned `firewall_rule_details`
  (rule_index 10001+). Predefined rules: `origin_type:"predefined_firewall_rule"`.
  Implemented in `UniFiClient.get_firewall_rules()` / `get_custom_firewall_rules()`.
- **Create path (partially cracked):** `POST …/firewall-policies` accepts
  `name`, `action` (`BLOCK`), `ip_version` (enum **`IPV4`/`IPV6`/`BOTH`),
  `schedule` (`{"mode":"ALWAYS"}`), `source.zone_id`, `destination.zone_id`
  — but zones must EXIST first (`api.err.FirewallPolicyZoneDoesNotExist`) and
  `/firewall/zone` is empty; the zone create schema is NOT yet known (rejects
  `zone_type`).
- **Workaround (current):** create rules in the UniFi UI (Settings → Firewall &
  Security → Rules). UI-created rules read back via the combined-traffic
  endpoint above.
- **TODO:** capture the UI's zone-create + policy-create POST payloads (browser
  devtools) to automate. This is the top item for BareNOC to automate firewall
  lockdown for deployments.

## 7. Device type quirk

UCG/UDM/UXG all report `type:"udm"` in `stat/device` (even though the model is
`UCGMAX`). `unifi.py::_map_type` maps `udm` → `gateway`.

## 8. Ops lessons (real network)

- **SSID name collisions** (legacy AP broadcasting the same name) shadow UniFi
  APs — verify with an over-the-air scan (`nmcli dev wifi list`), not just the
  controller.
- **Renaming an SSID drops its clients** (saved networks are keyed by name).
  Keep passwords identical to make rejoin painless.
- **Rename/move operations**: do additive work first (new networks/SSIDs), moves
  second, and the disruptive rename LAST.
- `rest/user` client records group poorly by `network_id`; use `stat/sta`
  (active) + IP-octet heuristics for placement questions.

## Reusable tooling in the repo

- `src/api/unifi.py` — `UniFiClient` with auth (session + API key), `_stat`/`_cmd`
  proxied helpers, `get_networks_map`, `get_switch_ports`, `find_client_port`,
  `get_port_profiles`, `set_port_vlans`, `restart_device`, `create_network`,
  firewall-group CRUD.
- `src/scripts/unifi_port.sh` — approved-agent-action for port VLAN assignment
  (delegates to the BareNOC API).
- `src/api/routes/unifi_sync.py` — `/api/v1/unifi/{config,test,sync,networks,
  client/{ip}/port,ports/{mac},ports/{mac}/{idx}/vlans}`.

## Pocket ID notes (2026-08-02)
- Pocket ID v2.12 reads **`APP_URL`** (NOT `PUBLIC_APP_URL` — that env var has zero hits in the binary). Without `APP_URL`, the issuer/RP defaults to `http://localhost:1411` and the setup wizard's passkey step fails with **"configured domain is invalid"** — even though the app is reachable at the real URL. Fix: set `APP_URL=https://<host>:8443` on the container (compose `environment:` block).
- Also observed: `INTERNAL_APP_URL` (internal listen, defaults fine), `UNIX_SOCKET_MODE` are the other URL-ish env vars in v2.12.
- Wizard stores no URL in the DB (kv table has only instance_id + jwt key) — the domain comes from APP_URL at runtime. Verified fix: issuer went localhost:1411 → https://192.0.2.207:8443 after setting APP_URL.
