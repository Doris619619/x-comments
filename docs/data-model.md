# 数据库设计文档

> 数据来源以当前代码为准：`app/models/*`、`alembic/versions/20260711_0001_initial.py`、`app/core/database.py`、`app/core/config.py`。  
> 若本文与代码冲突，以代码和迁移为准。

> 运行时引擎：**PostgreSQL**（本地 Compose、CI 集成环境和云端目标）
> ORM：**SQLAlchemy 2.x（同步）**，`create_engine` + `sessionmaker`；**不是**异步，**未使用** `asyncpg`  
> 迁移工具：**Alembic**（初始修订：`20260711_0001`）  
> 连接配置：环境变量 `DATABASE_URL`（见 `.env.example`）

依赖约束（`pyproject.toml`）：

| 组件 | 约束 |
|------|------|
| SQLAlchemy | `>=2.0,<3` |
| Alembic | `>=1.16,<2` |
| PostgreSQL 驱动 | `psycopg[binary]>=3.2,<4`（同步，`postgresql+psycopg://...`） |

---

## 一、引擎与字符集

### 1.1 实际使用方式

| 场景 | `DATABASE_URL` | 说明 |
|------|----------------|------|
| 本地直接运行 | `postgresql+psycopg://xcomments:...@127.0.0.1:5432/x_comments` | `Settings.database_url` 默认格式 |
| Docker Compose | `postgresql+psycopg://xcomments:...@postgres:5432/x_comments` | 数据落在 named volume `postgres_data` |
| 云端托管 PostgreSQL | `postgresql+psycopg://USER:PASSWORD@HOST:5432/x_comments` | x-comments 独占数据库和迁移 |

应用层使用**同步**会话：`app.core.database.build_engine` → `SessionFactory` → `get_db()`，并开启 `pool_pre_ping=True`。

### 1.2 字符集

- **PostgreSQL**：使用库编码 `UTF8` 即可覆盖中文标题等完整 Unicode；项目没有 MySQL 式 `utf8mb4` 声明。

当前 `docker-compose.yml` 运行 `postgres:16-alpine`、一次性 `migrate`、可横向扩容的 `api` 和唯一
`scheduler-worker`；后两个服务仅在迁移成功后启动。`APP_ROLE=api` 不运行 Playwright，
`APP_ROLE=scheduler_worker` 才运行定时采集和持久化任务轮询。

### 1.3 连接串示例

```text
# 本地/云端 PostgreSQL
postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME
```

---

## 二、ER 关系概览

```mermaid
erDiagram
    keywords ||--o{ item_keywords : "keyword_id"
    items ||--o{ item_keywords : "item_id"
    crawl_jobs {
        string job_id PK
        string keyword
        string status
    }
    items {
        string item_id PK
        text title
        numeric price
    }
    keywords {
        int id PK
        string normalized_value UK
        string display_value
    }
    item_keywords {
        string item_id PK_FK
        int keyword_id PK_FK
    }
```

说明：

- `crawl_jobs` **独立**，与商品表**无外键**。
- 商品按 `item_id` **全局去重**；同一商品可通过 `item_keywords` 关联多个关键词。
- ORM 关系上，`Item.keyword_links` / `Keyword.item_links` 配置了 `cascade="all, delete-orphan"`（**ORM 级**级联删除）。迁移中的外键**未**声明 `ON DELETE CASCADE`，数据库默认删除策略为 **NO ACTION**（需先删关联行，或由 ORM 级联处理）。

---

## 三、任务状态取值（非 PostgreSQL ENUM）

`CrawlJobStatus` 定义在 `app.models.crawl_job.CrawlJobStatus`。

ORM：`Enum(CrawlJobStatus, native_enum=False)` → **不创建**数据库原生 ENUM 类型。  
迁移物理列：`status VARCHAR/String(32)`，取值由应用层枚举约束。

| 枚举成员 | 存库字符串 | 含义 |
|----------|------------|------|
| `PENDING` | `pending` | 已创建，等待 worker |
| `RUNNING` | `running` | 正在采集 |
| `SUCCEEDED` | `succeeded` | 成功完成 |
| `PARTIALLY_SUCCEEDED` | `partially_succeeded` | 部分成功 |
| `FAILED` | `failed` | 失败 |
| `BLOCKED` | `blocked_by_auth_or_risk_control` | 登录失效或风控，安全停止 |

