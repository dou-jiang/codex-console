#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

cd "${SCRIPT_DIR}"

SERVICE_NAME="${SERVICE_NAME:-webui}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
REMOTE_NAME="${REMOTE_NAME:-origin}"
BRANCH_NAME="release-v1.1.2"

if [[ ! -x "${SCRIPT_DIR}/scripts/docker/update-compose.sh" ]]; then
  printf 'error: missing executable script: %s\n' "${SCRIPT_DIR}/scripts/docker/update-compose.sh" >&2
  exit 1
fi

exec \
  SERVICE_NAME="${SERVICE_NAME}" \
  COMPOSE_FILE="${COMPOSE_FILE}" \
  REMOTE_NAME="${REMOTE_NAME}" \
  "${SCRIPT_DIR}/scripts/docker/update-compose.sh" \
  "${BRANCH_NAME}"
