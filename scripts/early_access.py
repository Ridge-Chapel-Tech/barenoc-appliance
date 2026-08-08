#!/usr/bin/env python3
"""early_access.py — manage paid/free access to the private early-access repo.

Vendor (maintainer) tool; run on a machine with `gh` authenticated.
State lives in scripts/.early-access-state.json (gitignored — usernames, keys
and emails never enter the repos).

Access model:
  - collaborators (repo access, per person) AND
  - activation keys (update entitlement, per install, bound to the purchase
    email). Keys are checked against the PUBLIC allowlist at
    https://barenoc.com/downloads/activation-keys.json (keys + hashed emails +
    active flag). Revocation = drop/deactivate the key in that list.

Usage:
  python3 scripts/early_access.py grant <user> <email> [--free|--months N]  collaborator + key
  python3 scripts/early_access.py issue-key <user> <email> [--months N]      key only
  python3 scripts/early_access.py revoke <user>                             collaborator + key
  python3 scripts/early_access.py revoke-key <user>                         key only
  python3 scripts/early_access.py mark-paid <user> [--months N]             extend due (webhook)
  python3 scripts/early_access.py free <user> | unfree <user>               toggle free slot
  python3 scripts/early_access.py list                                      collaborators + keys + state
  python3 scripts/early_access.py check                                     monthly sweep (collaborators + keys)
  python3 scripts/early_access.py publish-keys [--no-push]                  regen + push allowlist
  python3 scripts/early_access.py install-timer                             systemd user timer for `check`
"""
import datetime
import hashlib
import json
import os
import pathlib
import random
import subprocess
import sys

REPO = os.environ.get("REPO", "Ridge-Chapel-Tech/barenoc-appliance")
WEBSITE_REPO = os.environ.get("WEBSITE_REPO", "Ridge-Chapel-Tech/BareNOC-Website")
DIR = pathlib.Path(__file__).resolve().parent
STATE = pathlib.Path(os.environ.get("STATE", DIR / ".early-access-state.json"))
FREE_SLOTS = int(os.environ.get("FREE_SLOTS", "2"))
DEFAULT_MONTHS = int(os.environ.get("DEFAULT_MONTHS", "1"))
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/l


def gh(args, check=True):
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"gh api {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def owner():
    return gh(["user", "--jq", ".login"])


def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"users": {}}


def save(d):
    STATE.write_text(json.dumps(d, indent=2))


def due_from_months(n):
    return (datetime.date.today() + datetime.timedelta(days=30 * n)).isoformat()


def gen_key():
    return "BARC-" + "-".join(
        "".join(random.choice(KEY_ALPHABET) for _ in range(4)) for _ in range(4))


