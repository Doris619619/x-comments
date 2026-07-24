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
      "image_urls": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
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
| image_url | string \| null | 兼容旧调用方的首图；缺失为 `null` |
| image_urls | string[] | 最多九张详情公开图库；首项与 `image_url` 相同，缺图为 `[]` |
| item_url | string | 商品链接 |
| location | string \| null | 公开地区；缺失为 `null` |
| source | string | 来源，当前为 `xianyu` |
| first_seen_at / last_seen_at | datetime | 首次 / 最近发现时间 |
| created_at / updated_at | datetime | 行创建 / 更新时间 |

参数不合法：**422**。

### 商城服务器端读取约定

商城服务应从其服务器端调用 `GET /api/v1/items` 和 `GET /api/v1/items/{item_id}`，并将
`item_id` 作为稳定外部标识。列表和详情可安全读取 `item_id`、`title`、`price`、`image_url`、
`image_urls`、`location`、`last_seen_at` 与 `source`；`image_urls` 最多九项，调用方必须为 `[]`、
`image_url=null` 与 `location=null` 提供降级展示。
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
      "image_urls": [],
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

## 7. 本地采购执行任务（仅 shopping 服务端）

以下接口均要求独立服务端令牌：

```http
Authorization: Bearer <PROCUREMENT_API_TOKEN>
```

令牌未配置或短于 32 字符返回 **503**，缺失或错误返回 **401**。它不能与
`XIANYU_API_TOKEN`/`CATALOG_SYNC_TOKEN` 共用，不得进入浏览器、数据库、日志或 Git。旧版 v1
任务还受 `PROCUREMENT_SOURCE_ITEM_ALLOWLIST` 保护；v2 任务改由商城的 Root/支付授权快照、
服务端令牌和本地来源快照共同约束。

### `POST /api/v1/procurement-tasks`

请求还必须携带 16–128 字符的 `Idempotency-Key`。服务端对严格 Pydantic 规范化后的完整 body 计算
SHA-256：同键同 body 返回原任务和原 `session_id`，同键不同 body 返回 **409**。

```json
{
  "contract_version": 2,
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "execution_mode": "operator_canary",
  "auto_send_authorized": false,
  "source": {
    "platform": "xianyu",
    "item_id": "1234567890",
    "expected_seller_id": null
  },
  "expected_listing": {
    "title": "格力空调遥控器",
    "price_cny_minor": 1250,
    "currency": "CNY",
    "verified_at": "2026-07-20T10:00:00Z"
  },
  "objectives": ["availability", "function", "shipping_time"],
  "policy": {
    "max_auto_rounds": 3,
    "response_deadline_at": "2026-07-21T10:00:00Z"
  }
}
```

v1 兼容请求只允许未授权的 `paid_order` 默认值；商城 POC 使用 v2 `operator_canary`。默认
`auto_send_authorized=false`，不允许页面发送。未来单商品 Canary 要授权发送时必须同时提供
`authorized_at` 和 `authorization_source="operator_canary"`，且仍受全局双开关和全部确定性策略约束。

请求不接受 `item_url`；服务端只从现有 `Item.item_url` 读取来源 URL。未知字段直接返回 **422**，因此
日本客户姓名、电话、地址、支付资料等额外字段不能进入任务。标题自由文本还会经过隐私/支付资料
扫描，错误响应只返回稳定 code，不回显正文。创建前依次验证：

1. v1 任务的 `PROCUREMENT_SOURCE_ITEM_ALLOWLIST` 已配置且包含该商品；v2 跳过这项静态白名单；
2. 标题未命中客户、联系方式、卡号或支付资料安全规则；
3. Item 表存在该商品；
4. 存在最新发布 Catalog 快照；
5. 彦诗筛选源的 `availability` 为 `active` 或 `suspected_missing`，且 `currency=CNY`；
6. 发布价格与 `price_cny_minor` 完全一致；
7. 同一 `item_id` 没有其他活动采购任务。

成功在同一事务中创建本地执行任务和会话，返回 **202**：