---

## 四、表结构明细

以下类型为迁移中的 SQLAlchemy 类型；运行时由 PostgreSQL 方言映射为对应物理类型。

时间列均为 `DateTime(timezone=True)`（带时区）。ORM 默认值函数为 `utc_now()`（`datetime.now().astimezone()`），**迁移未**为时间列设置 `server_default`（计数列除外）。

---

### 4.1 `crawl_jobs` — 采集任务表

**表备注**：一次 `POST /api/v1/crawl-jobs` 对应一行；保存关键词、状态、统计与安全错误信息。

| 字段名 | 数据类型 | 空值 | 默认值 | 字段备注 |
|--------|----------|------|--------|----------|
| `job_id` | `String(36)` | NOT NULL | ORM：`str(uuid.uuid4())` | 主键，UUID 字符串 |
| `keyword` | `String(100)` | NOT NULL | — | 用户展示关键词（创建时经 schema 空白折叠） |
| `status` | `String(32)` | NOT NULL | ORM：`pending` | 见第三节；迁移无 `server_default` |
| `created_at` | `DateTime(timezone=True)` | NOT NULL | ORM：`utc_now()` | 创建时间 |
| `started_at` | `DateTime(timezone=True)` | NULL | — | 开始执行时间 |
| `finished_at` | `DateTime(timezone=True)` | NULL | — | 结束时间 |
| `discovered_count` | `Integer` | NOT NULL | `server_default='0'` | 本轮发现条数 |
| `new_count` | `Integer` | NOT NULL | `server_default='0'` | 新入库条数 |
| `updated_count` | `Integer` | NOT NULL | `server_default='0'` | 已存在并更新条数 |
| `duplicate_count` | `Integer` | NOT NULL | `server_default='0'` | 重复条数 |
| `error_count` | `Integer` | NOT NULL | `server_default='0'` | 错误条数 |
| `error_message` | `Text` | NULL | — | 安全/失败说明 |

**主键**

| 名称（惯例） | 列 |
|--------------|-----|
| `crawl_jobs` PK | `job_id` |

**索引**（迁移显式创建）

| 索引名 | 类型 | 列 | 说明 |
|--------|------|-----|------|
| `ix_crawl_jobs_keyword` | B-tree | `keyword` | 对应 ORM `index=True` |
| `ix_crawl_jobs_status` | B-tree | `status` | 对应 ORM `index=True` |

**业务说明**

- 无用户表、无任务归属外键；当前 POC **无登录鉴权**。
- 风控停止时 `status` 为 `blocked_by_auth_or_risk_control`，原因写入 `error_message`。

---

### 4.2 `items` — 商品表

**表备注**：以闲鱼商品 ID 全局唯一的公开商品；重复采集更新同一行并推进 `last_seen_at`。

| 字段名 | 数据类型 | 空值 | 默认值 | 字段备注 |
|--------|----------|------|--------|----------|
| `item_id` | `String(64)` | NOT NULL | — | 主键；闲鱼商品唯一 ID |
| `title` | `Text` | NOT NULL | — | 标题 |
| `price` | `Numeric(12, 2)` | NOT NULL | — | 价格，避免浮点误差 |
| `image_url` | `Text` | NULL | — | 兼容旧调用方的图库首图；缺失为 `NULL` |
| `image_urls` | `JSON` | NOT NULL | `[]` | 最多九张的详情公开图库；旧数据由首图回填 |
| `item_url` | `Text` | NOT NULL | — | 商品链接 |
| `location` | `String(100)` | NULL | — | 公开地区；缺失为 `NULL` |
| `source` | `String(32)` | NOT NULL | `server_default='xianyu'`；ORM 默认 `"xianyu"` | 来源 |
| `first_seen_at` | `DateTime(timezone=True)` | NOT NULL | ORM：`utc_now()` | 首次发现 |
| `last_seen_at` | `DateTime(timezone=True)` | NOT NULL | ORM：`utc_now()` | 最近发现；列表排序键之一 |
| `created_at` | `DateTime(timezone=True)` | NOT NULL | ORM：`utc_now()` | 行创建时间 |
| `updated_at` | `DateTime(timezone=True)` | NOT NULL | ORM：`utc_now()`，`onupdate=utc_now` | 行更新时间 |

