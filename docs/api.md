# 前后端交互接口文档

> 数据模型（表、字段、状态枚举）见 [data-model.md](./data-model.md)。

> 基础路径：无统一前缀；业务接口挂在 `/api/v1/...`，健康检查与演示页在根路径。  
> 认证方式：商品读取与采集 POC 接口当前无用户登录鉴权；结算核验接口必须使用服务端
> `Bearer` 令牌，且令牌未配置时默认关闭。
> 校验失败统一返回 **422**（Pydantic 参数校验）；业务错误见各状态码。
> 商城对接方式：shopping 仅在服务器端通过 HTTP 调用本服务；当前服务未配置 CORS，浏览器不得直接调用商品接口。商品镜像同步必须使用下文的 `catalog-sync` 版本化接口，不得直连数据库或从普通商品分页的缺失推断下架。

## 通用约定

| 项目 | 说明 |
|------|------|
| Content-Type | `application/json` |
| 任务执行 | `POST /api/v1/crawl-jobs` 立即落库 `pending`；唯一 scheduler-worker 原子认领持久化任务，测试应用可禁用 worker |
| 排序（商品列表） | `last_seen_at DESC, item_id ASC` |
| OpenAPI | FastAPI 自动生成：`/docs`、`/openapi.json` |

### 错误响应格式

```json
{
  "detail": "错误描述"
}
```

校验失败（**422**）时 `detail` 为 FastAPI/Pydantic 校验错误列表。

### 采集任务状态

| 值 | 含义 |
|----|------|
| `pending` | 已创建，等待 worker |
| `running` | 正在采集 |
| `succeeded` | 成功完成 |
| `partially_succeeded` | 部分成功 |
| `failed` | 失败 |
| `blocked_by_auth_or_risk_control` | 登录失效或风控，安全停止 |

---

## 1. 健康检查

### `GET /health`

验证应用与数据库连通性，并返回部署监控所需的最近采集/发布指标。无需认证。

**响应 200**

```json
{
  "status": "ok",
  "database": "ok",
  "last_successful_crawl_at": "2026-07-17T10:00:00+00:00",
  "last_published_revision": 42,
  "last_published_at": "2026-07-17T10:00:01+00:00",
  "consecutive_failed_runs": 0
}
```

`consecutive_failed_runs` 只统计最近一次成功采集之后已结束的部分成功、失败和风控运行；它是
告警输入，不会触发自动下架。数据库不可用时由框架返回错误（非业务 200）。


## 2. 采集任务

### `POST /api/v1/crawl-jobs`

创建关键词采集任务：写入 `pending` 记录，并返回完整任务对象（含 `job_id`）。独立
scheduler-worker 会从持久化队列原子认领任务，因此 API 与 worker 不需要同进程。

**请求体**

