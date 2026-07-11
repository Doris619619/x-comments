# 项目架构

## 当前实现范围

当前代码完成 Goal 1 离线后端骨架，并已接入 Goal 2 单 worker 和 Playwright 有限采集。真实验证因闲鱼“非法访问”及异常空结果而阻塞，不能声明采集成功。

## 模块边界

```text
HTTP 请求
  → app/api          参数与协议边界
  → app/services     业务用例
  → app/repositories 数据访问
  → app/models       SQLAlchemy 映射
  → 数据库

真实采集（Goal 2）
  → app/jobs         单 worker 与任务状态
  → app/crawler      Playwright、风险识别、纯解析
  → app/services / repositories
```

- `app/api/`：FastAPI 路由，不包含 SQL 或页面操作。
- `app/services/`：业务用例，不依赖 FastAPI。
- `app/repositories/`：唯一数据库查询入口。
- `app/models/`：ORM 结构，不包含业务流程。
- `app/crawler/parser.py`：纯 JSON 解析器，可使用 fixture 离线测试。
- `app/static/`：无构建链内部演示页，只消费本项目 REST API。
- `app/core/`：环境配置、引擎和会话。
- `alembic/`：数据库结构版本。

## 数据流

`POST /api/v1/crawl-jobs` 校验关键词后立即写入 `pending` 任务、加入单 worker 队列并返回 `job_id`。正式应用 lifespan 启动唯一 worker；测试应用显式禁用 worker，保证离线测试不访问闲鱼。

商品解析优先使用页面正常访问触发的搜索响应 JSON。解析器验证响应 `itemId` 与商品 URL `item?id=` 一致，任何不一致记录都不会入库。

## 配置

数据库通过 `DATABASE_URL` 配置。本地默认 `sqlite:///./data/app.sqlite3`；PostgreSQL 可使用 `postgresql+psycopg://...`，依赖已预留。配置示例见 `.env.example`。

登录态路径由 `XIANYU_STORAGE_STATE_PATH` 配置。`storage_state.json`、`state/` 和 `*.storage_state.json` 已加入 `.gitignore`，应用日志和数据库都不得保存其内容。

## 安全边界

项目只访问公开商品搜索页面，不采集私聊、手机号、精确地址或其他非公开个人信息。验证码、登录失效、403/429、访问频繁和结构异常必须停止，禁止绕过、代理轮换或多账号续爬。