```json
{
  "contract_version": 2,
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "82dc9175-4ef1-43d6-b318-0b49e205ba2d",
  "status": "pending_source_verification",
  "next_action": "verify_source",
  "created_at": "2026-07-20T10:00:01Z"
}
```

`pending_source_verification` 与 `verify_source` 是 v2 兼容字段名。Worker 不再据此打开闲鱼详情页
复核库存或价格；它先比较本地 Item、官方 URL 和 CNY 快照，然后进入绑定聊天。聊天页仍会校对商品、
卖家和买家账号身份。

| 状态码 | code/说明 |
|--------|-----------|
| 401 | 采购 Bearer 令牌缺失或错误 |
| 403 | v1 的 `source_item_not_allowlisted` |
| 404 | `source_item_not_found` |
| 409 | `idempotency_conflict`、`task_id_conflict`、`source_item_has_active_procurement`、`source_not_active` 或 `source_price_changed` |
| 422 | body、UUID、幂等键、额外字段或自由文本安全规则不符合契约 |
| 503 | 采购令牌或 v1 商品白名单未配置 |

### `GET /api/v1/procurement-tasks/{task_id}`

返回任务保存的来源商品 ID、标题/CNY 整数分、目标、轮次/期限、任务与会话状态、下一动作、脱敏摘要/
原因和时间，以及 v2 执行模式和任务级授权快照。响应不返回请求幂等键、body 哈希、内部租约或商品
URL。不存在返回 **404**。

### `GET /api/v1/procurement-tasks/{task_id}/messages?after_seq={integer}&limit={1..200}`

按会话序号增量返回卖家原文和 AI 草稿，供商城受保护的管理后台形成统一时间线。响应携带
`next_seq` 和 `has_more`；正文不得进入公开用户 API 或普通日志。接口只读，不触发 AI、页面发送、
购买或付款。

### `POST /api/v1/procurement-tasks/{task_id}/cancel`

```json
{"reason_code": "cancelled_by_shopping"}
```

将仍在执行的任务与会话同事务改为 `cancelled`，`next_action=none`；重复取消幂等。其他终态返回
**409**。该操作只更新本地状态，不操作闲鱼页面、不购买、不付款。

LLM 输出继续使用 `$id=procurement-chat-v1` 的 `ProcurementLlmOutput`：只能建议安全询问、进入审核、
要求人工或停止，不存在 `confirm_purchase`。唯一 scheduler worker 可在双重功能开关显式开启后执行：
订单/商品/卖家/价格核验、读取新卖家回复、调用 DeepSeek 生成严格草稿、确定性 policy 审核，以及最多
三轮的单次脚本发送。DeepSeek 不能直接发送；policy 不通过、敏感信息、页面漂移、登录/风控、发送
结果不确定都会转人工或终止。每个状态变化通过固定 `SHOPPING_CALLBACK_URL` 回调，使用独立
`SHOPPING_PROCUREMENT_TOKEN`；商城按 `event_id` 幂等处理。存在未交付回调时不会继续下一次聊天动作。

`assistant.message_sent` 和发送不确定时的 `assistant.message_blocked` 可在 `data` 中携带
`send_request_evidence`。该对象只包含 `request_observed`、`transport`、`endpoint_sha256`、
`method`、`response_observed` 与 `response_status`，用于后台判断浏览器是否真的发出匹配草稿的
网络动作；它不包含 URL、正文、请求头、Cookie、昵称或凭据。`request_observed=false` 绝不能被
解释为成功发送，也不能触发重试。

---

## 推荐调用顺序（闭环）

```text
1. GET  /health
2. POST /api/v1/crawl-jobs          → 拿到 job_id
3. GET  /api/v1/crawl-jobs/{job_id} → 轮询至终态
4. GET  /api/v1/items?keyword=女生发饰&page=1&page_size=12
```

登录失效或风控时，任务会进入 `blocked_by_auth_or_risk_control`，`error_message` 给出安全停止原因；不得自动重试或绕过。详见 [known-limitations.md](./known-limitations.md)。
