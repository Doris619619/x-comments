# 云端运维：备份、健康检查与告警

本文是同机部署 `x-comments` 与 `c-shopping-jp-poc` 的运行手册。两个服务各自保有数据库：
x-comments 使用 PostgreSQL，shopping 使用 MongoDB；shopping 只经 Docker 私有网络调用受 Bearer
保护的 Catalog Sync API，绝不直连 PostgreSQL。

## PostgreSQL 每日备份

`scripts/backup_postgres.sh` 通过 PostgreSQL 容器内的 `pg_dump` 产生 custom-format 逻辑备份，并在
`/var/backups/x-comments/postgresql` 保留最近 7 天的 `.dump` 与 SHA-256 校验文件。备份目录权限为
`0700`，文件权限为 `0600`；脚本不读取或打印数据库密码。

安装 systemd 模板（路径按当前云服务器目录编写）后立即手动运行一次，确认文件实际存在：

```bash
sudo install -m 0644 deploy/systemd/x-comments-postgres-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/x-comments-postgres-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now x-comments-postgres-backup.timer
sudo systemctl start x-comments-postgres-backup.service
sudo systemctl status x-comments-postgres-backup.service --no-pager
sudo ls -l /var/backups/x-comments/postgresql
```

恢复演练必须在隔离 PostgreSQL 实例进行；不得把生产备份导入 shopping MongoDB，也不得在未确认的
生产库执行 `pg_restore --clean`。仓库提供如下检查脚本：它会校验最新 dump 的 SHA-256，将 dump 恢复到
无网络的临时 `postgres:16-alpine` 容器，检查 public 表和 `catalog_revisions`，并始终删除临时容器。

```bash
cd /home/ubuntu/opt/x-comments
sudo bash scripts/verify_postgres_backup_restore.sh
# 预期：isolated_restore=passed tables=<正数> catalog_revisions=<非负整数> dump=<文件名>
```

这是“备份可恢复”演练，不是生产回滚。生产出现需要回滚的故障时，必须先取得值班负责人批准、选定明确的
Git 提交和经过校验的备份；随后分别按两个仓库的 Compose 文档回退服务，先验证 `/health`、shopping 同步
游标与前台页面，再决定是否恢复生产 PostgreSQL。不得为了演练在业务运行期间直接 `pg_restore --clean`。

需要 14 天保留时，只修改 systemd service 的 `RETENTION_DAYS=14` 并 `daemon-reload` 后重启 timer。

## 健康检查与告警

`scripts/check_deployment_health.sh` 每次检查：

- x-comments `/health` 的数据库状态、最后成功采集时间和已发布 revision；
- shopping MongoDB 中持久化的同步游标与最后成功同步时间；
- shopping Nginx 反向代理是否仍返回 Next.js 响应。

默认阈值为 60 分钟，覆盖首批 3～5 个关键词“每 10 分钟只处理一个到期词”的正常轮转窗口。脚本失败会让 systemd service 失败并留下 `DEPLOYMENT_ALERT` 日志；若要把告警
真正发送到现有运维群或邮箱，运维可提供兼容 `{"text":"..."}` JSON 的 webhook，或使用服务器已经配置好的
本机 MTA（例如 Postfix relay）。仅在服务器创建权限为 `0600` 的 `/etc/x-comments-monitor.env`：

```dotenv
ALERT_WEBHOOK_URL=https://existing-operations-webhook.example/...
# 仅在 `command -v sendmail` 可用且服务器 MTA 已配置时启用：
ALERT_EMAIL_TO=operations@example.com
```

`ALERT_EMAIL_TO` 只保存收件人；它不携带 SMTP 密码。健康脚本在 MTA 不存在或投递失败时会留下
`DEPLOYMENT_ALERT` 日志，且不会把失败的邮件投递说成成功。若服务器没有现成 MTA，优先提供 webhook，
或由运维在服务器密钥管理中配置 SMTP relay，绝不将 SMTP 凭证写入 Git。

不要把此文件、Cookie、token、数据库密码或备份提交到 Git。安装和检查命令：

```bash
sudo install -m 0644 deploy/systemd/x-comments-deployment-health.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/x-comments-deployment-health.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now x-comments-deployment-health.timer
sudo systemctl start x-comments-deployment-health.service
sudo journalctl -u x-comments-deployment-health.service -n 50 --no-pager
```

未配置 webhook 前，健康检查仍会记录失败，但不应宣称已经具备外部告警投递。

## 已验证回滚基线

截至 2026-07-17，以下提交是已在同一云服务器验证通过的回滚基线，必须记录在仓库而不是依赖聊天记忆：

| 服务 | 回滚提交 | 已验证内容 |
| --- | --- | --- |
| x-comments | `a86c5b3` | PostgreSQL、单 worker、真实采集、revision 40、shopping 同步、健康检查与隔离备份恢复 |
| shopping | `efb536e` | 闲鱼目录展示、CNY 参考价加购、数量固定为 1、禁止结算 |

当前服务器已运行这两个基线版本，因此“回退到基线”现在是无操作。后续有代码部署时，先记录新提交；若新版本需要回退，
仅在工作日 22:00–24:00（UTC+8）窗口内回到相应基线，分别重建各自 Compose 服务，再依次验证 x-comments
`/health`、shopping 同步游标和 `/xianyu` 页面。不得把一个仓库的提交用于另一个仓库，也不得直接恢复生产数据库来代替代码回滚。
