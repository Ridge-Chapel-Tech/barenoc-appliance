#!/usr/bin/env python3
"""early_access.py — manage paid/free access to the private early-access repo.

Vendor (maintainer) tool; run on a machine with `gh` authenticated.
State lives in scripts/.early-access-state.json (gitignored — usernames and
due dates never enter the repos).

Usage:
  python3 scripts/early_access.py grant <user> [--free|--months N]  invite collaborator (read)
  python3 scripts/early_access.py revoke <user>                     remove collaborator + record
  python3 scripts/early_access.py mark-paid <user> [--months N]     extend due (payment webhook target)
  python3 scripts/early_access.py free <user> | unfree <user>       toggle free slot (cap FREE_SLOTS)
  python3 scripts/early_access.py list                              collaborators + state
  python3 scripts/early_access.py check                             monthly sweep: revoke past-due non-free
  python3 scripts/early_access.py install-timer                     systemd user timer for `check` (1st)

`check` never touches the owner (the gh account running it) or free users.
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys

REPO = os.environ.get("REPO", "Ridge-Chapel-Tech/barenoc-appliance")
DIR = pathlib.Path(__file__).resolve().parent
STATE = pathlib.Path(os.environ.get("STATE", DIR / ".early-access-state.json"))
FREE_SLOTS = int(os.environ.get("FREE_SLOTS", "2"))
DEFAULT_MONTHS = int(os.environ.get("DEFAULT_MONTHS", "1"))


def gh(args):
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if r.returncode != 0:
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


def grant(user, free=False, months=DEFAULT_MONTHS):
    gh(["-X", "PUT", f"repos/{REPO}/collaborators/{user}", "-f", "permission=read"])
    d = load()
    users = d["users"]
    if free:
        nfree = sum(1 for u in users.values() if u.get("free"))
        if user not in users and nfree >= FREE_SLOTS:
            sys.exit(f"free slots full ({FREE_SLOTS}) — grant paid (--months N) or unfree someone")
    users[user] = {"free": bool(free),
                   "due": None if free else due_from_months(months),
                   "months": months}
    save(d)
    print(f"granted {user} ({'free slot' if free else 'paid, due ' + users[user]['due']})")


def revoke(user):
    gh(["-X", "DELETE", f"repos/{REPO}/collaborators/{user}"])
    d = load()
    d["users"].pop(user, None)
    save(d)
    print(f"revoked {user}")


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


def list_all():
    own = owner()
    rows = gh([f"repos/{REPO}/collaborators", "--paginate",
               "--jq", ".[] | .login + \" \" + (.permissions.pull|tostring)"])
    d = load()
    users = d["users"]
    print(f"repo: {REPO}   owner: {own}   free slots: {FREE_SLOTS}")
    print(f"{'user':<24}{'free':<6}{'due':<12}access")
    names = []
    for line in rows.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, pull = parts[0], parts[1]
        names.append(name)
        u = users.get(name, {})
        print(f"{name:<24}{'F' if u.get('free') else '-':<6}{str(u.get('due') or '-'):<12}{'read' if pull == 'true' else '?'}")
    for name, u in users.items():
        if name not in names:
            print(f"{name:<24}{'F' if u.get('free') else '-':<6}{str(u.get('due') or '-'):<12}not a collaborator")


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
            revoked.append(name)
    if revoked:
        for name in revoked:
            d["users"].pop(name, None)
        save(d)
        print(f"[{t}] revoked past-due: {', '.join(revoked)}")
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


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "grant":
        if not args:
            sys.exit("usage: grant <user> [--free|--months N]")
        free = "--free" in args
        months = DEFAULT_MONTHS
        if "--months" in args:
            months = int(args[args.index("--months") + 1])
        grant(args[0], free=free, months=months)
    elif cmd == "revoke":
        if not args:
            sys.exit("usage: revoke <user>")
        revoke(args[0])
    elif cmd == "mark-paid":
        if not args:
            sys.exit("usage: mark-paid <user> [--months N]")
        months = DEFAULT_MONTHS
        if "--months" in args:
            months = int(args[args.index("--months") + 1])
        mark_paid(args[0], months)
    elif cmd == "free":
        if not args:
            sys.exit("usage: free <user>")
        toggle_free(args[0], True)
    elif cmd == "unfree":
        if not args:
            sys.exit("usage: unfree <user>")
        toggle_free(args[0], False)
    elif cmd == "list":
        list_all()
    elif cmd == "check":
        check()
    elif cmd == "install-timer":
        install_timer()
    else:
        sys.exit(f"unknown command: {cmd}\n\n{__doc__}")


if __name__ == "__main__":
    main()
