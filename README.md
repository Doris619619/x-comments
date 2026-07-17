# 闲鱼关键词商品采集闭环 POC

这是一个内部验证项目，用于打通：

```text
输入关键词 → 创建异步任务 → Playwright 有限搜索 → 清洗去重入库
→ REST API → POC 页面展示真实商品
```

当前默认关键词为“女生发饰”。项目不保证全量覆盖；定时采集每 10 分钟只调度一个到期清单词，
不实现多账号、代理池、验证码处理或正式用户前端。

## 安全规则

- 账号只允许开发者在本机可见浏览器中人工登录；
- 不要把账号密码、验证码、Cookie、Token 或登录态发送给 Codex/他人；
- `storage_state.json`、`.env`、`state/` 已被 Git 和 Docker 构建忽略；
- 验证码、登录失效、403/429、访问频繁、非法访问或异常空结果会立即停止，禁止自动重试或绕过；
- 只采集公开商品标题、价格、主图、链接和公开地区，不采集私聊、手机号或精确地址。

## 本地开发

要求 Python 3.11+。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

复制配置：

```bash
cp .env.example .env
```

本阶段运行时统一使用 PostgreSQL。请在 `.env` 中设置 `POSTGRES_PASSWORD`，并使
`DATABASE_URL` 指向 PostgreSQL；不得继续使用旧的 SQLite URL。真实 `.env`、数据库密码和
登录态均不得提交。

当前闲鱼会拦截 Playwright 无头模式，真实任务需设置：

```text
XIANYU_HEADLESS=false
```

### 人工登录

```bash
python -m scripts.login_xianyu
```

在打开的浏览器中自行登录并确认搜索正常，然后回到终端按 Enter。脚本只在本地生成 `storage_state.json`。

### 迁移和启动

```bash
python -m alembic upgrade head
# 终端 A：只提供 API，不启动 Playwright
APP_ROLE=api python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 终端 B：唯一的 scheduler-worker，负责持久化任务和 10 分钟调度
APP_ROLE=scheduler_worker python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

访问：

- POC 页面：`http://127.0.0.1:8000/`
- 健康检查：`http://127.0.0.1:8000/health`
- OpenAPI：`http://127.0.0.1:8000/docs`

## Docker Compose

容器内使用 Xvfb 运行有头 Chromium。首次启动前必须先在宿主机按上节完成人工登录，确保项目根目录有被 Git 忽略的 `storage_state.json`。

```bash
docker compose up --build
```

Compose 会：

1. 构建包含 Playwright Chromium 的镜像；
2. 把本地 `storage_state.json` 只读挂载进容器；
3. 启动 PostgreSQL 16，并以 `postgres_data` named volume 持久化数据库；
4. 由一次性 `migrate` 服务执行 `alembic upgrade head`，成功后 API 和 worker 才启动；
5. 在 `127.0.0.1:8000` 启动仅 API 服务；同机 shopping 的独立同步容器通过共享的 Docker 私有网络，以
   `x-comments-api:8000` 服务名同步目录，端口不直接暴露到公网；
6. 启动恰好一个不暴露端口的 `scheduler-worker`，负责 Playwright 与 10 分钟调度。

停止：

```bash
docker compose down
```

需要明确删除容器数据库并从空库重新验收时，先确认不再需要其中数据，再运行 `docker compose down -v`。

若宿主机登录状态过期，停止容器、重新运行人工登录脚本，再启动容器。不要把登录态复制进镜像。

## API

- `GET /health`
- `POST /api/v1/crawl-jobs`
- `GET /api/v1/crawl-jobs/{job_id}`
- `GET /api/v1/items`
- `GET /api/v1/items/{item_id}`
- `GET /api/v1/catalog-sync/revisions/latest`（shopping 服务端 Bearer 认证）
- `GET /api/v1/catalog-sync/changes`（shopping 增量同步）
- `GET /api/v1/catalog-sync/items`（游标失效后的分页全量重建）

完整请求/响应见 [docs/api.md](docs/api.md)。

## 测试与静态检查

```bash
python -m pytest -q
python -m ruff check app tests alembic scripts
python -m mypy app
python -m scripts.check_chinese_docstrings
```

单元测试使用隔离 ORM 数据库和脱敏 fixture，不访问真实闲鱼；`pytest -m postgresql` 是仅在
PostgreSQL 已迁移的集成环境运行的并发约束验证。Docker 引擎未运行时无法完成容器级
PostgreSQL 验收。

## 数据和限制

数据模型见 [docs/data-model.md](docs/data-model.md)，架构见 [docs/architecture.md](docs/architecture.md)，风险与限制见 [docs/known-limitations.md](docs/known-limitations.md)，云端备份和健康检查见 [docs/operations.md](docs/operations.md)。

当前真实验收只覆盖最多 3 页或 50 条商品。搜索结果未出现不能直接证明商品已售出或下架：
仅完整成功采集会计入缺失，首次缺失为 `suspected_missing`，连续两次完整缺失才为
`off_shelf`。shopping 必须通过 Catalog Sync revision 同步，不得因网络失败或同步失败批量下架。