```json
{
  "keyword": "女生发饰"
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| keyword | string | 必填；去首尾空白并折叠中间空白后 1–100 字符；全空白视为非法 |

**响应 202**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "keyword": "女生发饰",
  "status": "pending",
  "created_at": "2026-07-13T03:00:00+00:00",
  "started_at": null,
  "finished_at": null,
  "discovered_count": 0,
  "new_count": 0,
  "updated_count": 0,
  "duplicate_count": 0,
  "error_count": 0,
  "error_message": null
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| job_id | string | UUID，任务主键 |
| keyword | string | 清洗后的展示关键词 |
| status | string | 见「采集任务状态」 |
| created_at / started_at / finished_at | datetime \| null | UTC 时间；未开始/未结束为 `null` |
| discovered_count | int | 本轮发现条数 |
| new_count | int | 新入库条数 |
| updated_count | int | 已存在并更新条数 |
| duplicate_count | int | 重复条数 |
| error_count | int | 处理错误条数 |
| error_message | string \| null | 安全错误说明（如风控停止原因）；成功时为 `null` |

关键词不合法：**422**。

---

### `GET /api/v1/crawl-jobs/{job_id}`

查询任务状态、时间戳、统计与安全错误信息。前端可轮询直至终态。

**路径参数**

| 参数 | 类型 | 约束 |
|------|------|------|
| job_id | string | 任务 UUID |

**响应 200**：结构同创建接口的任务对象（`status` 等字段会随执行更新）。

**错误**

| 状态码 | 说明 |
|--------|------|
| 404 | `{"detail": "采集任务不存在"}` |

---

## 3. 商品

### `GET /api/v1/items`

分页查询已入库商品；可选按规范化关键词关联过滤。

**查询参数**

| 参数 | 类型 | 约束 | 默认 |
|------|------|------|------|
| page | int | ≥ 1 | 1 |
| page_size | int | 1–100 | 20 |
| keyword | string \| null | 可选，≤ 100 字符；按关键词关联过滤 | 不传则不过滤 |
| category | string \| null | 可选，≤ 64 字符；按杂货铺采集清单分类过滤 | 不传则不过滤 |

**响应 200**

```json
{
  "items": [
    {
      "item_id": "123456789012",
      "title": "蝴蝶结发夹",
      "price": "12.50",
      "image_url": "https://example.com/a.jpg",
      "item_url": "https://www.goofish.com/item?id=123456789012",
      "location": "上海",
      "source": "xianyu",
      "first_seen_at": "2026-07-13T03:01:00+00:00",
      "last_seen_at": "2026-07-13T03:01:00+00:00",
      "created_at": "2026-07-13T03:01:00+00:00",
      "updated_at": "2026-07-13T03:01:00+00:00"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1,
  "pages": 1
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| items | array | 当前页商品列表 |
| page / page_size | int | 当前页与每页大小 |
| total | int | 符合条件的总条数 |
| pages | int | 总页数 |

商品字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| item_id | string | 闲鱼商品唯一 ID |
| title | string | 标题 |
| price | decimal string | `NUMERIC(12,2)` 序列化结果，如 `"12.50"` |
| image_url | string \| null | 主图；缺失为 `null`，不伪造 |
| item_url | string | 商品链接 |
| location | string \| null | 公开地区；缺失为 `null` |
| source | string | 来源，当前为 `xianyu` |
| first_seen_at / last_seen_at | datetime | 首次 / 最近发现时间 |
| created_at / updated_at | datetime | 行创建 / 更新时间 |

参数不合法：**422**。

### 商城服务器端读取约定

商城服务应从其服务器端调用 `GET /api/v1/items` 和 `GET /api/v1/items/{item_id}`，并将
`item_id` 作为稳定外部标识。列表和详情可安全读取 `item_id`、`title`、`price`、`image_url`、
`location`、`last_seen_at` 与 `source`；`image_url`、`location` 允许为 `null`，调用方必须降级展示。
本 POC 不设置宽松 CORS，也不允许将服务端地址或未来鉴权信息暴露给浏览器。

商城首页可用 `category=潮玩手办`、`category=实用小物` 或 `category=怀旧收藏` 筛选。分类由
`catalog_keywords` 配置关联到实际采集词；当某个新增词尚未完成第一轮采集时，分类会合法地返回空列表。

---

### `GET /api/v1/items/{item_id}`

返回单个已入库商品。

**路径参数**

| 参数 | 类型 | 约束 |
|------|------|------|
| item_id | string | 闲鱼商品 ID |

**响应 200**：单个商品对象（字段同列表中的元素）。

**错误**

| 状态码 | 说明 |
|--------|------|
| 404 | `{"detail": "商品不存在"}` |

---

### `POST /api/v1/items/{item_id}/verify`

供商城服务器在结算前对一件已入库商品执行一次实时详情核验。该接口每次最多访问一个详情页，
不自动重试；只有 `available` 可以继续下单，其余状态均应安全阻止本次结算。

**认证**

```http
Authorization: Bearer <XIANYU_API_TOKEN>
```

`XIANYU_API_TOKEN` 未配置、为空或短于 32 字符时接口返回 **503**；令牌缺失或不匹配时返回 **401**。
令牌只能保存在两个服务的服务端环境变量中，不得发送给浏览器。

**请求体**

```json
{
  "expected_price": "12.50",
  "currency": "CNY",
  "context": "checkout"
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| expected_price | decimal string | 人工确认时保存的人民币来源价格；非负，最多两位小数 |
| currency | string | 固定为 `CNY` |
| context | string | 固定为 `checkout` |

**响应 200**

```json
{
  "status": "available",
  "current_price": "12.50",
  "verified_at": "2026-07-16T03:00:00+00:00",
  "reason_code": "listing_available",
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

| status | 含义 | 商城处理 |
|--------|------|----------|
| `available` | 详情身份与当前价格均可确认，且价格等于人工快照 | 可以继续下单 |
| `unavailable` | 页面出现明确已售、下架、删除或不存在信号 | 阻止下单 |
| `price_changed` | 当前人民币价格与人工快照不同 | 阻止下单并转人工复价 |
| `blocked` | 登录态缺失、失效或触发风控 | 阻止下单并人工处理 |
| `unknown` | 超时、结构变化、页面异常或身份/价格无法确认 | 阻止下单 |

`current_price` 仅在页面可以安全提取价格时返回，否则为 `null`。`request_id` 与服务端脱敏日志
对应，用于定位一次核验，不代表闲鱼订单或商城订单。

**错误**

| 状态码 | 说明 |
|--------|------|
| 401 | Bearer 令牌缺失或无效 |
| 404 | 本地数据库没有该商品；此时不会访问闲鱼 |
| 422 | 请求字段不符合固定契约 |
| 503 | 服务端未配置 `XIANYU_API_TOKEN`，接口默认关闭 |

---

## 4. Catalog Sync（仅 shopping 服务端）

以下接口均要求请求头：

```text
Authorization: Bearer <CATALOG_SYNC_TOKEN>
```

令牌未配置返回 **503**，缺失或错误返回 **401**。令牌只能存在两个服务的服务端环境变量中，不得放入 `NEXT_PUBLIC_*`、浏览器或日志。

### `GET /api/v1/catalog-sync/revisions/latest`

返回 shopping 可读取的最新完整发布版本。空目录稳定返回 `revision=0`、`status="empty"`。

```json
{
  "revision": 42,
  "published_at": "2026-07-16T10:00:00+00:00",
  "source": "xianyu",
  "status": "published"
}
```

### `GET /api/v1/catalog-sync/changes?after_revision={integer}&limit={1..500}`

按 `revision ASC, item_id ASC` 返回增量，且不在一个 revision 中间截断。shopping 必须先在自己的数据库幂等应用整页变更，再推进本地游标到 `to_revision`。

```json
{
  "from_revision": 40,
  "to_revision": 42,
  "has_more": false,
  "changes": [
    {
      "revision": 42,
      "change_type": "availability_changed",
      "item_id": "1234567890",
      "availability": "off_shelf",
      "title": "商品标题",
      "price": "12.50",
      "currency": "CNY",
      "image_url": null,
      "location": "上海",
      "last_seen_at": "2026-07-16T09:50:00+00:00",
      "status_changed_at": "2026-07-16T10:00:00+00:00"
    }
  ]
}
```

`availability` 只会是 `active`、`suspected_missing`、`sold`、`off_shelf` 或 `unknown`。接口不返回 `item_url`、Cookie、登录态或内部数据库字段。若游标早于已保留的最小 revision 或大于当前版本，返回 **409**；shopping 必须转用全量重建接口，不能把该错误解释为商品下架。

### `GET /api/v1/catalog-sync/items?page={page}&page_size={1..500}`

返回每个商品的**最近已发布快照**，按 `item_id ASC` 分页，用于处理上述 **409** 后的全量重建。每项字段与 `changes` 中的单条变更相同。

### `GET /api/v1/catalog-sync/items/{item_id}`

返回一个商品的最近已发布快照；不存在返回 **404**。该接口仅用于恢复或诊断，不替代增量游标同步。

---

## 5. 杂货铺搜索清单

### `GET /api/v1/catalog-keywords`

返回已启用的持久化杂货铺搜索清单。该接口只读；当前 POC 不开放未鉴权的浏览器配置写入。

**响应 200**

```json
[
  {
    "id": 1,
    "category": "潮玩收藏",
    "keyword": "手办",
    "interval_minutes": 60,
    "last_scheduled_at": "2026-07-15T10:00:00+00:00",
    "note": "动漫、模型和小摆件"
  }
]
```

---

## 6. POC 演示页

### `GET /`

返回内部演示 HTML（`POC internal demo`）。页面只调用上述 REST API：创建任务后轮询状态（约最多 3 分钟），商品列表每页 12 条。静态资源位于 `/static/`。

不纳入 OpenAPI schema。

---

## 推荐调用顺序（闭环）

```text
1. GET  /health
2. POST /api/v1/crawl-jobs          → 拿到 job_id
3. GET  /api/v1/crawl-jobs/{job_id} → 轮询至终态
4. GET  /api/v1/items?keyword=女生发饰&page=1&page_size=12
```

登录失效或风控时，任务会进入 `blocked_by_auth_or_risk_control`，`error_message` 给出安全停止原因；不得自动重试或绕过。详见 [known-limitations.md](./known-limitations.md)。
