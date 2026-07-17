# Goal 4 报告：POC 容器化交付

> 本文是 Goal 4 阶段的历史报告，记录当时的 SQLite POC 验收结果。当前跨仓库同步、PostgreSQL、定时调度与容器启动方式以 `docs/goals/goal-merge2-progress.md`、`docs/architecture.md` 和 `goalmerge2.txt` 为准。

## 目标与最终状态

让另一位工程师可以从全新容器数据库启动、访问并使用完整 POC。

```text
PASSED
```

## 交付内容

- `README.md`
- `.env.example`
- `.dockerignore`
- `Dockerfile`
- `docker-compose.yml`
- `docker-entrypoint.sh`
- Alembic 初始迁移
- `THIRD_PARTY_NOTICES.md`
- `docs/architecture.md`
- `docs/data-model.md`
- `docs/api.md`
- `docs/live-test-report.md`
- `docs/known-limitations.md`

## 容器设计

- 基础镜像：官方 `mcr.microsoft.com/playwright/python:v1.61.0-noble`；
- 项目显式打包 `app*` 和静态页面；
- `app_data` named volume 保存 SQLite；
- 本地 `storage_state.json` 只读挂载，不进入镜像；
- 入口先执行 `alembic upgrade head`；
- Xvfb 提供可见 Chromium 环境，`XIANYU_HEADLESS=false`；
- Uvicorn 通过 `exec` 成为 PID 1；
- Compose 健康检查请求 `/health`。

## 实际执行命令

```text
docker pull mcr.microsoft.com/playwright/python:v1.61.0-noble
docker compose up --build -d
docker compose down
docker compose up -d
docker compose up --build -d --force-recreate
docker compose ps
docker logs x-comments-app-1
docker exec x-comments-app-1 python -m alembic current
```

首次基础镜像拉取约 11 分钟，后续项目层构建成功。

## 空数据库启动证据

Compose 创建全新 `x-comments_app_data` named volume。容器日志显示：

```text
Running upgrade -> 20260711_0001
```

容器内迁移版本为 `20260711_0001 (head)`，真实任务前 `items` 行数为 `0`。

## 服务验收

- `docker compose ps`：容器 `healthy`；
- `GET /health`：HTTP 200，应用和数据库均 `ok`；
- `GET /docs`：HTTP 200；
- `GET /`：HTTP 200；
- Xvfb 与 Chromium 依赖通过后续真实任务证明可用。

## 容器真实采集

通过宿主机访问容器 API 创建任务：

- 任务 ID：`c4d65b2b-591f-4a11-bd67-c9c8f7771d2a`
- 关键词：`女生发饰`
- 状态：`succeeded`
- 发现/新增/更新/重复/错误：`50/50/0/0/0`
- 轮询 5 次后完成

该任务在全新 named volume 中真实执行，证明容器环境的 Playwright、只读登录态、解析、去重、任务统计和 SQLite 写入完整可用。

首次从 Windows PowerShell 手写 JSON 的诊断请求未声明 UTF-8，实际关键词保存为 `????`，因此没有被用作验收证据。最终请求使用 JSON Unicode 转义，容器数据库以 `\u5973\u751f\u53d1\u9970` 核对为“女生发饰”，关键词过滤 API 返回 50 条；终端显示的 mojibake 不影响数据库 Unicode 值。

## 自动测试与静态检查

```text
python -m pytest -q
9 passed

python -m ruff check app tests alembic scripts
All checks passed

python -m mypy app
Success: no issues found

docker compose config
配置正常展开
```

## 硬验收核对

| 标准 | 结果 |
| --- | --- |
| 从空数据库启动 | 通过 |
| 迁移自动成功 | 通过 |
| README 可复现 | 按文档实际执行通过 |
| `.env.example` 无真实凭据 | 通过 |
| Playwright 浏览器依赖完整 | 容器真实任务通过 |
| API、数据库、测试页面可用 | 通过 |
| 所有自动测试通过 | 9/9 |
| 容器环境真实采集 | 50 条，0 错误 |
| 明确不保证全量覆盖 | README/限制文档已说明 |
| 明确未实现五分钟调度和下架复查 | README/限制文档已说明 |

## 已知限制

人工登录不能在纯容器后台自动完成，必须按 README 在宿主机运行登录脚本，再只读挂载状态文件。Xvfb 解决容器内有头 Chromium 显示环境，不是风控绕过；任何验证码、访问限制或异常结果仍会安全停止。

## 最终结论

Goal 4 的全部硬验收标准均有实际构建、启动、迁移、HTTP 和真实任务证据支持，最终状态 `PASSED`。
