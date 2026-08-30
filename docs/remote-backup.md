# Offsite / Remote backup (Layer 4)

An **offsite backup** option in **Settings → Backups** with two flavors sharing
ONE transport:

1. **BareNOC-managed** (subscription entitlement) — the appliance encrypts its
   app-data archive and uploads it to the BareNOC backend (during beta: a
   MinIO/S3-compatible box the customer's OMV NAS runs for their own boxes).
2. **Bring your own (BYO)** — point the appliance at **any S3-compatible**
   endpoint (Cloudflare R2, Backblaze B2, MinIO, Synology C2, …). Works for
   everyone, free tier; data never touches BareNOC.

Both use the **same** client-side encryption + archive + upload layer — the
only difference is where the bytes land and who owns the credentials.

## Security model

- **Encryption first.** The appliance generates a per-install data-encryption
  key (DEK) and encrypts the archive **before** upload (AES-256-GCM). BareNOC
  never sees plaintext.
- **Recovery key shown once.** The DEK is surfaced as a human-readable recovery
  key **once** — print it or store it in a password manager. **Losing it means
  the offsite copy is unrecoverable.** It is deliberately never stored in the
  UI or logs after that first display.
- **Credentials at rest.** BYO access key/secret are Fernet-encrypted (the same
  mechanism as device credentials); the managed profile is gate-provisioned via
  env (like SMTP credentials).

## Configuration (Settings → Backups → Offsite)

- **Mode radio:** `Off` / `BareNOC-managed (subscription)` / `My own storage
  (S3-compatible)`.
- **Managed:** enter the **plan key** (beta static key, provisioned per box by
  the gate). The panel shows plan status + the backend endpoint + retention
  (beta default 30 days). No plan key → managed mode is blocked (BYO needs no
  key).
- **BYO:** endpoint, bucket, region, access key/secret, optional path prefix.
- **Schedule:** day (daily / weekday) + hour (local), default daily 03:00. The
  offsite job runs separately from the 6-hour local app-data cron (that one is
  unchanged).
- **Retention:** managed = 30 days (beta, configurable); BYO = your choice
  (bucket-side lifecycle rules are suggested in the UI).
- **Restore:** **Download a copy** returns the encrypted archive; decrypt it
  locally with the recovery key (`decrypt_remote_backup.py`) — plaintext never
  re-enters the appliance or touches BareNOC. In-app restore is a later lane.

## How it runs

The 6-hour `backup_app.sh` cron keeps producing the local app-data archive and
now also publishes a `latest_archive` pointer. A separate **hourly** host cron
(`offsite_backup.sh`) dispatches the offsite job inside the api container; the
job self-gates on the offsite schedule + mode, then:

1. encrypts the latest archive with the DEK,
2. uploads it to the S3-compatible endpoint (signed request, no boto3),
3. prunes remote objects older than the retention window (managed) and keeps
   the latest 2 local encrypted copies,
4. writes the status record (last ok/failed/size/next-run) the Backups UI shows.

## Restoring an offsite copy

```bash
# 1. On any machine with Python: decrypt the downloaded archive with your key
pip install cryptography
python3 decrypt_remote_backup.py backup-20260830.enc recovery-key.txt -o app-backup.tar.gz

# 2. On a Docker host: restore the app-data archive (see restore_app.sh)
bash restore_app.sh app-backup.tar.gz --apply
```

The archive format is intentionally simple so any AES-256-GCM tool can decrypt
it: a magic line `BARENOC_OFFSITE_V1`, a base64 12-byte nonce, then the base64
ciphertext (with the 16-byte GCM tag appended).

## Provisioning the managed backend (beta — gate only)

`proxmox/setup_omv_remote_backup.sh` installs MinIO on the OMV box, creates the
managed bucket with a 30-day expiry lifecycle, provisions a per-customer bucket
+ scoped access key, and prints the `OFFSITE_MANAGED_*` profile the gate stores
on each appliance. **The worker lane does not run this against the live NAS**
(vet-first; the gate runs it).

## Beta plan key

Managed mode is gated by a **documented static beta key** (offline-verifiable,
per-install). It is a placeholder: the Stripe → webhook → plan-key automation
is a separate later lane and will replace the static key with signed, expiring
entitlements.