**主键**

| 列 |
|----|
| `item_id` |

**索引**（迁移显式创建）

| 索引名 | 类型 | 列 | 说明 |
|--------|------|-----|------|
| `ix_items_last_seen_at` | B-tree | `last_seen_at` | 对应 ORM `index=True`；列表按 `last_seen_at DESC, item_id ASC` 排序 |

**业务说明**

- 写入入口：`ItemRepository.upsert_many`（按 `item_id` 插入或更新字段，并维护关键词关联）。
- 解析层 `ParsedItem.item_id` 约束为数字字符串（`^\d+$`）；库表本身为 `String(64)`，不在 DB 层强制正则。

---

### 4.3 `keywords` — 关键词表

**表备注**：规范化关键词唯一；另存展示值。

| 字段名 | 数据类型 | 空值 | 默认值 | 字段备注 |
|--------|----------|------|--------|----------|
| `id` | `Integer` | NOT NULL | 自增 | 主键 |
| `normalized_value` | `String(100)` | NOT NULL | — | 规范化值，全局唯一 |
| `display_value` | `String(100)` | NOT NULL | — | 展示用原文（创建关联时传入的关键词） |
| `created_at` | `DateTime(timezone=True)` | NOT NULL | ORM：`utc_now()` | 创建时间 |

**主键**

| 列 |
|----|
| `id` |

**唯一约束 / 索引**

| 名称 | 列 | 说明 |
|------|-----|------|
| 列级 `unique=True`（迁移） | `normalized_value` | 唯一约束 |
| `ix_keywords_normalized_value` | `normalized_value` | **唯一**索引（迁移 `unique=True`） |

**业务说明**

- 规范化规则在仓储层：`keyword_value.casefold().strip()`（见 `ItemRepository.upsert_many` / `list_page`）。
- 查询商品列表的 `keyword` 参数按同一规则与 `normalized_value` 匹配。

---

### 4.4 `item_keywords` — 商品–关键词关联表

**表备注**：多对多关联，并记录该关联的首次/最近发现时间。

| 字段名 | 数据类型 | 空值 | 默认值 | 字段备注 |
|--------|----------|------|--------|----------|
| `item_id` | `String(64)` | NOT NULL | — | 复合主键之一；FK → `items.item_id` |
| `keyword_id` | `Integer` | NOT NULL | — | 复合主键之一；FK → `keywords.id` |
| `first_seen_at` | `DateTime(timezone=True)` | NOT NULL | ORM：`utc_now()` | 该关联首次发现 |
| `last_seen_at` | `DateTime(timezone=True)` | NOT NULL | ORM：`utc_now()` | 该关联最近发现 |

**主键**

| 列 |
|----|
| `(item_id, keyword_id)` |

**外键**（迁移声明，未指定 `ondelete`）

| 列 | 引用 |
|----|------|
| `item_id` | `items(item_id)` |
| `keyword_id` | `keywords(id)` |

**额外唯一约束**

迁移与 ORM（`UniqueConstraint("item_id", "keyword_id")`）在复合主键之外又声明了同列唯一约束；与主键语义重复，但**当前代码/迁移确实如此**。

**业务说明**

- 已存在关联时，再次采集只更新 `last_seen_at`。
- 商品本身不存「单一关键词」字段。

---

### 4.5 `catalog_keywords` — 杂货铺采集清单表

保存允许定时采集的分类和搜索词；它与实际发现的 `keywords` 分开，前者是运营配置，后者是商品关联词典。

| 字段名 | 数据类型 | 空值 | 说明 |
|--------|----------|------|------|
| `id` | `Integer` | NOT NULL | 自增主键 |
| `category` | `String(64)` | NOT NULL | 首页分组：潮玩手办、实用小物或怀旧收藏 |
| `keyword` | `String(100)` | NOT NULL | 唯一、实际传给搜索页的词 |
| `is_enabled` | `Boolean` | NOT NULL | 关闭后调度器不会选中 |
| `interval_minutes` | `Integer` | NOT NULL | 同一个词的最小采集间隔，默认 60 分钟 |
| `last_scheduled_at` | `DateTime(timezone=True)` | NULL | 最近入队时间，用于重启后的调度恢复 |
| `last_completed_at` | `DateTime(timezone=True)` | NULL | 最近一次完整成功采集完成时间 |
| `next_due_at` | `DateTime(timezone=True)` | NULL | 下一次允许调度时间；为空时按历史时间兼容计算 |
| `note` | `Text` | NULL | 分类说明 |
| `created_at` / `updated_at` | `DateTime(timezone=True)` | NOT NULL | 配置创建与更新时间 |

