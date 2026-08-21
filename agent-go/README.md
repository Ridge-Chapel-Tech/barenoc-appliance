# NOC_Agent — Linux endpoint agent (P1b: adoption + job loop)

The **NOC_Agent** is the BareNOC stateful endpoint daemon (design:
`NOC_AGENT_DESIGN.md` at the repo root). It runs on managed Linux devices,
dials **out** to the appliance over the existing mTLS channel, self-reports
host facts, and executes a small set of safe, capability-gated actions the
appliance pushes to it — **no inbound SSH, no stored credentials**.

This directory is a **separate Go module** (stdlib + one pure-Go SQLite
driver, `modernc.org/sqlite` — no cgo). It is the *agent side* of the
feature; the appliance side (`src/api`) is in the same repo.

## P1b scope

- **One-command Linux install** (`scripts/agent_install.sh`): takes the
  appliance URL + an enrollment token, bootstraps the CA by fingerprint
  (mirrors `src/scripts/enroll_device.sh`), enrolls `device-<name>` to
  `/opt/noc-agent/certs/`, writes `config.json`, installs `noc-agent.service`
  (systemd, `User=nocagent`), grants the capability-gated sudoers set, and
  starts it. The first report auto-claims the device with `method="agent"`.
- **Job loop** (`internal/jobs`): after each report the agent POSTs
  `jobs/pull`; every job is validated against the embedded catalog (only the
  P1b action set), executed unprivileged (escalating **only** via the
  installed sudoers full paths), and its result POSTed back with the nonce.
- **Local SQLite state** (`internal/state`): a completed-jobs ledger keyed by
  `(job_id, nonce)` (re-pulled jobs never re-execute) + an offline buffer
  (jobs pulled while offline complete-or-fail within their deadline on
  reconnect; a computed result is re-POSTed, never re-run).

### P1b action set (design §6)

| Action | What it runs | Escalation |
|---|---|---|
| `collect_logs` | `journalctl --no-pager -n <lines>` (fallback `tail`) | sudo, full path |
| `reboot` | `/sbin/reboot` — **only when `params.confirm == true`** | sudo, full path |
| `check_updates` | `/opt/noc-agent/scripts/check_updates.sh` — multi-source read-only check (OS package manager + flatpak + firmware + snap + rpm-ostree) | script self-escalates via the scoped sudoers |
| `apply_updates` | `/opt/noc-agent/scripts/apply_updates.sh` — **only when `params.confirm == true`**; re-runs the check and applies each non-zero source (same multi-source family). Never reboots — surfaces `reboot_needed` only | script self-escalates via the scoped sudoers |
| `report_facts` | local `facts.Collect()` (no command) | none |

The sudoers grant (installed by `agent_install.sh`, mirrored in
`internal/sudoers` — the unit test pins the exact line):

```
nocagent ALL=(root) NOPASSWD: /usr/bin/systemctl status *, /usr/bin/tail *, /usr/bin/journalctl *, /sbin/reboot, /usr/bin/apt, /usr/bin/apt-get, /usr/bin/dnf, /usr/bin/yum, /usr/bin/apk, /usr/bin/zypper, /usr/bin/flatpak, /usr/bin/fwupdmgr, /usr/bin/snap, /usr/bin/rpm-ostree
```

## Layout

```
agent-go/
├── go.mod                          module github.com/Ridge-Chapel-Tech/BareNOC/agent-go
├── cmd/noc-agent/main.go           entry point: config → report loop + job loop
├── internal/
│   ├── config/                     JSON config load + validation
│   ├── version/                    agent_version
│   ├── facts/                      Linux host fact collectors
│   ├── transport/                  mTLS HTTP client + report/jobs payloads
│   ├── report/                     single-shot heartbeat (interleaved with jobs)
│   ├── actions/                    embedded action catalog (P1b set) + validation
│   ├── sudoers/                    capability-gated sudoers generation
│   ├── state/                      local SQLite ledger + offline job buffer
│   └── jobs/                       pull → validate → execute → result loop
└── scripts/
    ├── agent_install.sh            ONE-COMMAND install + step-ca enroll (P1b)
    └── install_linux.sh            minimal binary-only install (P1a)
```

