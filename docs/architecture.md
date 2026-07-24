# 项目架构

## 当前实现范围

当前代码保持 POC 的有限 Playwright 采集与 PostgreSQL Catalog 发布边界，并接入商城测试预授权
触发的采购对话任务。v2 采购入口只接受商城服务端令牌，并在本地目录、价格和活动任务唯一性上
失败关闭；自动发送仍需要全局开关与单任务授权同时成立。真实云端验证仍需由持有登录态和数据库
权限的部署者执行，不能仅凭离线测试声明真实聊天成功。

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
  → shopping 的独立同步容器每 10 分钟经共享 Docker 私有网络调用带 Bearer 认证的 Catalog Sync API
  → GET /api/v1/catalog-sync/changes?after_revision=...
  → shopping 自己的商品镜像和持久化游标（含最多九张的公开详情图库）
  → 详情图库使用独立总时间预算；预算耗尽时保留搜索页封面并继续发布完整目录
  → app/api → repositories → x-comments PostgreSQL

商城结算核验
  → Next.js 服务器携带 Bearer 令牌
  → POST /api/v1/items/{item_id}/verify
  → app/api 鉴权 → app/services 存在性与价格比较
  → app/crawler 单次详情访问
  → 在总核验预算内等待客户端渲染的主商品信息区价格
  → 五状态失败关闭响应

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
- 商城不读取本服务数据库，也不从浏览器直接调用本服务；Catalog 边界保持只读 HTTP，采购任务则只
  允许 shopping 服务端通过独立令牌创建、查询和取消本地执行记录。
- `app/services/item_verification.py`：保留给人工诊断 API 的单商品实时核验服务；采购编排不再调用。
- `app/crawler/item_verifier.py`：单次详情身份、风险、不可售文案和当前价格核验；不写数据库。
  当前真实详情页不使用稳定的 `main` 标签，核验器只等待并读取
  `item-main-info` 容器内的唯一主价格，避免把下方推荐商品价格当成目标商品价格；等待后会再次检查
  登录/风控与明确下架文案，节点缺失仍返回 unknown，不放宽身份或价格门禁。业务结果产生后的页面
  与浏览器清理只忽略精确的 `TargetClosedError`，避免目标已关闭的竞态覆盖结果；导航、身份或价格
  核验阶段发生同类异常时仍返回 `verification_target_closed` 和 unknown，不重试。
- `app/models/procurement.py`：本地采购执行任务、会话、消息、追加审计和事务 Outbox。
- `app/schemas/procurement.py` / `procurement_llm.py`：商城任务、回调事件和 LLM JSON 的严格契约；
  未知字段直接拒绝，模型不能输出购买或付款动作。
- `app/ai/`：供应商无关草稿契约、`procurement_v1` 提示词和同步 DeepSeek 适配器；卖家消息与
  商品标题按不可信外部数据隔离，模块只返回草稿，不访问数据库或 Playwright。
- `app/services/procurement_policy.py`：无副作用的确定性草稿检查；不调用 Playwright，也不发送消息。
- `app/services/procurement_payload_safety.py`：在任务入库和 AI 调用前扫描商品标题中疑似夹带的客户、
  联系方式、卡号或支付资料；只返回稳定错误码，不回显命中文本。
- `app/services/procurement_orchestrator.py`：把订单绑定任务、可信来源快照、聊天适配器、DeepSeek 草稿、
  确定性策略和单次发送事务串联起来；最多三轮，不包含购买、付款或地址动作。
- `app/services/procurement_outbox.py`：向固定商城回调地址投递有序事件；重试只重试回调，不会触发聊天重发。

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

采购本地 API 对 v1 与 v2 请求统一校验 `PROCUREMENT_SOURCE_ITEM_ALLOWLIST`；v2 还必须由商城
Root/可信支付事件写入逐任务授权，两层门禁同时成立。随后扫描标题中
的疑似客户或支付资料，再以请求幂等键和规范化 body SHA-256 查询原任务；同键同正文返回原任务，
同键不同正文返回 409。首次创建必须能在 `items` 找到商品，并且彦诗筛选源存在 CNY 价格快照且价格
与商城整数分快照完全一致；Catalog `availability` 只供目录展示和同步观察，不参与采购任务门禁，也
不会触发闲鱼详情页实时核验。通过后在同一短事务中创建
`ProcurementExecutionTask` 与 `ConversationSession`。商品 URL 不接受调用方输入，只从
`Item.item_url` 复制到内部会话；仓储会先 flush 父任务，再 flush 带外键的会话，避免不同数据库
对无 ORM relationship 对象采用不同插入顺序。测试 SQLite 显式开启外键约束，确保该 PostgreSQL
边界持续回归。数据库部分唯一索引同时阻止同一商品存在两个活动采购任务。

入站消息通过外部消息 ID、出站草稿通过 SHA-256 幂等键去重；状态变化与商城回调事件在同一数据库
事务中写入审计和 `ProcurementOutbox`。投递器按每个任务的 `event_seq` 串行回调，任何同任务未交付
事件都会阻止下一次外部聊天动作。商城须按 `event_id` 幂等接收；回调失败会独立退避重试，但绝不
重复执行 Playwright 发送。发送前先持久化 `sending`，并在任务/会话行锁内完成唯一一次点击；崩溃或
结果不确定时进入人工审核，恢复后不重发。首次打开会话保存既有末条消息指纹作为基线，避免把历史
消息误认为本次回复；读取时保存基线后的全部新消息，并把页面的 `column-reverse` DOM 顺序还原为
时间正序。采购编排不会再次访问商品详情页核验库存或价格；每次打开聊天仍通过页面绑定确认商品、
卖家和买家账号。回复轮询按 2、5、10 分钟及后续每 15 分钟退避，最长 24 小时。编排器和投递器只在唯一
`scheduler_worker` 角色运行。

