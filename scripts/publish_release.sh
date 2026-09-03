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
SIGN=0
STAGE=0
# NOTE: iterate with a while+shift, NOT for-in-$@ — a for loop snapshots the
# arg list at start, so shifting inside it mis-parses the NEXT arg (the old
# version wiped the tag: `--tag vX` -> second iteration cleared TAG).
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    --sign) SIGN=1; shift ;;
    --stage) STAGE=1; SIGN=1; shift ;;  # staged = the sign flow minus the versions.json flip
    --finalize) SIGN=1; shift ;;         # the final public launch = the sign (flip versions.json + .sig + mirror)
    *) TAG="$1"; shift ;;
  esac
done
DRY="${DRY_RUN:-0}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SRC="$(cd "$(dirname "$0")/.." && pwd)"

log() { echo "[publish] $*"; }

# ── release signing mode (gate machine only, AFTER the release workflow) ──
# Detached-sign the PUBLISHED tarball (the exact bytes the appliance will
# download) with the default gpg keyring's release key — looked up BY EMAIL,
# never hardcoded — then ship the .sig to the GitHub Release + barenoc.com/
# downloads and add the signature reference to versions.json (backward
# compatible). Run AFTER `publish_release.sh --tag vX` + a green release
# workflow:   publish_release.sh --sign vX
#
# The script now ENFORCES the green-workflow premise (waits for the tag's
# release run), pins every gh call to $PUBLIC_REPO (the dev repo has its own
# tag-triggered release — without --repo the .sig lands on the WRONG release),
# and VERIFIES both destinations serve the exact signed bytes before declaring
# success (the create step's asset list + the Hostinger auto-deploy both race).
# See docs/security/release-signing.md.
if [ "$SIGN" = "1" ]; then
  [ -n "$TAG" ] || { log "sign: pass the version — publish_release.sh --sign vX"; exit 1; }
  if [ "$STAGE" = "1" ]; then
    log "STAGED release mode — the tarball + .sig go live but versions.json is NOT"
    log "flipped (appliances stay fail-closed on the previous stable until rollout)."
  fi
  VER="${TAG#v}"
  log "release signing v$VER — signing the published tarball (key: release@barenoc.com)"
  if ! gpg --batch --list-secret-keys release@barenoc.com >/dev/null 2>&1; then
    log "ABORT: no secret key for release@barenoc.com in the default gpg keyring"
    exit 1
  fi

  DL="$TMP/dl"; mkdir -p "$DL"
  curl -fsSL "https://barenoc.com/downloads/bareNOC-$VER.tar.gz" -o "$DL/bareNOC-$VER.tar.gz" \
    || { log "ABORT: could not fetch the published tarball (has the release workflow finished?)"; exit 1; }
  curl -fsSL "https://barenoc.com/downloads/versions.json" -o "$DL/versions.json" 2>/dev/null || true

  # (1) sign the exact published bytes — never the local tree
  gpg --batch --yes --armor --detach-sign \
    --local-user release@barenoc.com \
    --output "$DL/bareNOC-$VER.tar.gz.sig" "$DL/bareNOC-$VER.tar.gz" \
    || { log "ABORT: gpg --detach-sign failed"; exit 1; }
  log "detached signature written: $DL/bareNOC-$VER.tar.gz.sig"

  # (2) versions.json gains the signature reference (every other field kept) —
  #     SKIPPED in --stage mode (the staged release must not flip what the
  #     appliances see as available; versions.json flips only at rollout).
  if [ "$STAGE" = "1" ]; then
    log "staged: versions.json left at the previous stable"
  elif [ -s "$DL/versions.json" ]; then
    python3 - "$DL/versions.json" "$VER" <<'PY'
import json, sys
p, ver = sys.argv[1], sys.argv[2]
m = json.load(open(p))
m.setdefault("assets", {})["signature"] = f"https://barenoc.com/downloads/bareNOC-{ver}.tar.gz.sig"
with open(p, "w") as f:
    json.dump(m, f, indent=2)
    f.write("\n")
