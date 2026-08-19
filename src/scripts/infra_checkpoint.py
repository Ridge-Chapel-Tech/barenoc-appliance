#!/usr/bin/env python3
"""Infra-change checkpoint helper (agent-foresight — capture-before / rollback).

Captures the FULL before-state of a UniFi switch's port table (native + tagged
assignments per port) before any port/VLAN change and restores it on rollback.
The agent's INFRA-CHANGE CONTRACT points at this script; the runner's timeout
watchdog surfaces the `restore` command so a mid-flight timeout is never a
half-applied mystery (the 08-19 incident).

Usage:
  python3 infra_checkpoint.py capture <switch_mac> [--checkpoint DIR]
        [--step N] [--total M]
      Read the complete port table and write DIR/checkpoint.json (or
      ./checkpoint.json) with the before-state + optional step progress.

  python3 infra_checkpoint.py restore --checkpoint <checkpoint.json path>
      Re-apply each captured port's native/tagged via the merge-safe appliance
      API and report the result.

Reads the agent service-account credentials (0600, pi-agent-owned), exactly
like the other unifi_*.sh scripts. Every write goes through the appliance API
(GET /api/v1/unifi/ports/{mac}, POST /api/v1/unifi/ports/{mac}/{port}/vlans)
which uses the merge-safe full-array path — this script never hand-writes a
partial port_overrides array (STANDING PROCEDURE #4).
"""

import argparse
import datetime
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

BASE_URL = "https://localhost"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def call(method, path, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE_URL + path, data=body, headers=headers,
                                 method=method)
    try:
        r = urllib.request.urlopen(req, timeout=20, context=CTX)
        return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode()).get("detail", str(e))
        except Exception:
            return None, str(e)
    except Exception as e:
        return None, str(e)


def load_creds():
    creds = {}
    try:
        with open("/opt/barenoc/agent/credentials") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    creds[k.strip()] = v.strip()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"cannot read agent credentials: {e}"}))
        sys.exit(1)
    if not creds.get("username") or not creds.get("password"):
        print(json.dumps({"ok": False, "error": "agent credentials file is incomplete"}))
        sys.exit(1)
    return creds


def login():
    creds = load_creds()
    data, err = call("POST", "/api/v1/auth/login",
                     {"username": creds["username"], "password": creds["password"]})
    if err or not data:
        print(json.dumps({"ok": False, "error": f"API login failed: {err}"}))
        sys.exit(1)
    return data.get("access_token", "")


def _port_state(p):
    """The restore-relevant before-state of one port: port_idx, name, and the
    native/tagged network IDs (kept as IDs so restore can pass them straight
    back to the merge-safe vlans endpoint)."""
    return {
        "port_idx": p.get("port_idx"),
        "name": p.get("name"),
        "native_network_id": p.get("native_network_id"),
        "tagged_network_ids": list(p.get("tagged_network_ids") or []),
    }


def capture(mac, checkpoint_dir, step, total):
    if not mac:
        print(json.dumps({"ok": False,
                          "error": "capture requires a switch MAC address"}))
        sys.exit(1)
    tok = login()
    data, err = call("GET", f"/api/v1/unifi/ports/{mac}", token=tok)
    if err or data is None:
        print(json.dumps({"ok": False, "error": f"could not read port table: {err}"}))
        sys.exit(1)
    ports = data.get("ports") or []
    state = {"switch_mac": mac, "ports": [_port_state(p) for p in ports]}
    out_dir = checkpoint_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    doc = {
        "captured_at": datetime.datetime.utcnow().isoformat() + "Z",
        "state": state,
    }
    if step is not None:
        doc["step"] = step
    if total is not None:
        doc["total"] = total
    path = os.path.join(out_dir, "checkpoint.json")
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    print(json.dumps({"ok": True, "checkpoint": path, "ports": len(ports),
                      "step": step, "total": total}))


def restore(checkpoint_path):
    if not checkpoint_path or not os.path.exists(checkpoint_path):
        print(json.dumps({"ok": False,
                          "error": f"checkpoint not found: {checkpoint_path}"}))
        sys.exit(1)
    try:
        with open(checkpoint_path) as f:
            doc = json.load(f)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"could not read checkpoint: {e}"}))
        sys.exit(1)
    state = (doc or {}).get("state") or {}
    mac = state.get("switch_mac")
    ports = state.get("ports") or []
    if not mac or not ports:
        print(json.dumps({"ok": False,
                          "error": "checkpoint has no switch_mac / ports"}))
        sys.exit(1)
    tok = login()
    results = []
    for p in ports:
        idx = p.get("port_idx")
        if idx is None:
            results.append({"port_idx": None, "ok": False,
                            "error": "port missing port_idx in checkpoint"})
            continue
        native = p.get("native_network_id")
        tagged = p.get("tagged_network_ids") or []
        body = {"tagged": tagged}
        if native:
            body["native"] = native
        res, err = call("POST", f"/api/v1/unifi/ports/{mac}/{idx}/vlans",
                        body, token=tok)
        if err:
            results.append({"port_idx": idx, "ok": False, "error": err})
        else:
            results.append({"port_idx": idx, "ok": True,
                            "after": (res or {}).get("after"),
                            "applied": (res or {}).get("applied")})
    ok = all(r.get("ok") for r in results)
    print(json.dumps({"ok": ok, "switch_mac": mac,
                      "restored": len(results),
                      "results": results}))
    if not ok:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Infra-change checkpoint/rollback")
    sub = ap.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="capture the before-state to a checkpoint")
    cap.add_argument("switch_mac")
    cap.add_argument("--checkpoint", default=None,
                     help="directory to write checkpoint.json into")
    cap.add_argument("--step", type=int, default=None)
    cap.add_argument("--total", type=int, default=None)

    res = sub.add_parser("restore", help="restore a captured before-state")
    res.add_argument("--checkpoint", required=True, help="path to checkpoint.json")

    args = ap.parse_args()
    if args.cmd == "capture":
        capture(args.switch_mac, args.checkpoint, args.step, args.total)
    elif args.cmd == "restore":
        restore(args.checkpoint)


if __name__ == "__main__":
    main()
