#!/usr/bin/env bash
# Route a public hostname to an app container through the shared edge proxy.
#
#   register-site.sh <hostname> <upstream>
#   register-site.sh notes.cwtches.co.uk notes:8000
#
# Runs the "Add site" workflow in alankey-dev/edge-proxy (override with EDGE_PROXY_REPO), which
# writes sites/<hostname>.caddy, validates, commits and hot-reloads Caddy. Needs an authenticated gh CLI.
set -euo pipefail

host="${1:-}"
upstream="${2:-}"
repo="${EDGE_PROXY_REPO:-alankey-dev/edge-proxy}"

if [ -z "$host" ] || [ -z "$upstream" ]; then
  echo "usage: register-site.sh <hostname> <upstream>   (e.g. app.cwtches.co.uk app:8000)" >&2
  exit 1
fi
# Same guards as edge-proxy's add-site.sh, so a bad value fails here rather than in the workflow.
printf '%s' "$host" | grep -Eq '^[a-zA-Z0-9.-]+$' || { echo "invalid hostname: $host" >&2; exit 1; }
printf '%s' "$upstream" | grep -Eq '^[a-zA-Z0-9._-]+:[0-9]+$' || { echo "invalid upstream (want host:port): $upstream" >&2; exit 1; }

if gh api "repos/$repo/contents/sites/$host.caddy" --jq .content >/dev/null 2>&1; then
  current="$(gh api "repos/$repo/contents/sites/$host.caddy" --jq .content | base64 -d | grep -o 'reverse_proxy .*' || true)"
  echo "route already exists for $host ($current); re-running updates it to $upstream"
fi

gh workflow run add-site.yml -R "$repo" -f hostname="$host" -f upstream="$upstream"
sleep 5
run_id="$(gh run list -R "$repo" --workflow add-site.yml -L 1 --json databaseId --jq '.[0].databaseId')"
echo "started: https://github.com/$repo/actions/runs/$run_id"
gh run watch -R "$repo" "$run_id" --exit-status
