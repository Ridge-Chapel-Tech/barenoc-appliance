#!/usr/bin/env python3
"""build_release_manifest.py — build versions.json + the release tarball + checksums.

Runs in the release workflow at every tag (or locally, pre-publish). Reads the
app version from src/api/version.py, classifies the release (major/minor/patch
by CalVer parts), and writes:

    <out>/versions.json                      the canonical manifest (public)
    <out>/bareNOC-<ver>.tar.gz               the release tree (gated by the activation key)
    <out>/bareNOC-<ver>.sha256               checksum of the tarball

Usage:
  python3 scripts/build_release_manifest.py --out /tmp/assets [--source DIR] [--no-tarball]
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


def build_tarball(source: pathlib.Path, ver: str, out: pathlib.Path) -> pathlib.Path:
    tarball = out / f"bareNOC-{ver}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        for p in sorted(source.iterdir()):
            if p.name in (".git", "__pycache__", "dist", "SESSION_LOG.md"):
                continue
            tf.add(p, arcname=p.name, recursive=True)
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
    args = ap.parse_args()

    source = pathlib.Path(args.source).resolve()
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    ver = app_version(source)
    prev_tag = previous_tag(source)
    prev = prev_tag.lstrip("v")
    kind = classify(prev, ver)

    assets = {}
    if not args.no_tarball:
        tarball = build_tarball(source, ver, out)
        digest = sha256_of(tarball)
        (out / f"bareNOC-{ver}.sha256").write_text(f"{digest}  bareNOC-{ver}.tar.gz\n")
        assets["tarball"] = f"{BASE_URL}/bareNOC-{ver}.tar.gz"
        assets["checksums"] = f"{BASE_URL}/bareNOC-{ver}.sha256"

    manifest = {
        "schema": 1,
        "version": ver,
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