---

### 4.6 Catalog Sync 发布表

以下表由 `20260716_0004` 创建。它们只在完整成功采集时与商品写入处于同一个短事务中；
shopping 只能读取 `catalog_revisions.status=published` 的变更，不能直接读取任一业务表。

| 表名 | 核心字段 | 用途 |
|------|----------|------|
| `crawl_runs` | `run_id`、`job_id`、`catalog_keyword_id`、`status`、`is_comparable`、时间与错误信息 | 区分完整成功、部分成功、失败和风控；只有完整成功可比较缺失 |
| `catalog_item_states` | `item_id`、`catalog_keyword_id`、`availability`、`missing_count`、`last_seen_at`、`last_checked_run_id` | 保存商品在单个清单词下的状态，避免多关键词互相误下架 |
| `catalog_revisions` | 自增 `revision`、`source_run_id`、`status`、`published_at` | shopping 的持久化游标边界 |
| `catalog_changes` | `revision`、`item_id`、`change_type`、`availability`、展示字段快照 | 可幂等应用的跨服务增量；不保存 `item_url` 或登录态 |

`availability`：`active`、`suspected_missing`、`sold`、`off_shelf`、`unknown`。
首次完整缺失转为 `suspected_missing`；连续达到 `CATALOG_MISSING_THRESHOLD` 后转为
`off_shelf`。任何部分成功、失败或风控运行都不会增加 `missing_count`。

---

### 4.7 采购聊天领域表

迁移 `20260720_0006` 增加以下五表，`20260723_0008` 再补充 v2 任务授权快照、Canary 模式、
卖家轮询次数和单商品活动任务唯一索引。真实发送默认关闭，离线编排会把已验证草稿与策略结果写入
消息、审计和 Outbox。

| 表名 | 核心字段 | 约束与用途 |
|------|----------|------------|
| `procurement_execution_tasks` | `task_id`、`contract_version`、`execution_mode`、任务级自动发送授权快照、`source_item_id`、标题/CNY 整数分快照、目标、轮次/期限、请求幂等键/body 哈希、状态/下一动作、租约、摘要/原因与时间 | `task_id` 主键、请求幂等键唯一；价格非负、轮次 1–3；同一商品只能有一个活动任务；授权只允许聊天发送，不代表购买或付款 |
| `conversation_sessions` | `session_id`、`task_id`、`source_item_id`、卖家快照、账号别名、`status`、`round_count`、`seller_poll_attempt_count`、`event_seq`、租约与时间 | `task_id` 唯一；只保存商城任务 UUID，不保存客户资料、Cookie 或密码 |
| `conversation_messages` | 会话内 `seq`、方向、角色、正文、正文哈希、回复关系、LLM/策略字段、发送时间 | `(session_id, seq)`、`(session_id, external_message_id)` 和 `idempotency_key` 唯一；一条出站消息从草稿演进至发送结果，不重复建行 |
| `procurement_audit_logs` | 任务/会话/消息关联、actor、动作、前后状态、原因、脱敏元数据、关联与幂等键 | 只追加；同任务、幂等键和动作不得重复；普通日志不得记录消息正文 |
| `procurement_outbox` | `event_id`、任务内 `event_seq`、事件类型、JSON payload、投递状态、租约、重试时间 | `event_id`、幂等键及 `(task_id, event_seq)` 唯一；供后续回调商城的事务 Outbox 使用 |

执行模式为 `paid_order` 或 `operator_canary`。任务级自动发送授权必须同时保存
`auto_send_authorized=true`、`authorized_at` 和与模式匹配的 `authorization_source`；POC 测试支付
使用 `operator_canary`，默认不授权自动发送。会话状态为 `pending_open`、`active`、`waiting_seller`、`completed`、
`human_review_required`、`blocked`、`failed` 或 `cancelled`。消息状态区分入站观察/分析、出站草稿/
策略/排队/发送以及 `policy_blocked`、`send_failed`、`superseded`，`sent` 只能表示页面结果已确认。

