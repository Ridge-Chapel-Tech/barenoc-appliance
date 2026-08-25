# BareNOC Documentation

**Version:** 2.0  
**Last Updated:** 2026-08-05

A portable, self-contained Network Operations Center that runs on a single Ubuntu Server deployed inside a client network. Designed for residential through 50-employee SMB environments.

---

## Documentation Tree

### Architecture & Design

| Document | Description |
|----------|-------------|
| [`01_pre_deployment_plan.md`](./01_pre_deployment_plan.md) | Architecture plan — VLAN scheme, system design, hardware sizing, security model |
| [`02_iac_and_setup_manifests.md`](./02_iac_and_setup_manifests.md) | **The install/ops reference** — real manifests (compose v3.8, per-service Dockerfiles, SQLAlchemy schema, pi-agent-runner, secrets layout), deployment checklist |
| [`03_post_deployment_runbook.md`](./03_post_deployment_runbook.md) | Post-deployment procedures — verification, backup rotation, troubleshooting |
| [`architecture/llm_providers.md`](./architecture/llm_providers.md) | LLM provider abstraction — adapters, price resolution, provider registry |
| [`architecture/pocket_id_oidc.md`](./architecture/pocket_id_oidc.md) | Pocket ID OIDC/passkey integration |
| [`architecture/device_adoption_pocketid.md`](./architecture/device_adoption_pocketid.md) | **Design: device adoption via Pocket ID** (passkey identity = gold standard; SSH = fallback); phases E–H |

### Appliance Build

| Document | Description |
|----------|-------------|
| [`appliance/hardware_bom.md`](./appliance/hardware_bom.md) | Bill of materials for the 10-inch rack appliance |
| [`appliance/hardware_sizing.md`](./appliance/hardware_sizing.md) | **Sizing matrix (S/M/L/XL)** — endpoint counts → vCPU/RAM/disk + appliance class + buying guide; feeds the installer's `--profile` flag |
| [`appliance/assembly_guide.md`](./appliance/assembly_guide.md) | Rack assembly, cabling, and labeling |
| [`appliance/proxmox_setup.md`](./appliance/proxmox_setup.md) | Proxmox VE installation and configuration |
| [`appliance/barenoc_vm_create.md`](./appliance/barenoc_vm_create.md) | Creating and provisioning the BareNOC VM |

### Installer & Distribution

| Document | Description |
|----------|-------------|
| [`../proxmox/barenoc-appliance.sh`](../proxmox/barenoc-appliance.sh) | **One-shot Proxmox installer** — `--ip` + `--profile` → ready appliance (cloud image + cloud-init + `deploy.sh`) |
| [`../proxmox/build_barenoc_iso.sh`](../proxmox/build_barenoc_iso.sh) | Builds the self-contained **BareNOC ISO** (Ubuntu 24.04 autoinstall + embedded app) |
| [`../install.sh`](../install.sh) | One-line bootstrap (`curl … | bash`) that pulls the released installer |
| [`../marketing/download_distribution.md`](../marketing/download_distribution.md) | Where downloads live (GitHub Releases + R2/B2 CDN + `versions.json` contract) |
| [`../proxmox/setup-usb-backup.sh`](../proxmox/setup-usb-backup.sh) | One-time LUKS setup for the USB backup stick (Layer 3) |

### Development

| Document | Description |
|----------|-------------|
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Commit format (Conventional Commits), branching, test gates, PR conventions |
| [`../CHANGELOG.md`](../CHANGELOG.md) | **Bug-fix + new-feature log** (Keep a Changelog, per release) |
| [`development/versioning.md`](./development/versioning.md) | CalVer scheme — `YYYY.MM` / `YYYY.MM.DD` / `YYYY.MM.DD.a`; single source of truth (`version.py`); release process (`bump_version.sh` + tag + workflow); bug/feature logging |
| [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) | Test gates on push/PR (`scripts/run_tests.sh`) |
| [`../.github/workflows/release.yml`](../.github/workflows/release.yml) | Tag `vX.Y.Z` → tests → GitHub Release + assets + `versions.json` |

### Operations

| Document | Description |
|----------|-------------|
| [`operations/update_pipeline.md`](./operations/update_pipeline.md) | Update and patching procedures for all layers |
| [`operations/backup_and_restore.md`](./operations/backup_and_restore.md) | Backup strategy, rotation, and disaster recovery |
| [`operations/unifi_api_runbook.md`](./operations/unifi_api_runbook.md) | UniFi controller API integration, sync, and write actions |
| [`operations/trial_lifecycle.md`](./operations/trial_lifecycle.md) | Build → ship → trial → recover lifecycle |

### Security

| Document | Description |
|----------|-------------|
| [`security/guardrails.md`](./security/guardrails.md) | Anti-abuse guardrails — prompt injection, job validation, action allowlist |
| [`security/access_control.md`](./security/access_control.md) | Authentication, authorization, role model, out-of-band access |
| [`security/audit_logging.md`](./security/audit_logging.md) | Hash-chained audit trail, log retention, incident response |
| [`security/release-signing.md`](./security/release-signing.md) | Detached-GPG release signing — verify-before-apply, key management |

### Customer-Facing

| Document | Description |
|----------|-------------|
| [`customer/quick_start_card.md`](./customer/quick_start_card.md) | One-page quick start shipped with the appliance |
| [`customer/admin_guide.md`](./customer/admin_guide.md) | Web UI guide, ticket management, report access |
| [`customer/compliance_controls.md`](./customer/compliance_controls.md) | Toggleable governance panel, Compliance baseline, attestation export — operator/auditor guide (v2026.08.25.b+) |
| [`customer/passkey_enrollment.md`](./customer/passkey_enrollment.md) | Pocket ID passkey enrollment for end users |

### Runbook (Internal)

| Document | Description |
|----------|-------------|
| [`runbook/troubleshooting.md`](./runbook/troubleshooting.md) | Common issues, diagnostics, recovery procedures |
| [`runbook/factory_reset.md`](./runbook/factory_reset.md) | Restoring appliance to factory state for re-shipment |
| [`runbook/remote_access.md`](./runbook/remote_access.md) | Tailscale, NanoKVM, and emergency access procedures |

### Test & Milestone Tracking

| Document | Description |
|----------|-------------|
| [`system_acceptance_test.md`](./system_acceptance_test.md) | Living SAT suite (gate before live deployment) |
| [`MILESTONES.md`](./MILESTONES.md) | Milestone/feature tracking |

---

## Quick Reference

```bash
# Deploy / update the VM (the actual installer — from the dev box)
./deploy.sh                        # defaults to barenoc@192.0.2.207

# SSH to the BareNOC VM
ssh barenoc@192.0.2.207

# Host-side agent runner (NOT in Docker — updated manually on runner.py changes)
systemctl status pi-agent-runner
journalctl -u pi-agent-runner -f

# Access the web UI
# https://192.0.2.207           (BareNOC)
# https://192.0.2.95:8006       (Proxmox)

# App-data backup / restore (see operations/backup_and_restore.md)
/opt/barenoc/scripts/backup_app.sh
/opt/barenoc/scripts/restore_app.sh --apply /opt/barenoc/backups/<archive>
```

> ⚠️ `docs/02_iac_and_setup_manifests.md` is the source of truth for what is
> actually deployed; the appliance build docs describe the hardware layer.
