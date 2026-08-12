#!/bin/sh
# Bump the project version and create a matching commit and git tag.
#
# Usage: scripts/bump-version.sh major|minor|patch
set -eu

kind="${1:-}"
case "$kind" in
    major | minor | patch) ;;
    *)
        echo "Usage: $0 major|minor|patch" >&2
        exit 1
        ;;
esac

if [ -n "$(git status --porcelain)" ]; then
    echo "Working tree is not clean. Commit or stash changes before bumping." >&2
    exit 1
fi

uv version --bump "$kind" >/dev/null

new_version=$(uv version --short)

git add pyproject.toml uv.lock
git commit -m "chore: bump version to $new_version"
git tag "v$new_version"

echo "Bumped to $new_version, committed and tagged as v$new_version."
echo "Push with: git push && git push --tags"
