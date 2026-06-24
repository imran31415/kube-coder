#!/usr/bin/env bash
# scripts/release.sh — cut a kube-coder release end to end.
#
# Usage: scripts/release.sh <version> [notes-file]
#   scripts/release.sh v1.3.0
#   scripts/release.sh 1.3.0 docs/releases/v1.3.0.md   (leading 'v' optional)
#
# What it does (in order, failing fast):
#   1. Preflight — version format, on 'main', clean tree, in sync with
#      origin/main, tag not already taken, gh authenticated.
#   2. Bump tracked version files: Makefile VERSION, every charts/*/Chart.yaml
#      appVersion, the chart default image tag, and the new-user scaffold
#      default — all to the release version.
#   3. Build + push the matching devlaptop-<version> image (make push).
#   4. Commit the bumps and create an annotated tag.
#   5. After an explicit y/N confirmation, push main + tag and create the
#      GitHub release (notes-file if given, else --generate-notes).
#
# It deliberately does NOT redeploy workspaces: every operator has their own
# (gitignored) workspaces, so rollout is a separate step. The script prints
# the exact follow-up command when it finishes.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION_IN="${1:-}"
NOTES_FILE="${2:-}"

if [ -z "$VERSION_IN" ]; then
  echo "Usage: $0 <version> [notes-file]   (e.g. $0 v1.3.0)" >&2
  exit 2
fi

# Normalize to a single leading 'v'; NUM is the bare numeric form (appVersion).
VER="v${VERSION_IN#v}"
NUM="${VER#v}"
TAG="$VER"
IMAGE_TAG="devlaptop-$VER"

if ! printf '%s' "$NUM" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "ERROR: version must be MAJOR.MINOR.PATCH (e.g. v1.3.0); got '$VERSION_IN'." >&2
  exit 2
fi

# Revert the version-file edits if we bail out before committing them, so a
# failed run never leaves the tree half-bumped. (Preflight guarantees the tree
# was clean, so checking these paths out is safe.)
COMMITTED=0
cleanup() {
  if [ "$COMMITTED" = "0" ]; then
    git checkout -- Makefile charts/*/Chart.yaml charts/workspace/values.yaml \
      scripts/new-user.sh 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "==> Releasing kube-coder $TAG  (image: $IMAGE_TAG)"

# ---- 1. Preflight ----
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || { echo "ERROR: must be on 'main' (on '$branch')." >&2; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "ERROR: uncommitted tracked changes — commit or stash first:" >&2
  git status --short >&2; exit 1
fi
git fetch origin --quiet
if [ -n "$(git log origin/main..HEAD --oneline)" ] || [ -n "$(git log HEAD..origin/main --oneline)" ]; then
  echo "ERROR: local main is out of sync with origin/main — push/pull first." >&2; exit 1
fi
git rev-parse "$TAG" >/dev/null 2>&1 && { echo "ERROR: tag $TAG already exists." >&2; exit 1; } || true
gh auth status >/dev/null 2>&1 || { echo "ERROR: gh is not authenticated (run: gh auth login)." >&2; exit 1; }

# ---- 2. Bump tracked version files ----
echo "==> Bumping versions (VERSION=$VER, appVersion=$NUM, image=$IMAGE_TAG)"
sed -i.bak -E "s/^VERSION := .*/VERSION := $VER/" Makefile
for c in charts/*/Chart.yaml; do
  sed -i.bak -E "s/^appVersion: .*/appVersion: \"$NUM\"/" "$c"
done
sed -i.bak -E "s|^([[:space:]]*tag:[[:space:]]*)devlaptop-.*|\1$IMAGE_TAG|" charts/workspace/values.yaml
sed -i.bak -E "s/(IMAGE_TAG:-)v?[0-9][^}]*/\1$VER/" scripts/new-user.sh
rm -f Makefile.bak charts/*/Chart.yaml.bak charts/workspace/values.yaml.bak scripts/new-user.sh.bak
git --no-pager diff --stat

# ---- 3. Build + push the release image ----
echo "==> Building + pushing $IMAGE_TAG"
make --no-print-directory push

# ---- 4. Commit + tag ----
echo "==> Committing version bump + tagging $TAG"
git add -u
git commit -m "chore(release): $TAG

Release-matched image $IMAGE_TAG, chart appVersion $NUM, and scaffold default.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git tag -a "$TAG" -m "$TAG"
COMMITTED=1

# ---- 5. Confirm, then push + publish ----
printf "\nPush 'main' + tag %s and publish the GitHub release? [y/N] " "$TAG"
read -r ans
if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
  echo "Stopped before publish. Commit + tag are local; finish with:"
  echo "  git push origin main && git push origin $TAG && gh release create $TAG ..."
  exit 0
fi

git push origin main
git push origin "$TAG"
if [ -n "$NOTES_FILE" ] && [ -f "$NOTES_FILE" ]; then
  gh release create "$TAG" --title "$TAG" --notes-file "$NOTES_FILE"
else
  echo "(no notes file given — using GitHub auto-generated notes)"
  gh release create "$TAG" --title "$TAG" --generate-notes
fi

echo
echo "==> Released $TAG  →  https://github.com/imran31415/kube-coder/releases/tag/$TAG"
echo "Next: roll your workspaces onto $IMAGE_TAG, e.g."
echo "  for u in <your-workspaces>; do make deploy USER=\$u; done && make ship-controller-config"
