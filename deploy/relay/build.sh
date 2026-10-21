#!/usr/bin/env bash
# Build the relay image from a COMMIT: `git archive` of the ref (default HEAD) is the build context, so the image
# holds the tracked files of that commit and nothing from the working tree, whether the tree is clean or not (the
# untracked config.yaml, auths/ and logs/ of this checkout cannot get in). The tag and the revision inside the image
# are the commit's id; /version and every run record then name it (docs/relay/docker.md).
#
# Usage: deploy/relay/build.sh [--test] [REF]
#   --test   build the `test` stage first: the whole offline suite on the image's interpreter (about 2 minutes)
#   REF      a commit-ish, default HEAD. Production builds the merge commit on main.
# Prints the tag it built. Nothing is pushed anywhere.
set -euo pipefail

RUN_TESTS=0
REF=HEAD
for arg in "$@"; do
  case "$arg" in
    --test) RUN_TESTS=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) REF="$arg" ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"
SHA="$(git rev-parse --verify "${REF}^{commit}")"
TAG="clisaving-relay:${SHA}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.14-slim@sha256:f85c5697265c178cc6887276c55fe16cf3d14ca35c3df6a5eab3b360534a55d2}"

build() {  # build TARGET [extra docker build args]
  local target="$1"; shift
  git archive --format=tar "$SHA" | docker build \
    --file deploy/relay/Dockerfile --target "$target" \
    --build-arg "PYTHON_IMAGE=${PYTHON_IMAGE}" --build-arg "REVISION=${SHA}" "$@" -
}

if [ "$RUN_TESTS" = 1 ]; then
  echo "== offline suite on ${PYTHON_IMAGE} for ${SHA}" >&2
  build test --tag "clisaving-relay-test:${SHA}" >&2
fi
echo "== runtime image ${TAG}" >&2
build runtime --tag "$TAG" >&2
echo "$TAG"
