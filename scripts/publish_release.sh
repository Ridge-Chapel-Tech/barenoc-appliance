#!/bin/bash
# publish_release.sh — sync the APPROVED-FOR-RELEASE tree from this (private
# dev) repo into the EARLY-ACCESS release repo (private, invite-only).
#
# The include list below is the RELEASE CONTRACT: only what's listed here ever
# goes out. Anything not listed (or excluded) never leaves the private repo.
#
# Usage:
#   bash scripts/publish_release.sh                  # sync + commit + push
#   bash scripts/publish_release.sh --tag v2026.08.1 # also tag the release
#                                                   # (early-access repo's release.yml
#                                                   #  then publishes the GitHub Release)
#   DRY_RUN=1 bash scripts/publish_release.sh        # show what would change
#
# Env:
#   PUBLIC_REPO   default Ridge-Chapel-Tech/barenoc-appliance (PRIVATE)
set -euo pipefail

PUBLIC_REPO="${PUBLIC_REPO:-Ridge-Chapel-Tech/barenoc-appliance}"
TAG=""
# NOTE: iterate with a while+shift, NOT for-in-$@ — a for loop snapshots the
# arg list at start, so shifting inside it mis-parses the NEXT arg (the old
# version wiped the tag: `--tag vX` -> second iteration cleared TAG).
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    *) TAG="$1"; shift ;;
  esac
done
DRY="${DRY_RUN:-0}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SRC="$(cd "$(dirname "$0")/.." && pwd)"

log() { echo "[publish] $*"; }

# the private repo must be committed (we sync its WORKING TREE)
if ! git -C "$SRC" diff --quiet; then
  log "ABORT: private repo has uncommitted changes — commit first"
  exit 1
fi
[ -f "$SRC/SESSION_LOG.md" ] && log "note: SESSION_LOG.md exists locally (gitignored, kept for continuity) — it is NOT in the release contract and will not be synced"

log "syncing approved-for-release tree -> $PUBLIC_REPO"
git clone -q "https://github.com/$PUBLIC_REPO.git" "$TMP/pub" 2>/dev/null || {
  log "create the early-access repo first: gh repo create $PUBLIC_REPO --private --accept-visibility-change-consequences --description 'BareNOC — self-hosted network operations center (early access)'"
  exit 1
}
cd "$TMP/pub"

# replace the whole tree so removals from the contract propagate
git rm -rq --ignore-unmatch . 2>/dev/null || true

# ── the release contract ────────────────────────────────────────────────────
for d in src client proxmox scripts .github agent-go; do
  rsync -rltz --delete "$SRC/$d/" "$TMP/pub/$d/"
done
# docs: everything except internal-only artifacts
mkdir -p "$TMP/pub/docs"
rsync -rltz --delete --exclude=google --exclude=unifi \
  --exclude=system_acceptance_test.md --exclude=MILESTONES.md \
  "$SRC/docs/" "$TMP/pub/docs/"
for f in deploy.sh install.sh README.md CHANGELOG.md CONTRIBUTING.md LICENSE \
         .env.example .gitignore; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$TMP/pub/"
done
# post-sync guard: no internal-only file may exist in the public tree
for forbidden in SESSION_LOG.md; do
  [ ! -e "$TMP/pub/$forbidden" ] || { log "ABORT: $forbidden landed in the public tree — fixing the contract"; exit 1; }
done

# ────────────────────────────────────────────────────────────────────────────

git add -A
if git diff --cached --quiet; then
  log "no changes — public repo already up to date"
  exit 0
fi
if [ "$DRY" = "1" ]; then
  log "DRY RUN — would commit:"; git status --short | head -20
  exit 0
fi

SRC_SHA="$(git -C "$SRC" rev-parse --short HEAD)"
git commit -q -m "release sync from private dev repo @ $SRC_SHA

Approved-for-release tree — see scripts/publish_release.sh for the contract."
if [ -n "$TAG" ]; then
  # -f: re-tagging is allowed — a failed workflow run leaves the tag on the
  # old commit; re-publishing moves it to the newly approved tree (no release
  # is ever created from the old one since the release job needs green CI).
  git tag -f "$TAG"
  git push -q -f origin main --tags
  log "pushed $PUBLIC_REPO @ $(git rev-parse --short HEAD) (tag $TAG)"
else
  git push -q origin main
  log "pushed $PUBLIC_REPO @ $(git rev-parse --short HEAD)"
fi
