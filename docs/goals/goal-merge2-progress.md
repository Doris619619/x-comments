# GoalMerge2 进度报告：x-comments 与 shopping 的定时目录同步

## 当前状态

```text
IN PROGRESS
```

本报告记录截至当前代码版本已经落地的实现、真实本机演练与仍待云端确认的事项，不将本机演练冒充为云端上线结果。

## 已完成实现

- x-comments 运行时数据库统一为 PostgreSQL；Compose 包含 PostgreSQL 16、一次性迁移、API 和唯一 scheduler-worker。
- API 不再启动浏览器；scheduler-worker 轮询持久化 pending 任务并按 10 分钟调度。数据库局部唯一索引和 PostgreSQL 并发集成测试共同验证同一关键词不会被两个 scheduler 重复创建。
- 一次完整采集在单个事务内发布商品、目录状态、采集批次和递增修订号；读取方不会看到半批次结果，因此不需要手工锁表。
- 目录状态包括 `active`、`suspected_missing`、`sold`、`off_shelf` 和 `unknown`。连续两次完整采集缺失后才自动标记为下架，部分结果、风控和失败不会触发下架。
- x-comments 提供携带 Bearer Token 的目录同步接口：最新修订、增量变更、全量快照和单商品读取；`/health` 同时返回最近成功采集、最近发布 revision 和连续失败次数，供告警使用。
- shopping 保持原有 MongoDB 商品镜像；每 10 分钟通过同机 `127.0.0.1` 的受认证 API 拉取 x-comments 增量，而不直连或迁移 x-comments 的 PostgreSQL。
- shopping 将下架/售出/疑似缺失商品从公开列表隐藏，并保留购物车条目；购物车显示不可购买提示、禁止结算，后端订单校验仍是最终保护。
- 同步游标持久化在 shopping 的 `XianyuCatalogSyncState` 中；增量历史过期时会收到 409 并执行全量重建。
- 负责人已确认闲鱼商品当前仅展示：shopping 禁止其加购、结算、扣款、采购和订单创建；服务端拒绝伪造请求且不调用详情核验或扣减库存。

## 已验证

```text
x-comments
python -m pytest -q                         # 33 passed, 1 skipped（本机未配置直连 PostgreSQL 集成环境）
python -m ruff check .                       # passed
python -m mypy app                           # passed
python -m scripts.check_chinese_docstrings   # 63 files passed
DATABASE_URL=postgresql+psycopg://... python -m alembic upgrade head --sql
                                                # PostgreSQL 迁移 SQL 成功生成
GitHub Actions CI                            # 配置 PostgreSQL 服务、迁移与 scheduler 并发集成测试

shopping
npm.cmd run xianyu:sync:verify               # passed
npm.cmd run xianyu:contract:verify           # passed
npm.cmd run xianyu:revision-sync:verify      # passed
npm.cmd run lint                             # passed（仅既有警告）
npm.cmd run build                            # passed
```

### 真实本机 Docker 演练（2026-07-17）

- `docker compose --env-file .env.example up --build -d`：PostgreSQL、一次性 `migrate`、`api` 和唯一 `scheduler-worker` 均启动；
- Alembic 实际升级至 `20260716_0005 (head)`，部分唯一索引 `uq_crawl_jobs_inflight_keyword` 已存在；
- API `/health` 返回数据库正常、连续失败 `0`；scheduler-worker 已实际完成多次公开搜索采集并发布多个 revision；
- 在容器内同时运行两个 scheduler 的隔离竞争验证，结果为一个任务 ID、一个 `pending` 任务；
- 重启 scheduler-worker 后服务恢复，已发布 revision 与成功任务记录仍保留；期间发现并修复了 Xvfb `:99` 残留锁导致的重启失败；
- shopping 使用隔离 MongoDB `c-shopping-goalmerge2` 真实拉取 revision：首次创建 80 条镜像并推进至 revision 2，重复同步无变更；后续成功从 revision 2 增量推进至 revision 4；
- shopping 订单仓储的仅展示验证确认：服务端拒绝闲鱼商品、未调用核验器、未扣减库存、未创建订单。

## 尚待真实环境验收

- `x-comments/.env` 是本地忽略文件，依据仓库安全规则未改写。部署前必须由 x-comments 部署负责人设置云端 PostgreSQL `DATABASE_URL` 和至少 32 位的 `CATALOG_SYNC_TOKEN`。
- 已确认同一云服务器部署：shopping 通过 `127.0.0.1` 调用 x-comments。真实 PostgreSQL/MongoDB 连接、生产密钥、登录态受限挂载及既有运维群或邮箱地址尚未提供，因此尚未执行真实部署。

## 下一步验收条件

1. 提供云端 PostgreSQL、MongoDB、同机回环调用与两仓库的生产 secret 注入方式；
2. 在云端部署一组 `api` 与单副本 `scheduler-worker`，确认迁移至 `20260716_0005`；
3. 执行完整采集、连续缺失两次、部分/失败采集和 409 全量重建，并保留审计证据；
4. 由 x-comments 部署负责人配置每日 PostgreSQL 备份（保留 7 天）及发往既有运维群或邮箱的最近采集、published revision 和 shopping 同步滞后告警；
5. 完成桌面与移动端浏览器验收，确认闲鱼商品仅展示且所有加购/订单绕过均被拒绝。
