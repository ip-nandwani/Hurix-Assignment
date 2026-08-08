#!/usr/bin/env bash
# Installs the reference publisher into the workspace so the solution scores 1.
# The task ships publisher/ empty; without this step `npm run report` finds no
# module and the verifier scores 0.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

root="${APP_ROOT:-/app}"
if [ ! -f "$root/package.json" ] && [ -f "$(pwd)/package.json" ]; then
  root="$(pwd)"
fi

mkdir -p "$root/publisher"
cp "$here/publisher/release-publisher.mjs" "$root/publisher/release-publisher.mjs"
echo "installed reference publisher -> $root/publisher/release-publisher.mjs"
