# 闲鱼关键词商品采集闭环 POC

这是一个内部验证项目，用于打通：

```text
输入关键词 → 创建异步任务 → Playwright 有限搜索 → 清洗去重入库
→ REST API → POC 页面展示真实商品
```

当前默认关键词为“女生发饰”。项目不保证全量覆盖，不实现每 5 分钟调度、商品售出/下架复查、多账号、代理池、验证码处理或正式用户前端。

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
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
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
3. 创建项目专属 `app_data` named volume 持久化 SQLite；
4. 启动时自动执行 `alembic upgrade head`；
5. 在 `0.0.0.0:8000` 启动 API 和 POC 页面。

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

完整请求/响应见 [docs/api.md](docs/api.md)。

## 测试与静态检查

```bash
python -m pytest -q
python -m ruff check app tests alembic scripts
python -m mypy app
```

自动测试使用内存 SQLite 和脱敏 fixture，不访问真实闲鱼。

## 数据和限制

数据模型见 [docs/data-model.md](docs/data-model.md)，架构见 [docs/architecture.md](docs/architecture.md)，风险与限制见 [docs/known-limitations.md](docs/known-limitations.md)。

当前真实验收只覆盖最多 3 页或 50 条商品。搜索结果未出现不能证明商品已售出或下架；本 POC 不会据此删除或隐藏历史商品。
