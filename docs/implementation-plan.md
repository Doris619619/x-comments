# 闲鱼关键词采集 POC 实施方案

## 1. 范围

本方案只定义后续 Goal 1 至 Goal 4 的实现路径。本次 Goal 0 不创建应用代码。

POC 只打通“关键词 → 异步任务 → 有限范围 Playwright 搜索 → 清洗去重入库 → REST API → 内部演示页”。不实现定时调度、多账号、代理池、验证码处理、状态复查、AI 选品或正式前端。

## 2. 技术选型

- Python 3.11+
- FastAPI + Pydantic 2
- SQLAlchemy 2 + Alembic
- SQLite（本地）/ PostgreSQL（生产配置预留）
- Playwright Chromium
- pytest、pytest-asyncio、httpx
- 原生 HTML/CSS/JavaScript 内部演示页，避免引入前端构建链
- Docker Compose 单应用容器；SQLite 文件通过 volume 持久化

不使用 Redis、Celery、Kafka 或 WebSocket。首版采用进程内 `asyncio.Queue` 和单 worker，确保同一账号永远只有一个采集任务运行。应用重启后，遗留 `pending/running` 任务应明确标记失败或重新等待人工触发，不伪装成功。

## 3. 建议目录结构

```text
app/
  __init__.py
  main.py                    # FastAPI 装配与 lifespan，不承载业务规则
  api/
    dependencies.py
    health.py
    crawl_jobs.py
    items.py
    demo.py
  core/
    config.py
    logging.py
    exceptions.py
  models/
    base.py
    item.py
    keyword.py
    crawl_job.py
  schemas/
    crawl_job.py
    item.py
    pagination.py
  repositories/
    protocols.py             # 业务层依赖的接口
    sqlalchemy_items.py
    sqlalchemy_jobs.py
  services/
    crawl_job_service.py
    item_service.py
    item_normalizer.py
  jobs/
    queue.py
    worker.py
  crawler/
    client.py                # Playwright 页面生命周期
    parser.py                # 纯解析器，可离线测试
    selectors.py             # 唯一选择器来源
    auth.py                  # 人工登录与状态校验
    risk_control.py
  static/
    index.html
    app.js
    styles.css
alembic/
tests/
  fixtures/
  unit/
  integration/
docs/
scripts/
  login_xianyu.py
```

所有 Python/JavaScript 源文件按 `AGENTS.md` 添加中文文件说明和函数/类 Docstring；选择器只允许存在于 `crawler/selectors.py`。

## 4. 数据模型

### 4.1 items

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| item_id | String，主键 | 闲鱼商品 ID，不允许占位值 |
| title | Text，非空 | 商品标题 |
| price | Numeric(12,2)，非空 | 当前价格 |
| image_url | Text，可空 | 页面真实主图，缺失不伪造 |
| item_url | Text，非空 | 规范化闲鱼链接 |
| location | String，可空 | 页面可获得时保存 |
| source | String，默认 `xianyu` | 数据来源 |
| first_seen_at | DateTime(tz)，非空 | 首次发现 |
| last_seen_at | DateTime(tz)，非空 | 最近发现 |
| created_at/updated_at | DateTime(tz) | 审计时间 |

### 4.2 keywords 与 item_keywords

`keywords(id, normalized_value UNIQUE, display_value, created_at)`；`item_keywords(item_id, keyword_id, first_seen_at, last_seen_at, PRIMARY KEY(item_id, keyword_id))`。同一商品可关联多个关键词。

### 4.3 crawl_jobs

`job_id` 使用 UUID 主键；包含 Goal 指定的全部时间、计数、错误字段。状态枚举：`pending`、`running`、`succeeded`、`partially_succeeded`、`failed`、`blocked_by_auth_or_risk_control`。

状态转换集中在服务层，并记录日志。登录/风控阻塞不得映射成成功或普通空结果。

## 5. API

- `GET /health`：进程与数据库健康状态。
- `POST /api/v1/crawl-jobs`：校验关键词，事务内创建 `pending` 任务并入队，立即返回 `202`、`job_id` 和状态。
- `GET /api/v1/crawl-jobs/{job_id}`：返回状态、统计、错误和时间。
- `GET /api/v1/items?page=1&page_size=20&keyword=女生发饰`：数据库分页和关键词过滤，稳定排序。
- `GET /api/v1/items/{item_id}`：商品详情和关键词关联。
- `GET /`：POC internal demo 页面（Goal 3）。

API 路由只做输入输出、依赖获取和异常映射；业务规则由 service 实现，数据库访问由 repository 实现。

## 6. Playwright 执行入口

### 6.1 人工登录

```text
python scripts/login_xianyu.py
```

