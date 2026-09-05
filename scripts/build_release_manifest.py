#!/usr/bin/env python3
"""build_release_manifest.py — build versions.json + the release tarball + checksums.

Runs in the release workflow at every tag (or locally, pre-publish). Reads the
app version from src/api/version.py, classifies the release (major/minor/patch
by CalVer parts), and writes:

    <out>/versions.json                      the canonical manifest (public)
    <out>/bareNOC-<ver>.tar.gz               the release tree (gated by the activation key)
    <out>/bareNOC-<ver>.sha256               checksum of the tarball
    <out>/bareNOC-<ver>.tar.gz.sig           detached GPG signature (when --sign)

Usage:
  python3 scripts/build_release_manifest.py --out /tmp/assets [--source DIR] [--no-tarball]
             [--sign] [--require-sign] [--signing-email release@barenoc.com]
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import tarfile

BASE_URL = "https://barenoc.com/downloads"
MANIFEST_NAME = "versions.json"
SIGNING_EMAIL = "release@barenoc.com"


def signing_key_available(email: str) -> bool:
    """True when the release-signing secret key is in the DEFAULT gpg keyring.
    The key is looked up BY EMAIL/NAME only — never hardcode key material."""
    try:
        out = subprocess.run(
            ["gpg", "--batch", "--list-secret-keys", email],
            capture_output=True, text=True, check=True).stdout
    except Exception:
        return False
    return "sec" in out


def sign_tarball(tarball: pathlib.Path, email: str) -> pathlib.Path:
    """Detached-sign the tarball with the default keyring's key for <email>.
    Produces <tarball>.sig next to the tarball."""
    sig = pathlib.Path(str(tarball) + ".sig")
    subprocess.run(
        ["gpg", "--batch", "--yes", "--armor", "--detach-sign",
         "--local-user", email, "--output", str(sig), str(tarball)],
        check=True, capture_output=True, text=True)
    return sig


def app_version(source: pathlib.Path) -> str:
    """Read APP_VERSION from src/api/version.py (the single source of truth)."""
    text = (source / "src/api/version.py").read_text()
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("APP_VERSION not found in src/api/version.py")
    return m.group(1)


def previous_tag(source: pathlib.Path) -> str:
    """The most recent tag before HEAD (or '' if none)."""
    try:
        return subprocess.run(
            ["git", "-C", str(source), "describe", "--tags", "--abbrev=0", "HEAD~1"],
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


def classify(prev: str, cur: str) -> str:
    """CalVer kind: year=major, month=minor, day=patch."""
    def parts(v: str):
        return [int(x) for x in re.findall(r"\d+", v)][:3]
    p, c = parts(prev or ""), parts(cur)
    if not p:
        return "minor" if len(c) < 3 else "patch"
    for i, name in ((0, "major"), (1, "minor"), (2, "patch")):
        if len(c) <= i:
            break
        if c[i] != p[i]:
            return name
    return "patch"


# The shared modules the worker image COPYs into its build context are the
# src/worker/Dockerfile `COPY <file>.py .` lines that do NOT exist in
# src/worker/ but DO exist in src/api/. They are DERIVED from the Dockerfile +
# the tree (the single source of truth) so the tarball always ships them
# side-by-side in src/worker/ — no manual list to drift. The 09-03 P0: the
# first shared-module fix injected the files at a root-level `worker/` arcname
# that never exists in the `src/` layout, so tierrouter.py + ratewindows.py
# never landed in src/worker/, the worker build failed '"/tierrouter.py": not
# found', and every self-update rolled back.
def _worker_shared_modules(source: pathlib.Path) -> list:
    """The shared modules the worker image COPYs that live in src/api/ (not
    src/worker/). This is the exact set the tarball must ship side-by-side."""
    dockerfile = source / "src" / "worker" / "Dockerfile"
    mods = []
    try:
        lines = dockerfile.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        m = re.match(r"^COPY\s+([A-Za-z0-9_]+\.py)\s+\.\s*$", line.strip())
        if not m:
            continue
        name = m.group(1)
        if not (source / "src" / "worker" / name).exists() \
                and (source / "src" / "api" / name).exists():
            mods.append(name)
    return mods


def build_tarball(source: pathlib.Path, ver: str, out: pathlib.Path) -> pathlib.Path:
    tarball = out / f"bareNOC-{ver}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        for p in sorted(source.iterdir()):
            if p.name in (".git", "__pycache__", "dist", "SESSION_LOG.md"):
                continue
            tf.add(p, arcname=p.name, recursive=True)
            if p.name == "src":
                # include the shared modules from src/api/ inside src/worker/ —
                # the worker's Dockerfile COPYs them into its build context, so
                # the release tar MUST ship them side-by-side (the 09-03
                # .03.b self-update bug: '/tierrouter.py': not found)
                for m in _worker_shared_modules(source):
                    mp = source / "src" / "api" / m
                    if mp.exists():
                        tf.add(mp, arcname=f"src/worker/{m}")
    return tarball


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist", help="output dir (default dist/)")
    ap.add_argument("--source", default=".", help="release tree to tar (default cwd)")
    ap.add_argument("--no-tarball", action="store_true", help="manifest only")
    ap.add_argument("--changelog", default="", help="override changelog URL")
    ap.add_argument("--sign", action="store_true",
                    help="detach-sign the tarball with the release key (best-effort)")
    ap.add_argument("--require-sign", action="store_true",
                    help="fail the build when signing is requested but impossible")
    ap.add_argument("--signing-email", default=SIGNING_EMAIL,
                    help="email/name the signing key is looked up by (never a secret)")
    args = ap.parse_args()

    source = pathlib.Path(args.source).resolve()
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    ver = app_version(source)
    prev_tag = previous_tag(source)
    prev = prev_tag.lstrip("v")
    kind = classify(prev, ver)

    assets = {}
    signature = None
    if not args.no_tarball:
        tarball = build_tarball(source, ver, out)
        digest = sha256_of(tarball)
        (out / f"bareNOC-{ver}.sha256").write_text(f"{digest}  bareNOC-{ver}.tar.gz\n")
        assets["tarball"] = f"{BASE_URL}/bareNOC-{ver}.tar.gz"
        assets["checksums"] = f"{BASE_URL}/bareNOC-{ver}.sha256"

        # Detached release signature (after the tarball — see publish_release.sh
        # and docs/security/release-signing.md). The key is looked up by email in
        # the default keyring; no secret ever appears here.
        if args.sign or args.require_sign:
            if signing_key_available(args.signing_email):
                sign_tarball(tarball, args.signing_email)
                signature = f"{BASE_URL}/bareNOC-{ver}.tar.gz.sig"
                print(f"-> {out / ('bareNOC-' + ver + '.tar.gz.sig')}  (signed)")
            elif args.require_sign:
                raise SystemExit(
                    f"--require-sign: no secret key for {args.signing_email!r} in "
                    "the default gpg keyring — refusing to publish unsigned")
            else:
                print("warning: no signing key available — publishing UNSIGNED "
                      "(hash-only); see docs/security/release-signing.md")

    if signature:
        assets["signature"] = signature

    manifest = {
        "schema": 1,
        "version": ver,
        "stable": "v2026.09",   # the MONTHLY stable label (1st-of-month baseline).
        # Policy: the stable stays vYYYY.MM all month — intermediate patches flow
        # through the update channel but never move the download/stable. The ONLY
        # exception: a serious post-release bug that breaks a fresh install or the
        # upgrade path re-cuts the stable to vYYYY.MM.a (new download target)
        # until the next 1st-of-month stable is verified.
        "stable_cut": "v2026.09.01.e",  # the concrete release carrying the stable label
        "stable_rule": "bump only on a fresh-install/upgrade-breaking bug after prod release",
        "previous": prev or None,
        "kind": kind,
        "published": datetime.datetime.utcnow().isoformat() + "Z",
        "changelog": args.changelog or
                     f"https://github.com/Ridge-Chapel-Tech/barenoc-appliance/releases/tag/v{ver}",
        "assets": assets,
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    print(f"\n-> {out / MANIFEST_NAME}")
    if assets:
        print(f"-> {out / ('bareNOC-' + ver + '.tar.gz')}  ({digest[:12]}…)")


if __name__ == "__main__":
    main()
