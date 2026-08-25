# Release Signing

**Status:** live design, first signed release **v2026.08.25.a** (gate time).
Last updated 2026-08-25.

BareNOC releases are signed with a detached GPG signature. The appliance
verifies the signature against a **pinned** public key before applying an
update, which breaks the previous *single trust chain* (manifest + tarball +
website all produced by one pipeline — compromise any one step and the whole
release is spoofable).

## 1. Trust model

**Before:** `publish_release.sh` → GitHub Release → `versions.json`
(+ `sha256`) → website downloads → appliance. One pipeline, one trust chain:
whoever owns the pipeline (or the site, or the manifest) can ship arbitrary
code and the `sha256` "verification" happily checks it against itself.

**After:** the release tarball carries a detached signature made with a key
that lives **only** on the gate machine. The appliance verifies the signature
against the **pinned public key** (`docs/security/release-signing.pub`,
installed at `/opt/barenoc/scripts/release-signing.pub`) — *not* the system
keyring, and *not* any key shipped inside the tarball being verified. A
compromised pipeline can still tamper with everything *except* a signature
made by the private key, which it doesn't have.

The `sha256` check is **kept** (it's cheap, catches transport corruption, and
is the sole check for pre-signing releases). Signature verification is a
**second, independent** gate layered on top.

## 2. Key management

### Private key (signing)

- Lives **only** on the gate machine's default gpg keyring
  (`~/.gnupg`, mode `0600` on the keyring files).
- Identity: `bareNOC release signing <release@barenoc.com>`
- Fingerprint: `43C3 6B6B 4C17 97F0 49DC  603B 2EC0 8107 A327 C11F`
  (ed25519).
- **NEVER** in the repo, never in CI, never handed to a dev worker, never
  printed. All signing code looks the key up **by email/name**
  (`release@barenoc.com`), never by a hardcoded secret.
- Back up the private key (and a revocation certificate) **offline** — a
  lost key strands the release train (see §6 rotation).

### Public key (verification)

- Canonical copy: `docs/security/release-signing.pub` (ships in the release
  tarball + the public repo, under the existing `docs/` publish contract).
- Runtime copy: `src/scripts/release-signing.pub` — byte-identical, applied by
  the self-update to `/opt/barenoc/scripts/release-signing.pub` (the pinned
  key path the verifier uses). A test asserts the two stay identical
  (`scripts/test_release_signing.sh`).

## 3. Signing a release

The tarball is signed "after the tarball" is built, in one of two ways:

1. **In CI (if the key is in the runner):** `scripts/build_release_manifest.py
   --sign` runs `gpg --batch --armor --detach-sign`, produces
   `bareNOC-<ver>.tar.gz.sig`, and adds `assets.signature` to `versions.json`.
   The release workflow already globs everything in the dist dir, so the
   `.sig` lands in the GitHub Release assets *and* `barenoc.com/downloads/`
   automatically.
2. **On the gate machine (the default):** the key is **not** in CI, so the
   release workflow publishes unsigned, and the gate signs the *published*
   bytes afterwards (so the signature is always over the exact tarball the
   appliance downloads):
   ```
   bash scripts/publish_release.sh --tag v2026.08.25.a   # sync + tag + push (CI runs)
   # …wait for the release workflow to go green + publish…
   bash scripts/publish_release.sh --sign v2026.08.25.a  # sign + ship the .sig
   ```
   `--sign` downloads `https://barenoc.com/downloads/bareNOC-<ver>.tar.gz`,
   signs those exact bytes, adds `assets.signature` to `versions.json`, uploads
   the `.sig` to the GitHub Release (`gh release upload`), and mirrors the
   `.sig` + signed `versions.json` to `barenoc.com/downloads/`.

### `versions.json` schema (backward compatible)

```json
"assets": {
  "tarball":    "https://barenoc.com/downloads/bareNOC-2026.08.25.a.tar.gz",
  "checksums":  "https://barenoc.com/downloads/bareNOC-2026.08.25.a.sha256",
  "signature":  "https://barenoc.com/downloads/bareNOC-2026.08.25.a.tar.gz.sig"
}
```

`schema` stays `1`. Older manifests simply have no `signature` field — every
consumer reads it with `.get("signature", "")`.

## 4. Verification (appliance)

`src/scripts/barenoc-self-update.sh` now, after the `sha256` check and before
applying:

1. Downloads the `.sig` (from the `signature` URL in `update_request.json`).
2. Calls `src/scripts/verify_release_signature.sh`, which imports **only** the
   pinned key into a throwaway `GNUPGHOME` and runs `gpg --verify`. The system
   keyring is never consulted, so a signature by any other key fails.
3. Applies the result:
   - valid → proceed;
   - missing sig + version **before** the mandatory boundary → hash-only with a
     warning;
   - missing sig + version **at/after** the boundary → **fail closed**;
   - bad sig / missing pinned key → **fail closed** (the update is aborted and
     the previous release is kept).

### Mandatory-sig boundary

**`v2026.08.25.a`** is the first signed release and the boundary: every release
at or after it **must** carry a valid signature; releases before it fall back
to hash-only. (Constant: `MANDATORY_SIG_VERSION` in
`verify_release_signature.sh`.)

### Bootstrap (trust-on-first-use)

A box still running a pre-signing self-update can't verify a signature (the
verifier ships alongside it). The **first** signed release therefore reaches
those boxes over the old hash-only path and, in doing so, installs the new
self-update + the pinned key into `/opt/barenoc/scripts/`. The active
self-update entrypoint is `/usr/local/bin/barenoc-self-update.sh` (installed by
`deploy.sh` / the installer); the self-update now re-syncs it from the applied
tree on every successful apply/rollback (`refresh_host_self_update`), so once a
box has been deployed/reinstalled at v2026.08.25.a or later, its subsequent
self-updates run the new logic. This is TOFU at the release-ship boundary and
is the accepted bootstrapping cost; the pin is then immutable until a signed
rotation (below).

## 5. What's tested

`scripts/test_release_signing.sh` generates a **throwaway** gpg key (never the
real key) and asserts: good-sig verifies, tampered tarball fails closed, a
signature by a *different* key fails closed (pinned key only), missing-sig
fallback vs fail-closed across the boundary, missing-key fail-closed, the
manifest `--sign`/`--require-sign` behavior, and the canonical/runtime pubkey
parity.

## 6. Rotation & compromise

**Rotation** (key change or scheduled): generate a new keypair on the gate
machine, publish the new public key at `docs/security/release-signing.pub` +
`src/scripts/release-signing.pub` (byte-identical), and sign a release with the
new key. The release that introduces the new key must be signed by the **old**
key so existing appliances still verify it; the new key then takes over for
the next release. Keep the old key around (read-only) to verify/roll back one
release.

**Compromise** (private key leaked): revoke immediately
(`gpg --gen-revoke`), publish the revocation + a new key in a release signed by
the new key, and treat every box's pin as needing the new key (a signed
rotation release does this automatically). Bump the mandatory boundary so
unsigned/old-key releases stop being accepted.