## Build & test

Go 1.26+ (the dev box has it at `~/.local/share/go/go/bin`):

```bash
export PATH="$HOME/.local/share/go/go/bin:$PATH"
cd agent-go
go build ./...
go vet ./...
go test ./...
# build the binary
go build -o noc-agent ./cmd/noc-agent
```

## Install (Linux, one command)

1. Build the binary (above), or have it staged somewhere the installer can
   find it.
2. Get a one-time enrollment token from the appliance:
   - `POST /api/v1/devices/<id>/adopt/cert` (admin/operator), or
   - `https://<appliance>/onboard/token?cn=device-<hostname>` (public).
3. Run the installer **as root**:

```bash
sudo bash agent-go/scripts/agent_install.sh https://<appliance> <enrollment-token> agent-go/noc-agent
```

What it does: fetches step-cli + the CA root + the CA fingerprint from the
appliance (no internet, no external trust), adds the `stepca.barenoc.local`
hosts mapping, bootstraps the CA by fingerprint, enrolls
`/opt/noc-agent/certs/noc-agent.{crt,key}` (key 0600), writes
`/opt/noc-agent/config.json` (appliance_url, cn, cert paths, poll 30s,
state_db), installs + enables `noc-agent.service`, and grants the
capability-gated sudoers set above. The first report links the device with
`adoption_method="agent"`.

Watch it:

```bash
journalctl -u noc-agent.service -f
```

## Configure

`config.json` (default path `/opt/noc-agent/config.json`):

```json
{
  "appliance_url": "https://appliance.barenoc.example",
  "cn": "device-mybox",
  "cert_file": "/opt/noc-agent/certs/noc-agent.crt",
  "key_file": "/opt/noc-agent/certs/noc-agent.key",
  "ca_file": "/opt/noc-agent/certs/ca.crt",
  "state_db": "/opt/noc-agent/state/noc-agent.db",
  "poll_interval": "30s",
  "log_level": "info"
}
```

Validation is strict-ish: `appliance_url` must be absolute `https`, `cn` and
the cert/CA/state paths must be set, `poll_interval` must parse as a positive
duration, `log_level` ∈ {debug, info, warn, error}.

> **Config format note (TOML swap):** the design §10 names `config.toml`;
> we deliberately use JSON to stay stdlib-only. The swap to TOML is a deferred
> follow-up — the field names are the same.

## Deferred (NOT in P1b)

Per the design milestones, these are out of scope for this slice and owned by
later workers:

- **Update channel** (design §9: appliance-served binaries + manifest,
  sha256 verify, self-swap) — P3.
- **macOS / Windows agents** + MSI/launchd/service (design §10) — P3.
- `install_chat_client` and the broader action catalog — P2/P3.
- **Appliance-initiated immediacy** — poll latency (≤30s) is the v1 answer
  (design §13 Q8); no push/WebSocket yet.
- Server-side re-queue of jobs stuck `running` past deadline (the agent
  completes-or-fails them itself; a mid-flight agent crash leaves the row
  `running` until a future re-queue policy lands) — P2.

## Security notes

- mTLS only; the appliance CA (`ca_file`) is the sole trust anchor
  (`InsecureSkipVerify` stays false).
- Runs unprivileged (`nocagent`); systemd hardening `NoNewPrivileges`,
  `ProtectSystem=strict`, `ReadWritePaths=/opt/noc-agent`.
- Escalation only via the five full-path sudoers commands; the embedded
  catalog re-validates every job before it runs (both-side allowlist).
- No plaintext credentials; identity is the short-lived device cert.
- A revoked device is rejected (403) and the agent stops working (design §4).
