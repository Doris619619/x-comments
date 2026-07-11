# Goal 0 参考项目审计与技术验证

## 1. 审计范围与方法

审计日期：2026-07-11（Asia/Shanghai）。

本次只执行 Goal 0，没有引入参考项目代码，也没有开始后端业务实现。审计采用以下方法：

1. 对四个指定仓库执行 `--depth 1` 浅克隆；
2. 记录审计提交；
3. 阅读许可证、入口文件以及登录、搜索、解析、存储、API、任务相关核心代码；
4. 使用受控浏览器直接访问闲鱼搜索页，观察未登录状态下的页面行为；
5. 将可采用的设计思路与禁止继承的高风险能力分开记录。

## 2. 当前仓库审计

审计前仓库只有 `AGENTS.md`、`Goal.txt` 和 `xianyu_account_safety_red_lines.md`，没有应用代码、数据库、测试或部署文件。因此当前不存在需要兼容的历史实现，也不存在需要删除的未经论证第三方代码。

## 3. 参考仓库结论

### 3.1 Usagi-org/ai-goofish-monitor

- 审计提交：[`f85d140b6b45029d9a0925feb96dad733b41396d`](https://github.com/Usagi-org/ai-goofish-monitor/tree/f85d140b6b45029d9a0925feb96dad733b41396d)
- 许可证：MIT，仓库包含 [`LICENSE`](https://github.com/Usagi-org/ai-goofish-monitor/blob/f85d140b6b45029d9a0925feb96dad733b41396d/LICENSE)。
- 登录态：爬虫从账号状态文件创建 Playwright `BrowserContext`；入口在 [`src/scraper.py`](https://github.com/Usagi-org/ai-goofish-monitor/blob/f85d140b6b45029d9a0925feb96dad733b41396d/src/scraper.py)，并检测跳转到 `passport.goofish.com`/`mini_login`。
- 搜索入口：构造 `https://www.goofish.com/search?...`，监听 `mtop.taobao.idlemtopsearch.pc.search` 搜索响应。
- 字段解析：[`src/parsers.py`](https://github.com/Usagi-org/ai-goofish-monitor/blob/f85d140b6b45029d9a0925feb96dad733b41396d/src/parsers.py) 从 `data.resultList[].data.item.main.exContent` 读取 `itemId`、`title`、`price`、`area`、`userNickName`、`targetUrl`、`picUrl`，发布时间来自 `clickParam.args.publishTime`。
- 唯一键与存储：[`src/services/result_storage_service.py`](https://github.com/Usagi-org/ai-goofish-monitor/blob/f85d140b6b45029d9a0925feb96dad733b41396d/src/services/result_storage_service.py) 优先使用商品 ID，缺失时退回规范化链接；SQLite 表以 `(result_filename, link_unique_key)` 唯一约束去重。
- API：[`src/api/routes/tasks.py`](https://github.com/Usagi-org/ai-goofish-monitor/blob/f85d140b6b45029d9a0925feb96dad733b41396d/src/api/routes/tasks.py) 使用 FastAPI 路由和服务依赖，任务运行由进程服务负责。
- 任务执行：`spider_v2.py` 从 SQLite 读取任务并创建异步任务；项目还包含调度、多账号、代理轮换、AI 分析和通知。
- 本项目只参考：Playwright 状态复用、搜索响应解析的分层、登录跳转检测、SQLite 去重思路。
- 不采用：多账号、代理轮换、自动重试、AI 分析、通知、定时监控及并发执行。这些超出 POC，部分能力与本项目安全红线冲突。

### 3.2 superboyyy/xianyu_spider

- 审计提交：[`4cf59de2a744557e7f1f5f8a2ed581faadf947e8`](https://github.com/superboyyy/xianyu_spider/tree/4cf59de2a744557e7f1f5f8a2ed581faadf947e8)
- 许可证：仓库根目录没有 `LICENSE`/`COPYING`，README 也未授予明确许可；代码只能阅读，不能复制。
- 登录态：未实现持久登录态，Playwright 直接创建无状态、无头上下文。
- 搜索入口：[`spider.py`](https://github.com/superboyyy/xianyu_spider/blob/4cf59de2a744557e7f1f5f8a2ed581faadf947e8/spider.py) 打开闲鱼首页、填写搜索框并监听 `mtop.taobao.idlemtopsearch.pc.search`。
- 商品 ID：没有直接保存 `itemId`，而是截断商品链接查询串后计算 MD5 作为 `link_hash`；这不满足本项目“商品唯一 ID”要求，只能作为缺失 ID 时的最后降级思路。
- 字段解析：同样读取搜索响应的 `exContent`，提取标题、价格、地区、卖家、链接、图片和发布时间。
- 数据库存储：FastAPI、爬虫、解析与 Tortoise 模型集中在同一文件；`link_hash` 唯一，使用 `get_or_create` 去重。
- FastAPI 路由：`POST /search/` 同步等待完整爬虫结束后才响应。
- 任务执行：没有独立任务模型或后台任务，接口请求即任务。
- 本项目只参考：搜索响应大致字段路径的交叉验证。
- 不采用：单文件结构、同步阻塞 API、无登录态无头抓取、链接 MD5 作为主键。

### 3.3 pbeenigg/LittleCrawler

- 审计提交：[`741780d1ab15d14ba175df560b212eb06c956282`](https://github.com/pbeenigg/LittleCrawler/tree/741780d1ab15d14ba175df560b212eb06c956282)
- 许可证：MIT，仓库包含 [`LICENSE`](https://github.com/pbeenigg/LittleCrawler/blob/741780d1ab15d14ba175df560b212eb06c956282/LICENSE)。
- 模块边界：[`src/core/base_crawler.py`](https://github.com/pbeenigg/LittleCrawler/blob/741780d1ab15d14ba175df560b212eb06c956282/src/core/base_crawler.py) 把 crawler、login、store、API client 定义为独立抽象；存储实现与平台实现分离。
- API 与任务：[`api/routers/crawler.py`](https://github.com/pbeenigg/LittleCrawler/blob/741780d1ab15d14ba175df560b212eb06c956282/api/routers/crawler.py) 只负责 HTTP；[`api/services/crawler_manager.py`](https://github.com/pbeenigg/LittleCrawler/blob/741780d1ab15d14ba175df560b212eb06c956282/api/services/crawler_manager.py) 使用锁、子进程和异步日志读取管理任务生命周期。
- 数据库存储：提供通用存储抽象、SQLite/MongoDB/Excel 基础设施，但不是本项目所需的 SQLAlchemy 关系模型。
- 闲鱼能力现状：配置与 API 枚举中出现 `xhy`（小黄鱼/闲鱼），但该审计提交的 `src/platforms/` 只有小红书和知乎，没有可审计的闲鱼登录、搜索、解析或存储实现。
- 本项目只参考：API/服务/爬虫/存储边界、进程生命周期管理思想。
- 不采用：WebSocket、通用多平台框架、强制子进程架构；首版仅需单进程单 worker。

### 3.4 overspread/xianyu-Auto

- 审计提交：[`9888bfd3e161e156bb5e3a2a65ba59e7724f0c97`](https://github.com/overspread/xianyu-Auto/tree/9888bfd3e161e156bb5e3a2a65ba59e7724f0c97)
- 许可证：仓库根目录没有 `LICENSE`/`COPYING`，README 未提供明确开源授权；代码只能阅读，不能复制。
- 登录态：[`login.py`](https://github.com/overspread/xianyu-Auto/blob/9888bfd3e161e156bb5e3a2a65ba59e7724f0c97/login.py) 以有头 Chromium 打开闲鱼，要求用户手工扫码，确认后调用 `context.storage_state(path=...)`。
- 搜索入口与解析：[`spider_v2.py`](https://github.com/overspread/xianyu-Auto/blob/9888bfd3e161e156bb5e3a2a65ba59e7724f0c97/spider_v2.py) 打开 `https://www.goofish.com/search?q=...`，解析 `resultList`、`exContent.itemId/title/price/area/userNickName/targetUrl/picUrl`。
- 商品 ID：直接使用 `exContent.itemId`，比链接哈希更适合作为本项目主唯一键。
- 数据库存储：[`database.py`](https://github.com/overspread/xianyu-Auto/blob/9888bfd3e161e156bb5e3a2a65ba59e7724f0c97/database.py) 使用 aiosqlite，商品按 `(product_id, task_id)` 查询去重，任务、商品、Cookie、日志混合在一个数据库类中。
- FastAPI 路由：`web_server.py` 提供任务、Cookie 和日志 API；路由和较多业务逻辑集中在大文件。
- 任务执行：任务由 Web 服务和爬虫脚本驱动，并包含限速、代理与 Cookie 池。
- 本项目只参考：人工有头登录后保存 Playwright storage state，以及 `itemId` 字段路径。
- 不采用：Cookie 池、账号轮换、代理、失败后切换账号、在数据库中保存明文登录状态、超大文件结构。

## 4. 当前闲鱼页面技术验证

### 4.1 执行方式

在 2026-07-11 使用受控 Chromium/Playwright 打开：

```text
https://www.goofish.com/search?q=%E5%A5%B3%E7%94%9F%E5%8F%91%E9%A5%B0
```

本次没有输入账号、密码、验证码、Cookie 或 Token，也没有尝试绕过任何限制。

### 4.2 观察结果

- 页面成功打开，最终 URL 保持搜索 URL；标题为“女生发饰_闲鱼”。
- 页面先显示“加载中...”，随后出现真实 `https://www.goofish.com/item?id=...` 商品链接。
- 可见商品链接中的 `id` 是稳定候选商品 ID，例如 URL 形态为 `/item?id=<数字>&categoryId=...`。
- 页面同时显示“登录”入口，说明当前浏览器上下文未处于已确认登录状态。
- 本次未登录上下文返回的前若干商品与“女生发饰”明显不匹配。该结果可能是公共推荐/降级结果，不能作为关键词真实采集成功的证据。
- 本次未观察到验证码、安全验证或 403/429 页面，但这不代表后续真实任务不会触发风控。

### 4.3 技术判断

1. Playwright 可以正常打开当前闲鱼搜索页面。
2. 仅“页面能打开”不等于“关键词搜索有效”；Goal 2 必须验证返回结果与关键词、页面筛选状态和搜索响应一致。
3. 首版需要人工有头登录。登录流程只允许用户在本机浏览器中操作，应用不得收集账号密码或短信验证码。
4. 登录后通过 `BrowserContext.storage_state(path=...)` 保存到本地文件；运行任务时用 `browser.new_context(storage_state=...)` 复用。
5. 状态文件放在 `state/` 或 `playwright/.auth/`，必须被 `.gitignore` 排除，文件路径通过环境变量配置，不进入数据库、不写日志。
6. 若跳转登录/验证页、出现验证码/访问频繁/403/429、搜索结果异常归零或结果明显与关键词不符，应停止并标记 `blocked_by_auth_or_risk_control`，不得自动重试。

## 5. 商品 ID 候选提取顺序

按可靠性从高到低：

1. 搜索响应 `data.resultList[].data.item.main.exContent.itemId`；
2. 商品链接的 `item?id=<数字>` 查询参数；
3. 仅用于诊断的规范化链接哈希。

前两种结果同时存在时必须一致；不一致则视为解析异常并停止保存该条。生产数据不得把“未知ID”或随机值当作 `item_id`。

## 6. 首版字段建议

商品：`item_id`、`title`、`price`（Decimal）、`image_url`、`item_url`、`location`、`source`、`first_seen_at`、`last_seen_at`、`created_at`、`updated_at`。

关联：独立 `keywords` 表和 `item_keywords` 多对多表，避免把单个关键词字符串写进商品表。

任务：`job_id`、`keyword`、`status`、`created_at`、`started_at`、`finished_at`、`discovered_count`、`new_count`、`updated_count`、`duplicate_count`、`error_count`、`error_message`。

## 7. 结论

技术路径可行，但必须将“正常页面访问”“正确关键词结果”“合法登录态”和“无风控信号”作为四个独立门槛。建议采用 FastAPI + SQLAlchemy 2 + Alembic + SQLite/PostgreSQL + Playwright；搜索响应解析优先，DOM 解析仅作受控降级。Goal 0 可以通过，建议进入 Goal 1，但进入 Goal 2 前必须由用户完成本机人工登录并单独执行真实采集验收。
