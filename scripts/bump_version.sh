#!/usr/bin/env bash
# Bump BareNOC version (CalVer — date-based) + open a CHANGELOG entry. Usage:
#   scripts/bump_version.sh [major|minor|hotfix|<YYYY.MM[.DD[.letter]]>]
#
#   major  → YYYY.MM        (monthly feature release; no-op if already this month)
#   minor  → YYYY.MM.DD     (first release of a day)
#   hotfix → YYYY.MM.DD.<next letter>  (same-day ordinal: a, b, c, …)
#
# Bumps src/api/version.py, moves [Unreleased] → [<ver>], commits, prints the
# tag command. Run from a clean main.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="${1:-hotfix}"  # default = hotfix: new day → YYYY.MM.DD.a, same day → next letter (the release convention; the bare-date "minor" mode is opt-in)
VERSION_FILE="src/api/version.py"
CURRENT="$(python3 -c "import sys;sys.path.insert(0,'src/api');import version;print(version.APP_VERSION)")"
NOW="$(date +%Y.%m.%d)"
MONTH="$(date +%Y.%m)"

new_version() {
  case "$MODE" in
    major)
      # monthly major: current month (no day). No-op if already this month.
      if [[ "$CURRENT" == "$MONTH" ]]; then
        echo "already at monthly major $CURRENT — nothing to do" >&2
        exit 0
      fi
      echo "$MONTH"
      ;;
    minor)
      if [[ "$CURRENT" == "$NOW" || "$CURRENT" == "$NOW".* ]]; then
        # a release already exists today → take the next same-day ordinal
        letter=$(next_letter "${CURRENT##*.}")
        echo "$NOW.$letter"
      else
        echo "$NOW"
      fi
      ;;
    hotfix)
      if [[ "$CURRENT" == "$NOW".* ]]; then
        echo "$NOW.$(next_letter "${CURRENT##*.}")"
      else
        echo "$NOW.a"
      fi
      ;;
    *)
      # explicit version (e.g. 2026.08.05.a) — validated loosely
      if [[ "$MODE" =~ ^[0-9]{4}\.[0-9]{2}(\.[0-9]{2})?(\.[a-z])?$ ]]; then
        echo "$MODE"
      else
        echo "invalid version '$MODE' — use major|minor|hotfix|<YYYY.MM[.DD[.letter]]>" >&2
        exit 1
      fi
      ;;
  esac
}

next_letter() {
  # current suffix letter (or 'z' cap); returns the next one
  local cur="${1:-}"
  if [[ -z "$cur" || ! "$cur" =~ ^[a-y]$ ]]; then echo a; else
    printf "\\$(printf '%03o' $(( $(printf '%d' "'$cur") + 1 )))"
  fi
}

NEWVER="$(new_version)"
[[ -n "$NEWVER" ]] || exit 1

[[ -z "$(git status --porcelain)" ]] || { echo "ERROR: working tree not clean" >&2; exit 1; }

sed -i "s/APP_VERSION = \"$CURRENT\"/APP_VERSION = \"$NEWVER\"/" "$VERSION_FILE"
grep -q "APP_VERSION = \"$NEWVER\"" "$VERSION_FILE" || { echo "ERROR: version bump failed" >&2; exit 1; }

# move [Unreleased] → [<ver>] with today's date; keep the section content
if grep -q '^## \[Unreleased\]' CHANGELOG.md; then
  sed -i "0,/^## \[Unreleased\]/s//## [$NEWVER] — $(date +%F)/" CHANGELOG.md
fi

# the GOTCHA: bump renames [Unreleased] → [<ver>] but MUST leave a fresh
# [Unreleased] header behind above it, or the next bump has nothing to rename
# and SAT-009 (missing [Unreleased] header) fires post-bump.
if grep -q "^## \[$NEWVER\]" CHANGELOG.md; then
  sed -i "0,/^## \[$NEWVER\]/s//## [Unreleased]\n\n## [$NEWVER]/" CHANGELOG.md
fi

git add "$VERSION_FILE" CHANGELOG.md
git commit -m "build: bump version to $NEWVER" >/dev/null

echo "Bumped $CURRENT → $NEWVER (commit created)"
echo "Next: git push origin main && git tag v$NEWVER && git push origin v$NEWVER"
