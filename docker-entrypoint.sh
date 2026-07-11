#!/usr/bin/env bash
# 本文件负责容器启动时迁移数据库并在 Xvfb 可见浏览器环境中启动 API。
# 它不负责登录、页面解析或业务逻辑，任何启动错误都会让容器失败退出。
set -euo pipefail

python -m alembic upgrade head
Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
export DISPLAY=:99
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
