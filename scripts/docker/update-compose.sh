#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

cd "${REPO_ROOT}"

SERVICE_NAME="${SERVICE_NAME:-webui}"
REMOTE_NAME="${REMOTE_NAME:-origin}"
BRANCH_NAME="${1:-${BRANCH_NAME:-$(git branch --show-current)}}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
PRESERVE_PATHS=("${COMPOSE_FILE}" ".env" ".env.local" "docker-compose.override.yml")
BACKUP_DIR="$(mktemp -d)"
OLD_HEAD="$(git rev-parse HEAD)"
STASH_REF=""
RESTORE_REQUIRED=0
EXISTING_PRESERVE_PATHS=()

log() {
  printf '[update-compose] %s\n' "$*"
}

warn() {
  printf '[update-compose] warning: %s\n' "$*" >&2
}

die() {
  printf '[update-compose] error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ ${RESTORE_REQUIRED} -eq 1 ]]; then
    restore_preserved_files || true
  fi

  if [[ -n "${STASH_REF}" ]]; then
    git stash drop --quiet "${STASH_REF}" >/dev/null 2>&1 || true
    STASH_REF=""
  fi

  rm -rf "${BACKUP_DIR}"
}

trap cleanup EXIT

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

usage() {
  cat <<'EOF'
用法:
  scripts/docker/update-compose.sh [branch]

说明:
  1. 保留本地 docker-compose / .env 配置
  2. 拉取指定分支最新代码
  3. 重新构建并重启 codex-console-commission 容器

可选环境变量:
  SERVICE_NAME   Compose 服务名，默认 webui
  REMOTE_NAME    Git 远端名，默认 origin
  BRANCH_NAME    Git 分支名；未传参时默认当前分支
  COMPOSE_FILE   Compose 文件路径，默认 docker-compose.yml
EOF
}

select_compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    return
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    return
  fi

  die "未找到 docker compose 或 docker-compose"
}

is_preserved_path() {
  local candidate="$1"
  local preserved

  for preserved in "${PRESERVE_PATHS[@]}"; do
    if [[ "${candidate}" == "${preserved}" ]]; then
      return 0
    fi
  done

  return 1
}

assert_clean_worktree() {
  local path
  local -a tracked_changes=()

  mapfile -t tracked_changes < <(
    {
      git diff --name-only
      git diff --cached --name-only
    } | sort -u
  )

  for path in "${tracked_changes[@]}"; do
    [[ -z "${path}" ]] && continue
    if ! is_preserved_path "${path}"; then
      die "检测到非部署配置文件的本地改动: ${path}。请先提交或手动处理后再执行。"
    fi
  done
}

backup_preserved_files() {
  local path
  local target

  EXISTING_PRESERVE_PATHS=()

  for path in "${PRESERVE_PATHS[@]}"; do
    [[ -e "${path}" || -L "${path}" ]] || continue
    EXISTING_PRESERVE_PATHS+=("${path}")
    target="${BACKUP_DIR}/${path}"
    mkdir -p -- "$(dirname -- "${target}")"
    cp -a -- "${path}" "${target}"
  done
}

restore_preserved_files() {
  local source
  local target

  while IFS= read -r -d '' source; do
    target="${source#"${BACKUP_DIR}/"}"
    mkdir -p -- "$(dirname -- "${target}")"
    cp -a -- "${source}" "${target}"
  done < <(find "${BACKUP_DIR}" \( -type f -o -type l \) -print0)
}

stash_preserved_files() {
  if [[ ${#EXISTING_PRESERVE_PATHS[@]} -eq 0 ]]; then
    return
  fi

  if [[ -z "$(git status --porcelain -- "${EXISTING_PRESERVE_PATHS[@]}")" ]]; then
    return
  fi

  git stash push --quiet --include-untracked --message "update-compose-preserve" -- "${EXISTING_PRESERVE_PATHS[@]}"
  STASH_REF="stash@{0}"
}

drop_preserved_stash() {
  [[ -n "${STASH_REF}" ]] || return
  git stash drop --quiet "${STASH_REF}" >/dev/null 2>&1 || true
  STASH_REF=""
}

warn_if_upstream_compose_changed() {
  local new_head

  new_head="$(git rev-parse HEAD)"

  if ! git diff --quiet "${OLD_HEAD}" "${new_head}" -- "${COMPOSE_FILE}"; then
    warn "上游更新了 ${COMPOSE_FILE}，本次仍保留了本地版本，请自行比对差异。"
  fi
}

main() {
  require_command git
  require_command docker
  select_compose_cmd

  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    return 0
  fi

  [[ -n "${BRANCH_NAME}" ]] || die "无法确定当前分支，请通过参数或 BRANCH_NAME 指定。"
  [[ -f "${COMPOSE_FILE}" ]] || die "未找到 Compose 文件: ${COMPOSE_FILE}"

  assert_clean_worktree
  backup_preserved_files
  stash_preserved_files

  RESTORE_REQUIRED=1

  log "拉取 ${REMOTE_NAME}/${BRANCH_NAME} 最新代码"
  git fetch "${REMOTE_NAME}" "${BRANCH_NAME}"
  git pull --ff-only "${REMOTE_NAME}" "${BRANCH_NAME}"

  restore_preserved_files
  RESTORE_REQUIRED=0
  drop_preserved_stash
  warn_if_upstream_compose_changed

  log "重建并重启服务 ${SERVICE_NAME}"
  "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" up -d --build --force-recreate "${SERVICE_NAME}"

  log "当前容器状态"
  "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" ps "${SERVICE_NAME}"
}

main "$@"
