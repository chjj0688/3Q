#!/bin/bash
set -euo pipefail
umask 077
support="$HOME/Library/Application Support/3Q"
cd "$support/current"
export THREEQ_RELEASE="$(cat .release-version)"
export THREEQ_RELEASE_REPO="$(cat "$support/repository-path")"
python="$support/venvs/$THREEQ_RELEASE/bin/python"
origin="$(cat "$support/mobile-origin")"
args=(live --port 8033)
if [[ -n "$origin" ]]; then
  args+=(--mobile-origin "$origin" --mobile-port 8034)
fi
exec "$python" -m threeq "${args[@]}"