脚本以有头 Chromium 打开 `https://www.goofish.com/`。用户自行扫码/登录；脚本只在用户确认后调用 `context.storage_state(path=配置路径)`。不提示用户向 Codex 或服务端提供任何凭据。

默认状态路径 `state/xianyu.storage_state.json`，通过 `XIANYU_STORAGE_STATE_PATH` 配置，并加入 `.gitignore`：`.env`、`state/`、`playwright/.auth/`、`*.storage_state.json`。

### 6.2 采集

单 worker 取得任务后：

1. 原子转换 `pending → running`；
2. 检查状态文件存在且权限可读；
3. `async_playwright()` 启动 Chromium，`new_context(storage_state=...)`；
4. 打开 `https://www.goofish.com/search?q=<urlencoded keyword>`；
5. 在导航前注册搜索响应监听，捕获 `mtop.taobao.idlemtopsearch.pc.search`；
6. 验证 URL、标题、登录状态、风险信号和返回关键词相关性；
7. 按 `max_pages=3` 有限翻页；若页码机制不可用，在文档中切换为 `max_items=50`，两者不会同时无限增长；
8. 解析、标准化、事务 upsert；
9. 更新任务统计和终态；
10. 无论成功失败都关闭 page/context/browser。

首版不并发执行多个账号或同账号任务，不对 403/429、验证码、登录失效、结果异常进行自动重试。

## 7. 解析与去重

解析器输入是搜索响应 JSON，输出 `ParsedItem`，不依赖 Page，便于 fixture 测试。字段优先路径：

```text
item_id: data.item.main.exContent.itemId
title: data.item.main.exContent.title
price: data.item.main.exContent.price[*].text
image_url: data.item.main.exContent.picUrl
item_url: data.item.main.targetUrl
location: data.item.main.exContent.area
```

ID 再与 `item?id=` 交叉校验。数据库以 `items.item_id` 唯一；upsert 已有商品时更新字段、`last_seen_at` 和关键词关联，保留 `first_seen_at`。任务统计定义：

- `discovered_count`：本轮解析出的有效记录数（含重复）；
- `new_count`：首次插入商品数；
- `updated_count`：已有商品字段或 `last_seen_at` 被更新的数；
- `duplicate_count`：本轮内重复或已存在但无业务字段变化的数；
- `error_count`：被拒绝或保存失败的记录/阶段数。

## 8. 风控与失败边界

以下任一信号立即安全停止，状态为 `blocked_by_auth_or_risk_control`，保留已确认写入的数据，不重试：

- 登录页、`passport.goofish.com` 或 `mini_login`；
- 验证码、安全验证、访问频繁、账号异常、操作受限；
- HTTP 403/429；
- 搜索结果异常归零或明显与关键词不符；
- 连续请求失败；
- 关键响应结构缺失，无法确认数据正确。

日志只记录分类后的原因、任务 ID 和阶段，不记录 Cookie、Token、storage state 内容、请求敏感头或个人非公开信息。

## 9. 分 Goal 验收计划

### Goal 1

实现骨架、Alembic、SQLite/PostgreSQL 配置、模型、repository/service/API、纯解析器和脱敏 fixture。自动测试覆盖迁移、唯一约束、多关键词关联、立即返回、分页过滤、OpenAPI 与健康检查。Goal 1 不访问闲鱼，不宣称执行真实采集。

### Goal 2

用户在本机人工登录后执行两次真实“女生发饰”任务，验证至少 20 条或配置范围全部可见商品、第二次不重复插入且更新 `last_seen_at`。随机抽查 10 条标题/价格/主图/链接/ID，全部正确；报告写入 `docs/live-test-report.md`。任何风控信号立即阻塞。

### Goal 3

实现无硬编码商品的内部演示页；轮询设最大持续时间和明确失败 UI，成功后从数据库 API 加载商品。

### Goal 4

容器从空库执行迁移并启动 API/页面；在容器环境复核真实采集。若人工有头登录必须在宿主机执行，README 明确状态文件挂载步骤。

## 10. Goal 1 自动验收命令（计划）

```bash
alembic upgrade head
pytest
ruff check app tests
mypy app
python -m scripts.check_chinese_docstrings
```

另行人工打开 `/docs` 和 `/health`。测试只使用脱敏 fixture，不访问闲鱼网络。

## 11. 已知限制

- 当前未登录页面可能返回与关键词不匹配的公共推荐，不能仅凭 HTTP 200 判成功。
- 闲鱼响应字段和 CSS class 可能变化，必须以 fixture 回归和真实抽查共同验证。
- storage state 会过期，需要人工重新登录。
- SQLite 适合 POC，生产 PostgreSQL 仅预留连接和迁移兼容性。
- 进程内队列不提供分布式可靠投递；这是首版明确取舍。
- POC 不保证全量覆盖，不实现五分钟调度和商品下架复查。
