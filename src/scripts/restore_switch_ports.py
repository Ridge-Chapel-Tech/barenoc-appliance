#!/usr/bin/env python3
"""EMERGENCY RESTORE — Mini Rack Switch port_overrides.

A test write replaced the switch's port_overrides with a single port, wiping
the others (the VM's port 6 lost its native VLAN). This restores the EXACT
original overrides captured before the write.

Usage: python3 restore_ports.py <unifi_user> <unifi_password>
The UniFi controller URL comes from UNIFI_URL (default https://192.0.2.1:443).
"""

import os
import sys
import json
import ssl
import urllib.request
import urllib.error
import http.cookiejar

BASE = os.getenv("UNIFI_URL", "https://192.0.2.1:443")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=CTX),
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

sw = os.getenv("SWITCH_MAC", "aa:bb:cc:dd:ee:01")
site = "default"

# EXACT original port_overrides captured 2026-08-04 16:26 before the write
ORIGINAL = [
    {"port_idx": 1, "setting_preference": "auto", "port_security_enabled": False,
     "poe_mode": "auto",
     "excluded_networkconf_ids": ["68236f14ef9e8b5d378f0971", "68237175ef9e8b5d378f0991",
                                   "687a4f676745ce7a11003cdc", "6a6f1946f2e7665abf7a372f",
                                   "6a6f1966f2e7665abf7a3743", "6a6f1967f2e7665abf7a3749"],
     "forward": "customize", "native_networkconf_id": "6823709cef9e8b5d378f0980",
     "name": "Port 1", "port_security_mac_address": [], "tagged_vlan_mgmt": "custom"},
    {"port_idx": 2, "setting_preference": "auto", "port_security_enabled": False,
     "voice_networkconf_id": "", "poe_mode": "auto", "forward": "native",
     "native_networkconf_id": "6823709cef9e8b5d378f0980", "name": "Port 2",
     "port_security_mac_address": [], "tagged_vlan_mgmt": "block_all"},
    {"port_idx": 4, "setting_preference": "auto", "port_security_enabled": False,
     "poe_mode": "auto", "forward": "all", "native_networkconf_id": "67fab14e4ea32d49b5843f85",
     "name": "Port 4", "port_security_mac_address": [], "tagged_vlan_mgmt": "auto"},
    {"port_idx": 5, "setting_preference": "auto", "port_security_enabled": False,
     "voice_networkconf_id": "", "forward": "native",
     "native_networkconf_id": "687a4f676745ce7a11003cdc", "name": "Port 5",
     "port_security_mac_address": [], "tagged_vlan_mgmt": "block_all"},
    {"port_idx": 6, "setting_preference": "auto", "port_security_enabled": False,
     "voice_networkconf_id": "", "forward": "native",
     "native_networkconf_id": "68236f14ef9e8b5d378f0971", "name": "Port 6",
     "port_security_mac_address": [], "tagged_vlan_mgmt": "block_all"},
    {"port_idx": 7, "setting_preference": "auto", "port_security_enabled": False,
     "voice_networkconf_id": "", "forward": "native",
     "native_networkconf_id": "68236f14ef9e8b5d378f0971", "name": "Port 7",
     "port_security_mac_address": [], "tagged_vlan_mgmt": "block_all"},
]


CSRF = {}


def req(method, path, data=None):
    headers = {"Content-Type": "application/json"}
    if CSRF.get("token"):
        headers["X-CSRF-Token"] = CSRF["token"]
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        resp = OPENER.open(r, timeout=15)
        t = resp.headers.get("X-CSRF-Token")
        if t:
            CSRF["token"] = t
        return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        t = e.headers.get("X-CSRF-Token") if e.headers else None
        if t:
            CSRF["token"] = t
        return None, f"HTTP {e.code}: {e.read().decode()[:200]}"


def main():
    if len(sys.argv) != 3:
        print("usage: restore_ports.py <unifi_user> <unifi_password>")
        sys.exit(1)
    user, pw = sys.argv[1], sys.argv[2]
    login, err = req("POST", "/api/auth/login",
                     {"username": user, "password": pw, "rememberMe": False,
                      "unique_id": "restore"})
    if err or not login:
        print("LOGIN FAILED:", err)
        sys.exit(1)
    # grab the device _id
    rd, err = req("GET", f"/proxy/network/api/s/{site}/stat/device")
    dev = next((d for d in (rd or {}).get("data", []) if (d.get("mac") or "").lower() == sw), None)
    if not dev:
        print("device not found")
        sys.exit(1)
    did = dev.get("_id")
    print("device:", dev.get("name"), "| _id:", did)
    res, err = req("PUT", f"/proxy/network/api/s/{site}/rest/device/{did}",
                   {"port_overrides": ORIGINAL})
    print("RESTORE ->", "OK" if res and res.get("meta", {}).get("rc") == "ok" else (err or res))
    if res and res.get("meta", {}).get("rc") == "ok":
        print("port overrides restored. Waiting for the switch to reapply (~30s)...")
    else:
        print("RESTORE FAILED — please restore in the UniFi UI instead: "
              "Mini Rack Switch -> Ports, set ports 2,5,6,7 native to the network "
              "matching their device (check each port's device), port 1 native WiFi "
              "+ excluded list, port 4 forward all.")


if __name__ == "__main__":
    main()
