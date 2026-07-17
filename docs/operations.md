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
生产库执行 `pg_restore --clean`。需要 14 天保留时，只修改 systemd service 的 `RETENTION_DAYS=14` 并
`daemon-reload` 后重启 timer。

## 健康检查与告警

`scripts/check_deployment_health.sh` 每次检查：

- x-comments `/health` 的数据库状态、最后成功采集时间和已发布 revision；
- shopping MongoDB 中持久化的同步游标与最后成功同步时间；
- shopping Nginx 反向代理是否仍返回 Next.js 响应。

默认阈值为 60 分钟，覆盖首批 3～5 个关键词“每 10 分钟只处理一个到期词”的正常轮转窗口。脚本失败会让 systemd service 失败并留下 `DEPLOYMENT_ALERT` 日志；若要把告警
真正发送到现有运维群或邮箱，运维必须提供兼容 `{"text":"..."}` JSON 的 webhook 地址，并仅在服务器
创建权限为 `0600` 的 `/etc/x-comments-monitor.env`：

```dotenv
ALERT_WEBHOOK_URL=https://existing-operations-webhook.example/...
```

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
