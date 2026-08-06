#!/usr/bin/env bash
# One-time GitHub setup for BareNOC. Run from the dev box (repo checkout).
#   bash scripts/github_setup.sh [<org-or-username>] [--public]
# Defaults: <github-user>/BareNOC, PRIVATE (flip to public at launch).
# Installs the gh CLI if missing, walks through auth, creates the repo,
# pushes main, and seeds the label taxonomy.
set -euo pipefail

ORG="${1:-}"
PUBLIC=0
[[ "${2:-}" == "--public" ]] && PUBLIC=1

if [[ -z "$ORG" ]]; then
  echo "Usage: bash scripts/github_setup.sh <org-or-username> [--public]" >&2
  exit 1
fi

# ── gh CLI ─────────────────────────────────────────────────────────────────
if ! command -v gh >/dev/null 2>&1; then
  echo "==> Installing gh CLI…"
  (type -p wget >/dev/null || (sudo apt update && sudo apt-get install -y wget)) >/dev/null
  sudo mkdir -p -m 755 /etc/apt/keyrings
  wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt update -qq && sudo apt install -y gh
fi

gh auth status >/dev/null 2>&1 || gh auth login

# ── repo ───────────────────────────────────────────────────────────────────
REPO="$ORG/BareNOC"
if gh repo view "$REPO" >/dev/null 2>&1; then
  echo "==> $REPO already exists — syncing remote"
else
  echo "==> Creating $REPO ($([ $PUBLIC -eq 1 ] && echo public || echo private))"
  gh repo create "$REPO" --source . --remote origin --push \
    --$([ $PUBLIC -eq 1 ] && echo public || echo private) \
    --description "BareNOC — self-hosted network operations center (SMB)"
fi
git remote -v | grep origin || git remote add origin "git@github.com:$REPO.git"

# ── labels ─────────────────────────────────────────────────────────────────
echo "==> Seeding issue labels"
for label in \
  "bug:🧑‍🔧 something is broken" \
  "feature:✨ new capability" \
  "enhancement:🔧 improvement to existing" \
  "security:🔐 hardening / credentials" \
  "docs:📚 documentation (local + wiki)" \
  "ops:🛠 deployment / backup / infra" \
  "hardware:🖥 appliance / network gear" \
  "install:📦 installer / ISO" \
  "ci:🤖 github actions / tests" \
  "release:🏷 versioning / changelog" \
  "triage:🚦 needs investigation" \
  "wontfix:🙅 won't do" \
  "duplicate:♻️ already tracked"; do
  name="${label%%:*}"
  color="$(echo "$label" | md5sum | head -c 6)"
  gh label create "$name" --color "$color" --force --description "${label#*:}" >/dev/null 2>&1 \
    && echo "  ✓ $name" || echo "  - $name (skipped)"
done

echo
echo "════════════════════════════════════════════════════════════"
echo "  Done: https://github.com/$REPO"
echo "  Push: git push -u origin main"
echo "  Next: .github/workflows/ci.yml runs on push; tag vX.Y.Z for a release"
echo "  (versioning + release process: docs/development/versioning.md)"
echo "════════════════════════════════════════════════════════════"
