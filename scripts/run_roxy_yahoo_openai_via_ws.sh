#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

WIN_HOST="${WIN_HOST:-${1:-}}"
CDP_PORT="${CDP_PORT:-${2:-}}"
PARENT_EMAIL="${PARENT_EMAIL:-}"
PARENT_PASSWORD="${PARENT_PASSWORD:-}"
PARENT_APP_PASSWORD="${PARENT_APP_PASSWORD:-}"
OPENAI_PROXY="${OPENAI_PROXY:-}"
SERVICE_ID="${SERVICE_ID:-2}"
WORKSPACE_ID="${WORKSPACE_ID:-0}"
SAVE_DB="${SAVE_DB:-1}"
DOMAIN="${DOMAIN:-yahoo.com}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUT_DIR="${OUT_DIR:-release}"

if [[ -z "$WIN_HOST" || -z "$CDP_PORT" ]]; then
  cat <<'EOF'
Usage:
  WIN_HOST=192.168.142.X \
  CDP_PORT=59305 \
  PARENT_EMAIL=xxx@yahoo.com \
  PARENT_PASSWORD='xxx' \
  PARENT_APP_PASSWORD='xxx' \
  OPENAI_PROXY='http://user:pass@host:port' \
  bash scripts/run_roxy_yahoo_openai_via_ws.sh

Or:
  bash scripts/run_roxy_yahoo_openai_via_ws.sh 192.168.142.X 59305
EOF
  exit 1
fi

if [[ -z "$PARENT_EMAIL" || -z "$PARENT_PASSWORD" ]]; then
  echo "[ERR] PARENT_EMAIL / PARENT_PASSWORD 必填" >&2
  exit 1
fi

if [[ -z "$OPENAI_PROXY" ]]; then
  echo "[ERR] OPENAI_PROXY 必填" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

VERSION_URL="http://${WIN_HOST}:${CDP_PORT}/json/version"
echo "[INFO] Fetch CDP version from: $VERSION_URL"

WS_ENDPOINT="$(
  curl -fsSL "$VERSION_URL" | "$PYTHON_BIN" - <<'PY'
import json, sys
payload = json.load(sys.stdin)
print(payload.get("webSocketDebuggerUrl", "").strip())
PY
)"

if [[ -z "$WS_ENDPOINT" ]]; then
  echo "[ERR] 未从 /json/version 获取到 webSocketDebuggerUrl" >&2
  exit 1
fi

echo "[INFO] WS endpoint: $WS_ENDPOINT"

ALIAS_OUTPUT="${OUT_DIR%/}/roxy_yahoo_alias_minimal.via_ws.json"
REMOTE_OUTPUT="${OUT_DIR%/}/roxy_yahoo_alias_openai_remote.via_ws.json"

echo "[STEP] 1/2 接管已登录 Yahoo 窗口并创建 alias"
"$PYTHON_BIN" scripts/roxy_yahoo_alias_minimal.py \
  --ws-endpoint "$WS_ENDPOINT" \
  --parent-email "$PARENT_EMAIL" \
  --parent-password "$PARENT_PASSWORD" \
  --parent-app-password "$PARENT_APP_PASSWORD" \
  --domain "$DOMAIN" \
  --output "$ALIAS_OUTPUT"

echo "[STEP] 2/2 复用同一 CDP 窗口执行 Yahoo alias -> OpenAI 注册"
"$PYTHON_BIN" scripts/roxy_yahoo_alias_openai_remote.py \
  --roxy-ws-endpoint "$WS_ENDPOINT" \
  --workspace-id "$WORKSPACE_ID" \
  --service-id "$SERVICE_ID" \
  --proxy "$OPENAI_PROXY" \
  --output "$REMOTE_OUTPUT" \
  $([[ "$SAVE_DB" == "1" ]] && printf '%s' '--save-db')

echo
echo "[OK] 执行完成"
echo "  alias output : $ALIAS_OUTPUT"
echo "  remote output: $REMOTE_OUTPUT"
echo "  save_db      : $SAVE_DB"
