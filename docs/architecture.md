# 项目架构

## 当前实现范围

当前代码保持 POC 的有限 Playwright 采集，并增加 Goal Merge 2 所需的 PostgreSQL Catalog
发布与商城同步边界。真实云端验证仍需由持有登录态和数据库权限的部署者执行，不能声明采集成功。

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

商城同步（Goal Merge 2）
  → shopping 服务器端每 10 分钟经同机回环地址调用带 Bearer 认证的 Catalog Sync API
  → GET /api/v1/catalog-sync/changes?after_revision=...
  → shopping 自己的商品镜像和持久化游标
  → app/api → repositories → x-comments PostgreSQL

商城结算核验
  → Next.js 服务器携带 Bearer 令牌
  → POST /api/v1/items/{item_id}/verify
  → app/api 鉴权 → app/services 存在性与价格比较
  → app/crawler 单次详情访问 → 五状态失败关闭响应

杂货铺定时采集（Goal 4）
  → catalog_keywords 持久化搜索清单
  → app/jobs/scheduler 每 10 分钟选择一个到期词
  → app/jobs/worker 单队列 Playwright 采集
  → crawl_runs / 商品-清单状态 / catalog_revisions / catalog_changes
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
- `app/repositories/catalog_sync.py`：在完整成功采集时，将商品、商品-清单状态和 revision
  合并为一个短事务；部分成功、风控和失败不会发布 revision。
- `app/api/catalog_sync.py`：只供 shopping 服务器端使用的 Bearer 认证增量、全量重建和单商品
  快照 API；不返回闲鱼原始链接或登录态。
- `alembic/`：数据库结构版本。
- 商城不读取本服务数据库，也不从浏览器直接调用本服务；跨服务边界固定为只读 HTTP API。
- `app/services/item_verification.py`：只依赖商品存在性协议和实时核验协议，不依赖 ORM 模型。
- `app/crawler/item_verifier.py`：单次详情身份、风险、不可售文案和当前价格核验；不写数据库。

## 数据流

`POST /api/v1/crawl-jobs` 校验关键词后立即写入 `pending` 任务并返回 `job_id`。API 角色不在内存中
持有 worker；唯一 `scheduler_worker` 角色会轮询持久化 pending 任务并原子认领，因此 API、worker
可独立部署且重启不丢任务。测试应用显式禁用 worker，保证离线测试不访问闲鱼。

`scheduler_worker` 会初始化默认杂货铺搜索清单。调度器以 `CATALOG_SCHEDULER_INTERVAL_SECONDS`
（默认 600 秒）为全局节奏，每次只为一个到期清单词创建任务，因此清单内多个词不会
并发访问闲鱼。`next_due_at` 和未完成任务关键词均会被检查；PostgreSQL 的部分唯一索引
额外阻止同一关键词存在两条 pending/running 任务。

完整成功采集时，worker 在不持有网页访问事务的前提下，以短事务依次写入商品、关键词关联、
`catalog_item_states`、`crawl_runs`、`catalog_revisions` 和 `catalog_changes`。首次缺失标为
`suspected_missing`，连续 `CATALOG_MISSING_THRESHOLD`（默认 2）次完整缺失后才标为
`off_shelf`；部分成功、风控和失败只保留本轮确实看到的商品，不进行缺失判断。首批清单控制在
3 至 5 个词内，单轮上限保持 50 条；N 个持续到期词时，单个词约每 `N × 10` 分钟轮询一次。

商品解析优先使用页面正常访问触发的搜索响应 JSON。解析器验证响应 `itemId` 与商品 URL `item?id=` 一致，任何不一致记录都不会入库。

## 配置

运行时数据库通过 `DATABASE_URL` 配置，目标为 PostgreSQL，例如
`postgresql+psycopg://USER:PASSWORD@HOST:5432/x_comments`。Docker Compose 启动独立
PostgreSQL 容器并使用 `postgres_data` volume。配置示例见 `.env.example`；真实 `.env` 必须由
部署者自行更新，不能提交密码。

当前 API 不启用 CORS。若未来确有浏览器直连需求，必须新增显式允许源配置及自动化测试，不能使用通配符。

登录态路径由 `XIANYU_STORAGE_STATE_PATH` 配置。`storage_state.json`、`state/` 和 `*.storage_state.json` 已加入 `.gitignore`，应用日志和数据库都不得保存其内容。

结算核验超时由 `XIANYU_VERIFY_TIMEOUT_SECONDS` 配置，默认 12 秒，必须短于商城侧 HTTP
超时。`XIANYU_API_TOKEN` 是商城服务器与本服务共享的至少 32 字符随机令牌；未配置时核验接口返回
503，不允许匿名降级。`CATALOG_SYNC_TOKEN` 是单独的只读同步令牌；示例值只在
`.env.example`，真实值不得提交。当前两服务部署在同一台云服务器：Catalog Sync 仅绑定宿主机
回环地址；若未来拆分服务器，才必须改为 HTTPS、来源限制和服务间认证。`APP_ROLE` 只能为 `api` 或 `scheduler_worker`：云端可扩容 API，
但只能部署一个 scheduler-worker；该 worker 同时持有唯一 Playwright 队列和定时调度职责。

## 安全边界

项目只访问公开商品搜索页面，不采集私聊、手机号、精确地址或其他非公开个人信息。验证码、登录失效、403/429、访问频繁和结构异常必须停止，禁止绕过、代理轮换或多账号续爬。

正式应用为同一进程的采集 worker 与结算核验器注入同一个 `asyncio.Lock`。等待锁也计入各自
安全超时，因此同一登录态不会在定时采集与并发结算中被同时使用。核验器每次只导航一次，
不会因超时、未知结构或风控自动重试。

## 容器运行

Docker Compose 使用官方 Playwright Python 镜像和 PostgreSQL `postgres_data` named volume。一次性
`migrate` 服务在空 PostgreSQL 执行 Alembic；成功后 `api` 与唯一 `scheduler-worker` 才启动。后者
显式启动 Xvfb，并以 `DISPLAY=:99` 运行有头 Chromium。入口脚本会同时监管 Xvfb 与 Uvicorn；容器
停止时会向两者发送结束信号并等待退出，避免遗留显示锁影响下一次启动。登录态由
宿主机只读挂载，不写入镜像或 volume。
