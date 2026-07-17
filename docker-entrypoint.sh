#!/usr/bin/env bash
# 本文件负责容器启动时按角色迁移数据库、管理 Xvfb 生命周期并启动应用。
# 它不负责登录、页面解析或业务逻辑，任何启动错误都会让容器失败退出。
set -euo pipefail

if [[ "${RUN_MIGRATIONS:-false}" == "true" ]]; then
  python -m alembic upgrade head
fi

if [[ "${MIGRATE_ONLY:-false}" == "true" ]]; then
  exit 0
fi
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
xvfb_pid=$!
export DISPLAY=:99

cleanup() {
  kill -TERM "$app_pid" "$xvfb_pid" 2>/dev/null || true
  wait "$app_pid" 2>/dev/null || true
  wait "$xvfb_pid" 2>/dev/null || true
}

python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
app_pid=$!
trap cleanup EXIT INT TERM
wait "$app_pid"
