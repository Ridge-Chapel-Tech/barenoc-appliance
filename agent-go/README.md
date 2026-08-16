# NOC_Agent — P1a (Linux-first agent skeleton + self-report)

The **NOC_Agent** is the BareNOC stateful endpoint daemon (design:
`NOC_AGENT_DESIGN.md` at the repo root). It runs on managed devices, dials
**out** to the appliance over the existing mTLS channel, and self-reports
host facts so an endpoint with a BareNOC CA cert can **auto-claim** with
`adoption_method="agent"`.

This directory is a **separate Go module** (stdlib only — zero external
dependencies). It is the *agent side* of the feature; the appliance side
(`src/api`) is in the same repo.

## P1a scope (what this does)

- Load a JSON config (`/opt/noc-agent/config.json` by default).
- Collect Linux host facts: OS (`/etc/os-release` ID+VERSION_ID), kernel
  (`uname -r`), hostname, non-loopback MACs + IPv4 addresses, uptime
  (`/proc/uptime`), free disk on `/` (`statfs`).
- POST them to `https://<appliance>/api/v1/device/report` over **mTLS**
  (client cert + key from config, CA from `ca_file`; `InsecureSkipVerify`
  stays `false`).
- Loop on `poll_interval` (default 30s); transient errors are logged and
  retried on the next cycle.

The report body carries `agent_version`, `agent_capabilities: ["report_facts"]`
and `adoption_method: "agent"` — the appliance links the device with
`method="agent"` on first report (design §4).

## Layout

```
agent-go/
├── go.mod                          module github.com/Ridge-Chapel-Tech/BareNOC/agent-go
├── cmd/noc-agent/main.go           entry point: load config, run report loop
├── internal/
│   ├── config/                     JSON config load + validation
│   ├── version/                    agent_version = "0.1.0-p1a"
│   ├── facts/                      Linux host fact collectors
│   ├── transport/                  mTLS HTTP client + report body shape
│   └── report/                     report loop (collect → POST → sleep)
└── scripts/install_linux.sh        systemd install (User=nocagent)
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

## Configure

`config.json` (default path `/opt/noc-agent/config.json`):

```json
{
  "appliance_url": "https://appliance.barenoc.example",
  "cn": "device-mybox",
  "cert_file": "/opt/noc-agent/certs/noc-agent.crt",
  "key_file": "/opt/noc-agent/certs/noc-agent.key",
  "ca_file": "/opt/noc-agent/certs/ca.crt",
  "poll_interval": "30s",
  "log_level": "info"
}
```

Validation is strict-ish: `appliance_url` must be absolute `https`, `cn` and
the three cert paths must be set, `poll_interval` must parse as a positive
duration, `log_level` ∈ {debug, info, warn, error}.

> **Config format note (TOML swap):** the design §10 names `config.toml`;
> P1a deliberately uses JSON to stay stdlib-only. The swap to TOML (with a
> TOML lib) is a deferred follow-up — the field names are the same.

## Install (Linux)

```bash
sudo ./scripts/install_linux.sh ./noc-agent
```

This creates `/opt/noc-agent/{config.json,certs,logs,state}`, a `nocagent`
system user, copies the binary, and installs + enables `noc-agent.service`
(systemd, `User=nocagent`, `After=network-online.target`).

> **Certificates are provisioned out-of-band for P1a.** step-ca enrollment
> (the `step ca certificate` one-liner) is a **P1b follow-up**. Drop
> `noc-agent.crt`, `noc-agent.key` (0600) and `ca.crt` into
> `/opt/noc-agent/certs/`, set `appliance_url` + `cn` in the config, then
> `systemctl start noc-agent.service`.

## Deferred (NOT in P1a)

Per the design milestones, these are out of scope for this slice and owned by
later workers:

- **Jobs pull/execute + result** (design §5) — P2.
- **SQLite local state / nonce idempotency / offline buffer** (design §8) — P2.
- **Update channel** (design §9) — P3.
- **macOS / Windows agents** + MSI/launchd/service (design §10) — P3.
- **Per-capability sudo** (`SUDO_SCOPED`) — P2 (only noted here; P1a is
  report-only, no privilege escalation of any kind).
- `install_chat_client` and the broader action catalog — P2/P3.

## Security notes

- mTLS only; the appliance CA (`ca_file`) is the sole trust anchor.
- Runs unprivileged (`nocagent`); systemd hardening `NoNewPrivileges`,
  `ProtectSystem=strict`, `ReadWritePaths=/opt/noc-agent`.
- No plaintext credentials; identity is the short-lived device cert.