def email_hash(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def collaborator_names():
    out = gh([f"repos/{REPO}/collaborators", "--paginate", "--jq", ".[].login"])
    return set(l for l in out.splitlines() if l.strip())


def grant(user, email, free=False, months=DEFAULT_MONTHS):
    gh(["-X", "PUT", f"repos/{REPO}/collaborators/{user}", "-f", "permission=read"])
    d = load()
    users = d["users"]
    if free:
        nfree = sum(1 for u in users.values() if u.get("free"))
        if user not in users and nfree >= FREE_SLOTS:
            sys.exit(f"free slots full ({FREE_SLOTS}) — grant paid (--months N) or unfree someone")
    key = gen_key()
    users[user] = {"free": bool(free),
                   "due": None if free else due_from_months(months),
                   "months": months,
                   "key": key, "email": email.strip().lower(),
                   "key_active": True}
    save(d)
    publish_keys()
    print(f"granted {user} ({'free' if free else 'paid, due ' + users[user]['due']})")
    print(f"  activation key: {key}   (bound to {email.strip().lower()})")


def issue_key(user, email, months=DEFAULT_MONTHS):
    d = load()
    u = d["users"].get(user)
    if not u:
        sys.exit(f"{user} has no record — grant them first")
    u["key"] = gen_key()
    u["email"] = email.strip().lower()
    u["key_active"] = True
    save(d)
    publish_keys()
    print(f"issued new key for {user}: {u['key']}   (bound to {u['email']})")


def revoke(user):
    gh(["-X", "DELETE", f"repos/{REPO}/collaborators/{user}"])
    d = load()
    if user in d["users"]:
        d["users"][user]["key_active"] = False
    save(d)
    publish_keys()
    print(f"revoked {user} (collaborator + activation key)")


def revoke_key(user):
    d = load()
    u = d["users"].get(user)
    if not u:
        sys.exit(f"{user} has no record")
    u["key_active"] = False
    save(d)
    publish_keys()
    print(f"revoked activation key for {user}")


def mark_paid(user, months=DEFAULT_MONTHS):
    d = load()
    u = d["users"].get(user)
    if not u:
        sys.exit(f"{user} has no record — grant them first")
    u["free"] = False
    u["due"] = due_from_months(months)
    u["months"] = months
    save(d)
    print(f"{user} marked paid — due {u['due']}")


def toggle_free(user, free):
    d = load()
    u = d["users"].get(user)
    if not u:
        sys.exit(f"{user} has no record — grant them first")
    if free:
        nfree = sum(1 for x in d["users"].values() if x.get("free"))
        if not u.get("free") and nfree >= FREE_SLOTS:
            sys.exit(f"free slots full ({FREE_SLOTS})")
    u["free"] = free
    if free:
        u["due"] = None
    save(d)
    print(f"{user} is now {'free (never auto-revoked)' if free else 'paid'}")


def publish_keys(no_push=False):
    """Regenerate downloads/activation-keys.json in the website repo (public
    allowlist: key + email hash + active). Pushes via the website repo's
    push-to-deploy when not --no-push."""
    d = load()
    keys = []
    for name, u in d["users"].items():
        if not u.get("key"):
            continue
        keys.append({"key": u["key"],
                     "email_hash": email_hash(u.get("email") or ""),
                     "issued": datetime.date.today().isoformat(),
                     "active": bool(u.get("key_active", True))})
    allowlist = {"schema": 1, "updated": datetime.datetime.utcnow().isoformat() + "Z",
                 "keys": keys}
    tmp = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "bareNOC-site"
    subprocess.run(["rm", "-rf", str(tmp)], check=False)
    subprocess.run(["git", "clone", "-q", f"https://github.com/{WEBSITE_REPO}.git", str(tmp)],
                   check=True)
    dl = tmp / "downloads"
    dl.mkdir(exist_ok=True)
    (dl / "activation-keys.json").write_text(json.dumps(allowlist, indent=2) + "\n")
    if no_push:
        print(f"allowlist written (no push): {dl / 'activation-keys.json'}")
        print(json.dumps(allowlist, indent=1)[:400])
        return
    subprocess.run(["git", "-C", str(tmp), "add", "downloads/activation-keys.json"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-q",
                    "-m", "activation keys: update allowlist"], check=True)
    subprocess.run(["git", "-C", str(tmp), "push", "-q"], check=True)
    print(f"published allowlist -> https://barenoc.com/downloads/activation-keys.json "
          f"({len(keys)} keys)")


def list_all():
    own = owner()
    names = collaborator_names()
    d = load()
    users = d["users"]
    print(f"repo: {REPO}   owner: {own}   free slots: {FREE_SLOTS}")
    print(f"{'user':<22}{'free':<6}{'due':<12}{'key':<22}access")
    for name in sorted(set(users) | names):
        u = users.get(name, {})
        key = u.get("key") or "-"
        ka = "" if not u.get("key") else ("·active" if u.get("key_active") else "·REVOKED")
        acc = "collab" if name in names else "key-only"
        print(f"{name:<22}{'F' if u.get('free') else '-':<6}{str(u.get('due') or '-'):<12}"
              f"{key:<22}{acc} {ka}")


def check():
    own = owner()
    d = load()
    t = datetime.date.today().isoformat()
    revoked = []
    for name, u in d["users"].items():
        if name == own or u.get("free"):
            continue
        due = u.get("due")
        if due and due < t:
            gh(["-X", "DELETE", f"repos/{REPO}/collaborators/{name}"])
            u["key_active"] = False
            revoked.append(name)
    if revoked:
        save(d)
        publish_keys()
        print(f"[{t}] revoked past-due: {', '.join(revoked)} (collaborators + keys)")
    else:
        print(f"[{t}] no past-due non-free users to revoke")


def install_timer():
    unit_dir = pathlib.Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    script = pathlib.Path(__file__).resolve()
    (unit_dir / "early-access-check.service").write_text(
        f"""[Unit]
Description=Early-access monthly payment check

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 {script} check
""")
    (unit_dir / "early-access-check.timer").write_text(
        """[Unit]
Description=Early-access monthly payment check (1st of month)

[Timer]
OnCalendar=*-*-01 06:00:00
Persistent=true

[Install]
WantedBy=timers.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now",
                    "early-access-check.timer"], check=True)
    print("installed user timer: early-access-check.timer (runs `check` on the 1st, Persistent=true)")


def _months_from(args):
    if "--months" in args:
        return int(args[args.index("--months") + 1])
    return DEFAULT_MONTHS


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "grant":
        if len(args) < 1:
            sys.exit("usage: grant <user> <email> [--free|--months N]")
        email = args[1] if len(args) > 1 and not args[1].startswith("--") else ""
        if not email:
            sys.exit("email required — grant <user> <purchase-email> (the key is bound to it)")
        grant(args[0], email, free="--free" in args, months=_months_from(args))
    elif cmd == "issue-key":
        if len(args) < 2:
            sys.exit("usage: issue-key <user> <email> [--months N]")
        issue_key(args[0], args[1], months=_months_from(args))
    elif cmd == "revoke":
        if not args:
            sys.exit("usage: revoke <user>")
        revoke(args[0])
    elif cmd == "revoke-key":
        if not args:
            sys.exit("usage: revoke-key <user>")
        revoke_key(args[0])
    elif cmd == "mark-paid":
        if not args:
            sys.exit("usage: mark-paid <user> [--months N]")
        mark_paid(args[0], _months_from(args))
    elif cmd in ("free", "unfree"):
        if not args:
            sys.exit(f"usage: {cmd} <user>")
        toggle_free(args[0], cmd == "free")
    elif cmd == "list":
        list_all()
    elif cmd == "check":
        check()
    elif cmd == "publish-keys":
        publish_keys(no_push="--no-push" in args)
    elif cmd == "install-timer":
        install_timer()
    else:
        sys.exit(f"unknown command: {cmd}\n\n{__doc__}")


if __name__ == "__main__":
    main()
