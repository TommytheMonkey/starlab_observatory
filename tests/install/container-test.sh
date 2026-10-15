#!/usr/bin/env bash
# Full installation test in a throwaway container: a clean Linux with no USB devices, no Starlab config,
# no database. Never touches this machine's Starlab, services, data or radios.
#
#   tests/install/container-test.sh [image]        default image: ubuntu:24.04
set -euo pipefail
IMAGE="${1:-ubuntu:24.04}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# the working tree as a user would download it (tracked and new files; no .dev sandbox, no venv, no prompts)
git -C "$ROOT" ls-files -co --exclude-standard -z | grep -zv '^prompts/' | tar -C "$ROOT" --null -T - -czf "$WORK/starlab.tgz"
cp "$ROOT/tests/install/inside.sh" "$ROOT/tests/install/e2e.py" "$WORK/"
echo "== testing in $IMAGE (no devices passed in)"
docker run --rm --network bridge -v "$WORK:/work:ro" "$IMAGE" bash /work/inside.sh
