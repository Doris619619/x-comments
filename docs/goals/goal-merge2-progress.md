# GoalMerge2 进度报告：x-comments 与 shopping 的定时目录同步

## 当前状态

```text
IN PROGRESS
```

本报告记录已经落地的实现、真实本机演练和 2026-07-17 的云端部署证据；未配置的外部告警投递不会被冒充为已完成。

## 已完成实现

- x-comments 运行时数据库统一为 PostgreSQL；Compose 包含 PostgreSQL 16、一次性迁移、API 和唯一 scheduler-worker。
- API 不再启动浏览器；scheduler-worker 轮询持久化 pending 任务并按 10 分钟调度。数据库局部唯一索引和 PostgreSQL 并发集成测试共同验证同一关键词不会被两个 scheduler 重复创建。
- 一次完整采集在单个事务内发布商品、目录状态、采集批次和递增修订号；读取方不会看到半批次结果，因此不需要手工锁表。
- 目录状态包括 `active`、`suspected_missing`、`sold`、`off_shelf` 和 `unknown`。连续两次完整采集缺失后才自动标记为下架，部分结果、风控和失败不会触发下架。
- x-comments 提供携带 Bearer Token 的目录同步接口：最新修订、增量变更、全量快照和单商品读取；`/health` 同时返回最近成功采集、最近发布 revision 和连续失败次数，供告警使用。
- shopping 保持原有 MongoDB 商品镜像；独立同步容器每 10 分钟经共享 Docker 私有网络以 `x-comments-api:8000` 调用受认证 API 拉取 x-comments 增量，而不直连或迁移 x-comments 的 PostgreSQL。
- shopping 将下架/售出/疑似缺失商品从公开列表隐藏，并保留购物车条目；购物车显示不可购买提示、禁止结算，后端订单校验仍是最终保护。
- 同步游标持久化在 shopping 的 `XianyuCatalogSyncState` 中；增量历史过期时会收到 409 并执行全量重建。
- 负责人已确认闲鱼商品可加入 shopping 购物车（每件最多 1 个），但仍禁止结算、扣款、采购和订单创建；服务端拒绝伪造请求且不调用详情核验或扣减库存。

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

## 已完成真实云端部署（2026-07-17）

- 同一台云服务器已运行 x-comments 的 PostgreSQL、一次性迁移、单副本 API、单副本 scheduler-worker，以及 shopping 的 MongoDB、web 和独立 catalog-sync 容器；Catalog Sync 对宿主机仅绑定回环地址，容器间使用共享 Docker 私有网络。
- 云端 `/health` 与 shopping 的持久化同步游标已实际核对：x-comments revision 和 shopping `lastAppliedRevision` 一致，最近成功采集与同步均在健康阈值内。
- 已安装 `x-comments-postgres-backup.timer`（每日 02:20）与 `x-comments-deployment-health.timer`（每 5 分钟）。手工备份已成功生成 custom-format PostgreSQL dump 和 SHA-256 校验文件，备份保留策略为 7 天。
- shopping 已实际同步 active 闲鱼镜像；`/xianyu` 可列出商品，首页响应也带入首批 8 条闲鱼商品。闲鱼商品仅能以 CNY 参考价加入购物车（每件 1 个），结算、订单和履约仍被禁止。
- 已完成桌面及移动端浏览器验收：闲鱼商品详情可保存到购物车、数量固定为 1、购物车的零金额折扣显示为 `(0.0%)` 而非 `NaN`，且“購入手続きへ”按钮处于禁用状态。

## 尚待确认的运行项

- 云端 secret、登录态与数据库凭证继续只保留在受限服务器配置中，未写入 Git。外部告警 webhook 尚未提供，因此健康检查失败时当前只写入 systemd 日志；拿到既有运维群或邮箱的兼容 webhook 后，才可开启外发告警。
- 本次云端采集/同步链路已运行，但仍需在业务方确认的关键词清单上持续观察完整采集、两次连续缺失和恢复场景；不能伪造商品消失来宣称下架演练完成。

## 下一步验收条件

1. 提供既有运维群或邮箱的兼容 webhook，并以 `/etc/x-comments-monitor.env` 的受限权限配置外部告警；
2. 在确认的关键词清单上记录完整采集、连续缺失两次、部分失败采集和 409 全量重建的审计证据；
3. 在不影响线上业务的前提下，补齐可执行的回滚演练记录，并核对回滚后服务健康、同步游标与备份可恢复性。
