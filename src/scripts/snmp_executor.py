#!/usr/bin/env python3
"""SNMP executor skeleton — poll (GET) / set (SET) for SNMP-only devices.

This is the thin host-side executor for the `snmp` control channel
(device_adoption_model.md §3/§6). It wraps snmpget/snmpset (v2c + v3) and
speaks JSON, matching the runner's executor contract. It is NOT yet wired into
the action catalog — completing it (v3 auth+priv param plumbing + routing
snmp_poll/reboot_device for SNMP-only gear) is a documented follow-up.

Security (design §4): v3 auth+priv is the supported secure mode; v2c
(plaintext community) is accepted for polling but the caller MUST pass the
plaintext warning through (it is never auto-selected by the fingerprint
recommendation).

Usage (stdin JSON, stdout JSON):
  echo '{"op":"get","target":"10.0.4.20","version":"2c","community":"public",
         "oid":"1.3.6.1.2.1.1.1.0"}' | python3 snmp_executor.py

v3 example:
  {"op":"get","target":"10.0.4.20","version":"3",
   "user":"noc","auth_proto":"SHA","auth_pass":"...",
   "priv_proto":"AES","priv_pass":"...","oid":"1.3.6.1.2.1.1.1.0"}
"""

import json
import shutil
import subprocess
import sys

OPS = ("get", "set")


def _bin(name: str) -> bool:
    return shutil.which(name) is not None


def _run(args: list, timeout: int = 6) -> dict:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out = r.stdout.strip()
        return {
            "ok": r.returncode == 0 and bool(out),
            "output": out,
            "error": r.stderr.strip() if r.stderr and r.returncode != 0 else None,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"ok": False, "output": "", "error": str(e)}


def snmp_get(target: str, oid: str, version: str = "2c", **kw) -> dict:
    if not _bin("snmpget"):
        return {"ok": False, "error": "snmpget not installed"}
    args = ["snmpget", f"-v{version}"]
    if version == "3":
        args += ["-u", kw.get("user", "noc"),
                 "-a", kw.get("auth_proto", "SHA"),
                 "-A", kw.get("auth_pass", ""),
                 "-x", kw.get("priv_proto", "AES"),
                 "-X", kw.get("priv_pass", ""),
                 "-l", "authPriv"]
    else:
        args += ["-c", kw.get("community", "public")]
    args += ["-t", "3", "-r", "1", target, oid]
    return _run(args)


def snmp_set(target: str, oid: str, value: str, value_type: str = "i",
             version: str = "2c", **kw) -> dict:
    if not _bin("snmpset"):
        return {"ok": False, "error": "snmpset not installed"}
    args = ["snmpset", f"-v{version}"]
    if version == "3":
        args += ["-u", kw.get("user", "noc"),
                 "-a", kw.get("auth_proto", "SHA"),
                 "-A", kw.get("auth_pass", ""),
                 "-x", kw.get("priv_proto", "AES"),
                 "-X", kw.get("priv_pass", ""),
                 "-l", "authPriv"]
    else:
        args += ["-c", kw.get("community", "public")]
    args += ["-t", "3", "-r", "1", target, oid, value_type, value]
    return _run(args)


def execute(job: dict) -> dict:
    """Executor entry point: {op, target, oid, value?, version?, ...} -> JSON."""
    op = (job or {}).get("op", "get")
    target = (job or {}).get("target", "")
    oid = (job or {}).get("oid", "")
    if not target or not oid:
        return {"ok": False, "error": "snmp_executor requires target + oid"}
    if op not in OPS:
        return {"ok": False, "error": f"unknown op {op!r} (use get|set)"}
    if op == "set":
        return snmp_set(target, oid, str(job.get("value", "")),
                        str(job.get("value_type", "i")),
                        str(job.get("version", "2c")), **job)
    return snmp_get(target, oid, str(job.get("version", "2c")), **job)


if __name__ == "__main__":
    try:
        job = json.loads(sys.stdin.read())
    except Exception:
        job = {}
    print(json.dumps(execute(job)))
