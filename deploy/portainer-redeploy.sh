#!/usr/bin/env bash
# Redeploy a git-backed Portainer stack onto a freshly pushed image, then
# wait for every listed container to come back healthy *on the new image*.
# Called by .github/workflows/publish.yml after a push from main.
#
# Required env:
#   PORTAINER_URL, PORTAINER_API_KEY, PORTAINER_STACK_ID
#   REGISTRY, IMAGE, REGISTRY_USERNAME, REGISTRY_PASSWORD
#   PORTAINER_CONTAINERS   space-separated container names to watch
# Optional env:
#   PORTAINER_ENDPOINT_ID (1), REGISTRY_REF_NAME (refs/heads/main),
#   MAX_WAIT_SECONDS (300), GIT_SHA (for the final log line)
#
# Steps:
#   1. read the stack's current env (redeploy would otherwise wipe it)
#   2. force-pull :latest on the host (Portainer's pullImage alone can race)
#   3. PUT /stacks/{id}/git/redeploy (Portainer re-reads the compose from git)
#   4. poll each container until it is running, healthy, and on a new image ID
set -euo pipefail

: "${PORTAINER_URL:?}" "${PORTAINER_API_KEY:?}" "${PORTAINER_STACK_ID:?}"
: "${REGISTRY:?}" "${IMAGE:?}" "${REGISTRY_USERNAME:?}" "${REGISTRY_PASSWORD:?}"
: "${PORTAINER_CONTAINERS:?}"
PORTAINER_ENDPOINT_ID="${PORTAINER_ENDPOINT_ID:-1}"
REGISTRY_REF_NAME="${REGISTRY_REF_NAME:-refs/heads/main}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-300}"

api() { curl -sS -H "X-API-Key: $PORTAINER_API_KEY" "$@"; }
container_url() { echo "$PORTAINER_URL/api/endpoints/$PORTAINER_ENDPOINT_ID/docker/containers/$1"; }

# Run a request; print its body, fail loudly on non-2xx.
call() {
  local step="$1"; shift
  local out code
  out=$(api -w $'\n%{http_code}' "$@")
  code="${out##*$'\n'}"
  out="${out%$'\n'*}"
  if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
    echo "❌ $step failed (HTTP $code): $out" >&2
    exit 1
  fi
  echo "✅ $step (HTTP $code)" >&2
  printf '%s' "$out"
}

echo "🔄 Redeploying stack $PORTAINER_STACK_ID"
STACK_ENV_JSON=$(call "Stack info" "$PORTAINER_URL/api/stacks/$PORTAINER_STACK_ID" | jq '.Env')

REGISTRY_AUTH=$(jq -cn --arg u "$REGISTRY_USERNAME" --arg p "$REGISTRY_PASSWORD" --arg s "$REGISTRY" \
  '{username: $u, password: $p, serveraddress: $s}' | base64 -w0)
call "Image pull" -X POST -H "X-Registry-Auth: $REGISTRY_AUTH" \
  "$PORTAINER_URL/api/endpoints/$PORTAINER_ENDPOINT_ID/docker/images/create?fromImage=$REGISTRY/$IMAGE&tag=latest" >/dev/null

declare -A OLD_IMAGE
for c in $PORTAINER_CONTAINERS; do
  OLD_IMAGE[$c]=$(api "$(container_url "$c")/json" | jq -r '.Image // "missing"')
  echo "📌 $c pre-deploy image: ${OLD_IMAGE[$c]}"
done

BODY=$(jq -cn \
  --argjson env "$STACK_ENV_JSON" \
  --arg u "$REGISTRY_USERNAME" --arg p "$REGISTRY_PASSWORD" --arg ref "$REGISTRY_REF_NAME" \
  '{env: $env, prune: true, pullImage: true, repositoryAuthentication: true,
    repositoryUsername: $u, repositoryPassword: $p, repositoryReferenceName: $ref}')
call "Stack redeploy" -X PUT -H "Content-Type: application/json" -d "$BODY" \
  "$PORTAINER_URL/api/stacks/$PORTAINER_STACK_ID/git/redeploy?endpointId=$PORTAINER_ENDPOINT_ID" >/dev/null

START=$(date +%s)
for c in $PORTAINER_CONTAINERS; do
  echo "⏳ Waiting for $c to be healthy on the new image..."
  while true; do
    INSPECT=$(api "$(container_url "$c")/json")
    STATUS=$(jq -r '.State.Status // "missing"' <<<"$INSPECT")
    HEALTH=$(jq -r '.State.Health.Status // "none"' <<<"$INSPECT")
    CURRENT=$(jq -r '.Image // "missing"' <<<"$INSPECT")
    changed=no; [[ "$CURRENT" != "${OLD_IMAGE[$c]}" ]] && changed=yes
    echo "  $c status=$STATUS health=$HEALTH image_changed=$changed"

    if [[ "$STATUS" == "exited" || "$STATUS" == "dead" ]]; then
      echo "❌ $c is $STATUS"
      api "$(container_url "$c")/logs?stdout=1&stderr=1&tail=100" | tr -cd '[:print:]\n' || true
      exit 1
    fi
    if [[ "$STATUS" == "running" && "$HEALTH" == "healthy" && "$changed" == "yes" ]]; then
      echo "✅ $c healthy on new image"
      break
    fi
    if [[ "$HEALTH" == "unhealthy" && "$changed" == "yes" ]]; then
      echo "❌ $c is unhealthy after redeploy"
      api "$(container_url "$c")/logs?stdout=1&stderr=1&tail=100" | tr -cd '[:print:]\n' || true
      exit 1
    fi
    if (( $(date +%s) - START >= MAX_WAIT_SECONDS )); then
      echo "❌ Timed out waiting for $c"
      exit 1
    fi
    sleep 5
  done
done

echo "🎉 Redeploy complete: ${GIT_SHA:-unknown}"