---

## 五、表清单汇总

| 序号 | 表名 | 中文名 | 核心用途 |
|------|------|--------|----------|
| 1 | `crawl_jobs` | 采集任务表 | 异步任务状态与统计 |
| 2 | `items` | 商品表 | 公开商品全局去重存储 |
| 3 | `keywords` | 关键词表 | 规范化关键词字典 |
| 4 | `item_keywords` | 商品关键词关联表 | 商品与关键词多对多 |
| 5 | `catalog_keywords` | 杂货铺采集清单表 | 分类、搜索词和安全调度时间 |
| 6 | `crawl_runs` | 采集批次表 | 判断本轮是否完整且可比较 |
| 7 | `catalog_item_states` | 商品-清单状态表 | 多关键词下的缺失和可用状态 |
| 8 | `catalog_revisions` | 发布版本表 | shopping 增量游标 |
| 9 | `catalog_changes` | 商品变更表 | shopping 的可幂等同步事件 |
| 10 | `procurement_execution_tasks` | 本地采购执行任务表 | 来源快照、幂等创建、状态与下一动作 |
| 11 | `conversation_sessions` | 采购聊天会话表 | 商城任务与闲鱼会话的一对一执行快照 |
| 12 | `conversation_messages` | 采购聊天消息表 | 入站去重及出站草稿到发送结果的生命周期 |
| 13 | `procurement_audit_logs` | 采购审计表 | 脱敏、追加式状态与安全决策证据 |
| 14 | `procurement_outbox` | 采购事件 Outbox | 与领域变更同事务保存的有序回调事件 |

当前 POC **没有**用户表、额度表或文件元数据表；采购会话表不保存商城用户身份和客户资料。

---

## 六、索引设计说明

| 设计点 | 本库实践（以迁移为准） |
|--------|------------------------|
| 主键 | 原有采集/目录主键，以及采购表的 UUID 主键 |
| 外键列 | `item_keywords` 两列作为复合主键组成部分；未另建单独非唯一索引 |
| 唯一业务键 | `keywords.normalized_value`；采购 `task_id`、消息/事件幂等键和任务内事件序号 |
| 查询加速 | `crawl_jobs.keyword` / `status`；`items.last_seen_at`；采购任务、会话状态和 Outbox 到期时间 |
| 全文检索 | **未实现** |

---

## 七、迁移与版本

| 修订 ID | 文件 | 说明 |
|---------|------|------|
| `20260711_0001` | `alembic/versions/20260711_0001_initial.py` | 创建核心 4 张表及索引；`down_revision = None` |
| `20260715_0002` | `alembic/versions/20260715_0002_catalog_keywords.py` | 创建杂货铺采集清单和五个默认搜索词 |
| `20260715_0003` | `alembic/versions/20260715_0003_expand_catalog_keywords.py` | 归并为三个首页分类，并扩充至 18 个搜索词 |
| `20260716_0004` | `alembic/versions/20260716_0004_catalog_sync.py` | 创建采集批次、缺失状态、revision 和变更事件表 |
| `20260716_0005` | `alembic/versions/20260716_0005_unique_inflight_keyword.py` | 创建 PostgreSQL 同关键词 pending/running 部分唯一索引 |
| `20260720_0006` | `alembic/versions/20260720_0006_procurement_chat_core.py` | 创建本地采购执行任务、聊天会话、消息、审计和事务 Outbox 表 |
| `20260722_0007` | `alembic/versions/20260722_0007_catalog_image_urls.py` | 为商品与 Catalog Sync 快照增加最多九张的详情图库，并由旧首图回填 |
| `20260723_0008` | `alembic/versions/20260723_0008_procurement_authorization_v2.py` | 增加 v2 授权快照、Canary 模式、卖家轮询次数及单商品活动任务唯一索引 |

```bash
alembic upgrade head    # 应用迁移
alembic current         # 查看当前版本
```

Alembic 在线/离线环境读取 `get_settings().database_url`，元数据来自 `Base.metadata`（通过 `import app.models` 注册全部模型）。

迁移已通过 PostgreSQL 离线 SQL 渲染验证。真实 PostgreSQL 升级需要在配置好 `DATABASE_URL` 和
数据库备份后执行 `alembic upgrade head`；不得对生产库手工修改表结构。
