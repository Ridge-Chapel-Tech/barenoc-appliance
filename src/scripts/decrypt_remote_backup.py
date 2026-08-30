#!/usr/bin/env python3
"""Decrypt a BareNOC offsite backup archive locally, with the recovery key.

The offsite copy is encrypted on the appliance BEFORE upload; BareNOC never
sees plaintext. To restore, download the .enc file from Settings → Backups →
Offsite and run this script with the recovery key you saved:

    python3 decrypt_remote_backup.py backup.enc recovery-key.txt
    # or:   python3 decrypt_remote_backup.py backup.enc   (prompts for the key)
    # or:   python3 decrypt_remote_backup.py backup.enc -o restored.tar.gz

Requires only the `cryptography` package (pip install cryptography). The output
is the app-data archive (a .tar.gz) — feed it to restore_app.sh --apply on a
Docker host.

The file format is documented in docs/remote-backup.md — any AES-256-GCM tool
can decrypt it (magic line + base64 nonce + base64 ciphertext/tag).
"""

import argparse
import base64
import getpass
import os
import re
import sys

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    print("This script needs the 'cryptography' package:\n"
          "    pip install cryptography", file=sys.stderr)
    sys.exit(2)

MAGIC = b"BARENOC_OFFSITE_V1"
AAD = b"barenoc-offsite-v1"


def decode_recovery_key(s: str) -> bytes:
    compact = re.sub(r"[\s\-_]", "", s).upper()
    compact += "=" * ((8 - len(compact) % 8) % 8)
    return base64.b32decode(compact)


def decrypt(enc_path: str, out_path: str, key: bytes) -> None:
    with open(enc_path, "rb") as f:
        parts = f.read().split(b"\n", 2)
    if len(parts) != 3 or parts[0] != MAGIC:
        print("Not a BareNOC offsite archive (bad magic).", file=sys.stderr)
        sys.exit(1)
    nonce = base64.b64decode(parts[1])
    ciphertext = base64.b64decode(parts[2])
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, AAD)
    except Exception as e:  # noqa: BLE001
        print(f"Decryption failed (wrong recovery key?): {e}", file=sys.stderr)
        sys.exit(1)
    with open(out_path, "wb") as f:
        f.write(plaintext)
    print(f"✅ Decrypted {enc_path} -> {out_path} ({len(plaintext)} bytes)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archive", help="the .enc file downloaded from the appliance")
    p.add_argument("keyfile", nargs="?", help="file containing the recovery key (or omit to type it)")
    p.add_argument("-o", "--out", help="output path (default: <archive>.tar.gz)")
    args = p.parse_args()

    if not os.path.isfile(args.archive):
        print(f"archive not found: {args.archive}", file=sys.stderr)
        return 1

    if args.keyfile:
        with open(args.keyfile) as f:
            raw = decode_recovery_key(f.read())
    else:
        raw = decode_recovery_key(getpass.getpass("Recovery key: "))

    out = args.out or re.sub(r"\.enc$", "", args.archive) + ".tar.gz"
    decrypt(args.archive, out, raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
