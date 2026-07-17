# GoalMerge2 验收审计

## 审计结论

```text
GOAL 0–4 ACCEPTED — GOAL 5 EXTERNAL ALERT, LIVE STATE-CHANGE AND APPROVED ROLLBACK DRILL PENDING
```

代码实现、离线测试、真实本机 PostgreSQL 和 2026-07-17 的同机云端部署均已有证据：迁移、单 worker、目录同步、首次备份和健康检查已实际完成。本文件仍不把“仅 systemd 日志告警”标为外部告警已通过，也不伪造连续缺失或风控失败来完成状态演练。

## Goal 0：决策已确认

已由代码和文档固定的内容：

- x-comments 独占 PostgreSQL，shopping 独占 MongoDB；只通过版本化受认证 API 交接；
- shopping 服务端变量统一为 `X_COMMENTS_SYNC_TOKEN`，x-comments 使用 `CATALOG_SYNC_TOKEN`；
- 默认策略是每 10 分钟只调度一个到期关键词，连续两次完整缺失后才下架；首批清单控制在 3 至 5 个词、每轮最多 50 条；
- 云端角色固定为可扩容 `api` 与单副本 `scheduler-worker`。

已确认：闲鱼商品可加入 shopping 购物车（每件最多 1 个），但 shopping 必须在结算页和服务端拒绝其结算、采购和订单；两个服务同机部署，Catalog Sync 只绑定 `127.0.0.1`；x-comments 部署负责人保管登录态与服务端 secret、执行 PostgreSQL 迁移和每日备份，备份保留 7 天（可按需要延长至 14 天），失败告警发送到既有运维群或邮箱。

## Goal 1：验收通过

- `Settings` 与引擎仅接受 `postgresql+psycopg://`；Compose 使用 PostgreSQL 16；
- `migrate` 一次性服务执行 Alembic，`api` 与 `scheduler-worker` 角色分离；
- PostgreSQL 迁移离线 SQL 渲染成功，GitHub Actions 配置了 PostgreSQL 服务和集成测试；
- 本机 Compose 实际升级至 `20260716_0005`，部分唯一索引已存在；
- PostgreSQL 并发集成测试及本机容器竞争演练均验证两个 scheduler 对同一关键词只能创建一条进行中任务；
- scheduler-worker 重启后成功恢复，已发布 revision 与成功任务记录仍在。

云端证据：迁移、每日 PostgreSQL 备份 timer 和每 5 分钟健康检查 timer 已部署；已手工成功生成一次带 SHA-256 校验的 PostgreSQL 逻辑备份，并将其恢复到无网络临时 PostgreSQL 容器，验证到 10 张 public 表和 22 条 `catalog_revisions` 后自动清理。外部 webhook 告警和经负责人批准的生产代码回滚演练仍待完成。

## Goal 2：验收通过

- 完整采集在短事务中发布商品、关联状态、revision 和 changes；部分/失败不发布 revision；
- 测试覆盖 active、首次/连续缺失、部分结果、多关键词仍 active、重复游标、未来失效游标、token 与快照接口；
- `/health` 输出最近成功采集、最近 published revision 与连续失败次数。

自动化契约测试已覆盖旧游标 409 与失败采集不发布 revision。真实风控/失败采集的云端端到端状态属于 Goal 5 演练，仍不得伪造。

## Goal 3 与 Goal 4：验收通过

- shopping 持久化 MongoDB 同步游标，支持增量、重复事件、409 全量重建和乱序保护；
- 公开目录从 MongoDB 镜像筛选 active 商品；购物车保留不可售条目并禁用结算，订单服务端仍重新校验；
- `xianyu:revision-sync:verify`、`xianyu:sync:verify`、`xianyu:contract:verify`、lint 和 production build 已通过；
- 本机隔离 MongoDB 已真实同步 x-comments revision，第二次以持久化游标无变更完成；仅展示订单请求被服务端拒绝。

云端证据：shopping 持久化游标已与 x-comments published revision 实际一致；独立 catalog-sync 容器继续按 10 分钟运行。桌面/移动端浏览器已验证可加购、数量固定为 1、零金额折扣显示为 `(0.0%)` 且结算按钮禁用。断网重试与真实状态切换属于 Goal 5 演练，仍待完成。

## Goal 5：已开始，尚未完全验收

已记录真实云端部署、revision 与 shopping 游标一致、首次 PostgreSQL 备份、隔离恢复和健康检查通过。后续只应在真实业务数据出现相应事件时记录状态变化；还需配置外部告警 webhook，并在取得负责人批准后演练实际代码回滚。

## 当前可重复命令

```text
x-comments
python -m pytest -q
python -m ruff check .
python -m mypy app
python -m scripts.check_chinese_docstrings

shopping
npm.cmd run xianyu:revision-sync:verify
npm.cmd run xianyu:sync:verify
npm.cmd run xianyu:contract:verify
npm.cmd run lint
npm.cmd run build
```