发送动作使用可见按钮中心点的 Playwright 鼠标移动、按下、短暂停顿与松开，不执行 JavaScript
`click()`、不回退 Enter，也不加入指纹伪装或风控绕过。唯一点击窗口同时监听携带完整草稿的官方
XHR/fetch 请求或 WebSocket 出站帧；只有网络出站证据与页面本人同文消息回显同时成立，消息才会
进入 `sent`。审计与 Outbox 只保存请求是否出现、传输类型、脱敏端点 SHA-256、方法和粗粒度响应
状态，不保存 URL、请求正文、Cookie、昵称或凭据。任一证据缺失都转人工且永不自动重试。

真实商品页可能同时存在多个 `/im` 入口，且动态 CSS 类名与可见文案会变化。页面适配器必须等待
客户端渲染完成，只接受唯一可见且同时带完整 `itemId`/`peerUserId` 参数的链接；随后仍以当前
商品 URL、目标商品 ID、卖家 ID 和买家账号指纹逐项核对。缺少参数、出现多个候选或身份不一致时
均失败关闭，不会回退到全页任意消息入口。入口首次可见但 `href` 尚未就绪时，仅在约两秒的
只读稳定化窗口内重复读取 DOM；每轮会在浏览器同一次执行中原子读取候选可见性和 `href`，
避免 React 重渲染发生在两个读取动作之间。该窗口不重复导航、不点击、不输入也不发送。

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
`.env.example`，真实值不得提交。当前两服务部署在同一台云服务器：Catalog Sync 对宿主机仅绑定
回环地址，shopping 的独立同步容器通过内部 Docker 网络的 `x-comments-api` 服务名访问；若未来拆分服务器，才必须改为 HTTPS、来源限制和服务间认证。`APP_ROLE` 只能为 `api` 或 `scheduler_worker`：云端可扩容 API，
但只能部署一个 scheduler-worker；该 worker 同时持有唯一 Playwright 队列和定时调度职责。

采购编排和脚本发送分别由 `PROCUREMENT_CHAT_ENABLED`、`PROCUREMENT_AUTO_SEND_ENABLED` 控制，
两者默认均为 `false`；自动发送不能脱离聊天编排单独开启。即使两个进程开关均打开，v2 任务仍须
携带可信的任务级 `auto_send_authorized=true`、`authorized_at` 与匹配执行模式的
`authorization_source` 才可能发送。置信度阈值默认 0.85，每会话自动发送上限硬限制为三轮；模型
输出还须通过意图白名单、风险文本、最新消息、商品/卖家/账号、价格、DOM、冷却时间和唯一写锁等
全部确定性检查。

DeepSeek 草稿适配器不读取环境变量；worker 必须显式构造 `DeepSeekConfig` 并提供 API Key，默认使用
`https://api.deepseek.com/chat/completions`、非流式 JSON Output 和关闭 thinking 的
`deepseek-v4-flash`。密钥使用 `SecretStr`，模块不记录密钥、提示词、卖家原文或供应商错误正文。

采购任务 API 使用与核验、Catalog Sync 分离的 `PROCUREMENT_API_TOKEN`，少于 32 字符按未配置处理并
返回 503。兼容旧调用方时，API 容器还可配置逗号分隔的
`PROCUREMENT_SOURCE_ITEM_ALLOWLIST`；该值同时限制 v1 与 v2，但本身不会授权发送。回调另用
`SHOPPING_PROCUREMENT_TOKEN`，并和 DeepSeek 密钥一样只注入 scheduler-worker，
不注入 API 容器；这些令牌不进入请求体、响应、浏览器、数据库或普通日志。

## 安全边界

当前采购任务 API 只读本地目录并写本地任务，不访问闲鱼页面。跨服务采购契约禁止日本客户姓名、
手机号、精确地址和支付资料进入 x-comments；严格 Schema 拒绝额外字段，自由文本再经过安全扫描。
验证码、登录失效、403/429、访问频繁和结构异常必须停止，禁止绕过、代理轮换或多账号续爬。

正式应用为 API 核验器与 scheduler worker 注入同一种 `XianyuAccountGuard`。它先用
进程内 `asyncio.Lock` 阻止本进程并发，再用 PostgreSQL session-level advisory lock
阻止两个容器同时使用同一登录态。等待锁仍计入各自安全超时；取消或连接断开时
会释放锁或使连接失效，避免带锁连接回到连接池。SQLite 离线测试安全退化为进程内锁。
核验器每次只导航一次，不会因超时、未知结构或风控自动重试。详见
`docs/xianyu-account-guard.md`。

## 容器运行

Docker Compose 使用官方 Playwright Python 镜像和 PostgreSQL `postgres_data` named volume。一次性
`migrate` 服务在空 PostgreSQL 执行 Alembic；成功后 `api` 与唯一 `scheduler-worker` 才启动。后者
显式启动 Xvfb，并以 `DISPLAY=:99` 运行有头 Chromium。入口脚本会同时监管 Xvfb 与 Uvicorn；容器
停止时会向两者发送结束信号并等待退出，避免遗留显示锁影响下一次启动。登录态由
宿主机只读挂载，不写入镜像或 volume。
