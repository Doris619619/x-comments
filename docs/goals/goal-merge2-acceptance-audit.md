# GoalMerge2 验收审计

## 审计结论

```text
NOT READY FOR CLOUD ACCEPTANCE
```

代码实现、离线测试、真实本机 PostgreSQL、两服务运行和重启恢复已具备证据；云端部署所需
策略已经确认，但尚未拿到生产 secret、数据库连接和告警地址执行部署。本文件不把本机演练误标为云端上线通过。

## Goal 0：决策已确认

已由代码和文档固定的内容：

- x-comments 独占 PostgreSQL，shopping 独占 MongoDB；只通过版本化受认证 API 交接；
- shopping 服务端变量统一为 `X_COMMENTS_SYNC_TOKEN`，x-comments 使用 `CATALOG_SYNC_TOKEN`；
- 默认策略是每 10 分钟只调度一个到期关键词，连续两次完整缺失后才下架；首批清单控制在 3 至 5 个词、每轮最多 50 条；
- 云端角色固定为可扩容 `api` 与单副本 `scheduler-worker`。

已确认：闲鱼商品可加入 shopping 购物车（每件最多 1 个），但 shopping 必须在结算页和服务端拒绝其结算、采购和订单；两个服务同机部署，Catalog Sync 只绑定 `127.0.0.1`；x-comments 部署负责人保管登录态与服务端 secret、执行 PostgreSQL 迁移和每日备份，备份保留 7 天（可按需要延长至 14 天），失败告警发送到既有运维群或邮箱。

## Goal 1：代码已准备，真实 PostgreSQL 未验收

- `Settings` 与引擎仅接受 `postgresql+psycopg://`；Compose 使用 PostgreSQL 16；
- `migrate` 一次性服务执行 Alembic，`api` 与 `scheduler-worker` 角色分离；
- PostgreSQL 迁移离线 SQL 渲染成功，GitHub Actions 配置了 PostgreSQL 服务和集成测试；
- 本机 Compose 实际升级至 `20260716_0005`，部分唯一索引已存在；
- PostgreSQL 并发集成测试及本机容器竞争演练均验证两个 scheduler 对同一关键词只能创建一条进行中任务；
- scheduler-worker 重启后成功恢复，已发布 revision 与成功任务记录仍在。

缺失证据：云端 PostgreSQL 的迁移、备份、监控与部署回滚尚未执行。

## Goal 2：代码已准备，真实数据库契约未验收

- 完整采集在短事务中发布商品、关联状态、revision 和 changes；部分/失败不发布 revision；
- 测试覆盖 active、首次/连续缺失、部分结果、多关键词仍 active、重复游标、未来失效游标、token 与快照接口；
- `/health` 输出最近成功采集、最近 published revision 与连续失败次数。

缺失证据：保留历史后的旧游标 409、真实风控/失败采集的云端端到端状态尚未执行。

## Goal 3 与 Goal 4：代码和构建已准备，双服务验收未完成

- shopping 持久化 MongoDB 同步游标，支持增量、重复事件、409 全量重建和乱序保护；
- 公开目录从 MongoDB 镜像筛选 active 商品；购物车保留不可售条目并禁用结算，订单服务端仍重新校验；
- `xianyu:revision-sync:verify`、`xianyu:sync:verify`、`xianyu:contract:verify`、lint 和 production build 已通过；
- 本机隔离 MongoDB 已真实同步 x-comments revision，第二次以持久化游标无变更完成；仅展示订单请求被服务端拒绝。

缺失证据：云端 10 分钟同步、断网重试、状态切换与桌面/移动端浏览器验收尚未执行。

## Goal 5：尚未开始真实云端演练

必须在负责人提供云端 PostgreSQL、MongoDB、同机回环调用、secret、告警和登录态挂载方式后，记录真实采集、
revision 发布、shopping 同步、状态变化、重启恢复和最终 Git 秘密审查结果。

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