PY
  else
    log "warning: versions.json not fetched — generate it via build_release_manifest.py --sign"
  fi

  # (3) GitHub Release asset — upload + VERIFY. Every gh call pins
  #     --repo $PUBLIC_REPO: run from the dev repo, gh resolves to the DEV
  #     release (tag-triggered) and the .sig silently lands on the wrong one.
  #     Also wait for the tag's release run to go green first — uploading
  #     while `gh release create` is finalizing the asset list can drop it.
  GH_ARGS="--repo $PUBLIC_REPO"
  if command -v gh >/dev/null 2>&1; then
    # 3a. wait for the tag's release workflow run to complete successfully
    RUN_STATE=""
    for _ in $(seq 1 60); do
      RUN_STATE="$(gh run list $GH_ARGS --limit 30 --json status,conclusion,headBranch \
        --jq "[.[] | select(.headBranch == \"v$VER\")][0] | .status + \"/\" + (.conclusion // \"none\")" 2>/dev/null || true)"
      case "$RUN_STATE" in
        completed/success) break ;;
        completed/failure|completed/cancelled)
          log "ABORT: release workflow for v$VER ended $RUN_STATE — fix it before signing"; exit 1 ;;
      esac
      sleep 10
    done
    [ "$RUN_STATE" = "completed/success" ] \
      || { log "ABORT: release workflow for v$VER not green after 10 min (state: $RUN_STATE)"; exit 1; }

    gh release upload "v$VER" "$DL/bareNOC-$VER.tar.gz.sig" --clobber $GH_ARGS \
      || { log "ABORT: gh release upload failed — run: gh release upload v$VER $DL/bareNOC-$VER.tar.gz.sig --clobber --repo $PUBLIC_REPO"; exit 1; }

    # 3b. verify the asset is actually served from the release, byte-exact
    ok=0
    for _ in $(seq 1 12); do
      if curl -fsSL "https://github.com/$PUBLIC_REPO/releases/download/v$VER/bareNOC-$VER.tar.gz.sig" -o "$DL/verify.sig" 2>/dev/null \
         && cmp -s "$DL/verify.sig" "$DL/bareNOC-$VER.tar.gz.sig"; then ok=1; break; fi
      sleep 5
    done
    [ "$ok" = "1" ] \
      || { log "ABORT: .sig not live on the v$VER release after retries — upload manually + re-run --sign"; exit 1; }
    log "attached + verified .sig on GitHub Release v$VER ($PUBLIC_REPO)"
  else
    log "note: gh not installed — run: gh release upload v$VER $DL/bareNOC-$VER.tar.gz.sig --clobber --repo $PUBLIC_REPO"
  fi

  # (4) mirror to barenoc.com/downloads (same path as the tarball)
  if [ -n "${WEBSITE_PAT:-}" ]; then
    git clone -q "https://x-access-token:${WEBSITE_PAT}@github.com/Ridge-Chapel-Tech/BareNOC-Website.git" "$TMP/site" 2>/dev/null || true
  else
    git clone -q "https://github.com/Ridge-Chapel-Tech/BareNOC-Website.git" "$TMP/site" 2>/dev/null || true
  fi
  if [ -d "$TMP/site/.git" ]; then
    mkdir -p "$TMP/site/downloads"
    cp "$DL/bareNOC-$VER.tar.gz.sig" "$TMP/site/downloads/"
    if [ "$STAGE" != "1" ] && [ -s "$DL/versions.json" ]; then
      cp "$DL/versions.json" "$TMP/site/downloads/"
    fi
    git -C "$TMP/site" config user.email "release@barenoc.com"
    git -C "$TMP/site" config user.name "bareNOC release bot"
    git -C "$TMP/site" add downloads/
    git -C "$TMP/site" commit -q -m "release v$VER: detached signature + signed manifest" || true
    if git -C "$TMP/site" push -q; then
      # Hostinger auto-deploy is async + flaky (its 404 page is an HTML 200) —
      # verify the mirror actually serves the PGP .sig and the signed manifest,
      # retrying up to ~4 min, and fail loudly if it never lands.
      ok=0
      for _ in $(seq 1 24); do
        if curl -fsSL "https://barenoc.com/downloads/bareNOC-$VER.tar.gz.sig" -o "$DL/mirror.sig" 2>/dev/null \
           && grep -q -- "-----BEGIN PGP SIGNATURE-----" "$DL/mirror.sig" \
           && cmp -s "$DL/mirror.sig" "$DL/bareNOC-$VER.tar.gz.sig" \
           && curl -fsSL "https://barenoc.com/downloads/versions.json" -o "$DL/mirror.json" 2>/dev/null \
           && { [ "$STAGE" = "1" ] \
                || grep -q "\"signature\": \"https://barenoc.com/downloads/bareNOC-$VER.tar.gz.sig\"" "$DL/mirror.json"; }; then
          ok=1; break
        fi
        sleep 10
      done
      if [ "$ok" = "1" ]; then
        if [ "$STAGE" = "1" ]; then
          log "mirrored + verified the .sig on barenoc.com/downloads (versions.json NOT flipped — staged)"
        else
          log "mirrored + verified .sig + signed versions.json on barenoc.com/downloads"
        fi
      else
        log "WARNING: website push landed but barenoc.com is not serving the .sig yet (Hostinger flake) —"
        log "         rerun the website deploy (gh run rerun) then re-run: publish_release.sh --sign v$VER"
        exit 1
      fi
    else
      log "ABORT: website push failed — push $DL/bareNOC-$VER.tar.gz.sig + versions.json to downloads/ manually"; exit 1
    fi
  else
    log "note: BareNOC-Website not clonable here — push $DL/bareNOC-$VER.tar.gz.sig to downloads/ manually"
  fi

  if [ "$STAGE" = "1" ]; then
    log "STAGED release complete (v$VER): artifact + .sig live, versions.json at the previous"
    log "stable — VM B's upgrade-path SAT can now self-update against v$VER fail-closed."
    log "At rollout: publish_release.sh --sign v$VER (flips versions.json)."
  else
    log "release signing complete (v$VER) — releases >= 2026.08.25.a must be signed (fail-closed)"
  fi
  exit 0
fi

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
export GIT_AUTHOR_NAME="bareNOC release bot"
export GIT_AUTHOR_EMAIL="release@barenoc.com"
export GIT_COMMITTER_NAME="bareNOC release bot"
export GIT_COMMITTER_EMAIL="release@barenoc.com"
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
