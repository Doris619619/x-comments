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

商城读取（Goal 1）
  → Next.js 服务器端 HTTP
  → GET /api/v1/items 或 /api/v1/items/{item_id}
  → app/api → repositories → 数据库

杂货铺定时采集（Goal 4）
  → catalog_keywords 持久化搜索清单
  → app/jobs/scheduler 每 10 分钟选择一个到期词
  → app/jobs/worker 单队列 Playwright 采集
  → items / keywords / item_keywords
```

- `app/api/`：FastAPI 路由，不包含 SQL 或页面操作。
- `app/services/`：业务用例，不依赖 FastAPI。
- `app/repositories/`：唯一数据库查询入口。
- `app/models/`：ORM 结构，不包含业务流程。
- `app/crawler/parser.py`：纯 JSON 解析器，可使用 fixture 离线测试。
- `app/static/`：无构建链内部演示页，只消费本项目 REST API。
- `app/core/`：环境配置、引擎和会话。
- `app/jobs/scheduler.py`：按全局安全间隔轮流选择一个到期搜索词；不并发采集。
- `catalog_keywords`：杂货铺分类与持久化搜索清单，当前有潮玩手办、实用小物、怀旧收藏三个首页分类，共 18 个搜索词。
- `alembic/`：数据库结构版本。
- 商城不读取本服务数据库，也不从浏览器直接调用本服务；跨服务边界固定为只读 HTTP API。

## 数据流

`POST /api/v1/crawl-jobs` 校验关键词后立即写入 `pending` 任务、加入单 worker 队列并返回 `job_id`。正式应用 lifespan 启动唯一 worker；测试应用显式禁用 worker，保证离线测试不访问闲鱼。

正式应用还会初始化默认杂货铺搜索清单。调度器以 `CATALOG_SCHEDULER_INTERVAL_SECONDS`（默认 600 秒）为全局节奏，每次只为一个到期清单词创建任务，因此五个搜索词不会在同一时刻并发访问闲鱼。每个词的 `last_scheduled_at` 持久化，重启后不会丢失调度记录。

商品解析优先使用页面正常访问触发的搜索响应 JSON。解析器验证响应 `itemId` 与商品 URL `item?id=` 一致，任何不一致记录都不会入库。

## 配置

数据库通过 `DATABASE_URL` 配置。本地默认 `sqlite:///./data/app.sqlite3`；PostgreSQL 可使用 `postgresql+psycopg://...`，依赖已预留。配置示例见 `.env.example`。

当前 API 不启用 CORS。若未来确有浏览器直连需求，必须新增显式允许源配置及自动化测试，不能使用通配符。

登录态路径由 `XIANYU_STORAGE_STATE_PATH` 配置。`storage_state.json`、`state/` 和 `*.storage_state.json` 已加入 `.gitignore`，应用日志和数据库都不得保存其内容。

## 安全边界

项目只访问公开商品搜索页面，不采集私聊、手机号、精确地址或其他非公开个人信息。验证码、登录失效、403/429、访问频繁和结构异常必须停止，禁止绕过、代理轮换或多账号续爬。

## 容器运行

Docker Compose 使用官方 Playwright Python 镜像和 `app_data` named volume。入口在空库执行 Alembic 后显式启动 Xvfb，并以 `DISPLAY=:99` 运行有头 Chromium；Uvicorn 通过 `exec` 成为 PID 1。登录态由宿主机只读挂载，不写入镜像或 volume。
