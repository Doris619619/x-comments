# 云端运维：备份、健康检查与告警

本文是同机部署 `x-comments` 与 `c-shopping-jp-poc` 的运行手册。两个服务各自保有数据库：
x-comments 使用 PostgreSQL，shopping 使用 MongoDB；shopping 只经 Docker 私有网络调用受 Bearer
保护的 Catalog Sync API，绝不直连 PostgreSQL。

`PROCUREMENT_SOURCE_ITEM_ALLOWLIST` 必须配置为英文逗号分隔的获批闲鱼 `item_id`，v1 与 v2
都失败关闭；v2 Canary 还必须由商城 Root 手工复输商品 ID 后逐任务授权。当前生产
`PROCUREMENT_CHAT_ENABLED=true` 用于受控草稿验收，
`PROCUREMENT_AUTO_SEND_ENABLED=false` 保持关闭。仅做一个白名单商品的低流量 Canary 时，
也必须人工复核商品 ID 与商城任务级授权，
且不得把客户资料、支付资料、Cookie 或账号密码写入环境变量。
该白名单必须同时出现在 API 与唯一 scheduler-worker 的运行时配置中；运维验收只比较两个容器的
条目数量和脱敏摘要，不在终端、工单或日志中回显真实商品 ID。

## 采购聊天部署与只读标定

初次部署数据库迁移和代码时两个开关都关闭；完成只读标定和草稿验收后，生产基线为：

```dotenv
PROCUREMENT_CHAT_ENABLED=true
PROCUREMENT_AUTO_SEND_ENABLED=false
```

部署后执行 `alembic upgrade head`，再重启 API 与唯一的 scheduler-worker。关闭聊天开关会停止领取
新的聊天动作；重新开启前必须先取消已经过期的排队任务，不能让旧任务在恢复时突然发送。

真实页面选择器已经在服务器现有登录态上完成只读标定：商品聊天入口使用唯一可见、文案为
“聊一聊”或“我想要”且带完整 `itemId`/`peerUserId` 参数的 IM 链接，明确排除侧栏“消息”，
不依赖会漂移的动态类名；入口首次可见后允许约两秒
只读等待身份 URL 稳定，并原子读取可见性和身份 URL，但不会因此重复导航或触发写操作。聊天输入框
使用页面明确的消息占位文本，
发送按钮和消息方向使用稳定 class 前缀；消息列表是
`column-reverse`，客户端会转换为时间正序。账号绑定使用登录态 `tracknick` 的 SHA-256，不在环境、
日志或标定报告保存原账号文本。生产只配置 64 位小写十六进制摘要：

```dotenv
XIANYU_EXPECTED_ACCOUNT_ID=<只读标定得到的 tracknick SHA-256>
```

页面结构变化后只能运行只读标定脚本；它不得填写输入框、点击发送或输出聊天正文：

```bash
xvfb-run -a python scripts/calibrate_procurement_chat_dom.py \
  --item-id <经人工批准的真实商品 ID>
```

标定结果只保留选择器计数、消息方向布局、URL 参数名的摘要和账号匹配结果，不保存 Cookie、真实卖家
身份或聊天内容。遇到登录失效、验证码、403、429、HTTP 200 的
`_____tmd_____/punish*` 风控流程、商品/卖家/账号不一致或 DOM 不唯一时立即停止。
TMD 风控只能通过账号持有人使用官方登录流程刷新同一账号状态并重新只读标定处理；不得增加重试、
指纹伪装或验证码绕过。

回复轮询按“发送后 2 分钟、5 分钟、10 分钟、此后每 15 分钟”退避，最长等待 24 小时。每次读取
基线后的全部新消息，不只取最后一条。彦诗筛选源不再执行商品详情页库存或价格复核；每次打开聊天
仍须确认商品、卖家和买家账号绑定。草稿必须逐字产生键盘输入事件，再点击语义确认的“发 送”按钮；
若约十秒内无法确认出现同正文的本人消息，直接转人工且不重试，禁止再回退 Enter 或再次点击。

发送按钮只允许通过可见鼠标轨迹完成一次中心点按下与松开。点击窗口必须同时观察到携带完整草稿的
官方 XHR/fetch 请求或 WebSocket 出站帧；只有该网络证据和页面本人同文回显都成立才记为成功。
运维只可查看脱敏端点指纹、传输类型、方法和粗粒度响应状态，不得把请求正文、URL、Cookie 或账号
信息写入日志。网络证据缺失、响应不确定或页面回显缺失一律转人工，禁止再次点击或改用 Enter。

上线顺序必须是：

1. 两个开关均关闭，验证迁移、API、任务和后台消息时间线；
2. 只打开聊天开关，验证三方聊天绑定和 DeepSeek 草稿，确认页面没有发送；
3. 由 Root 创建一个 `operator_canary`，人工复输商品 ID 并单独授权；
4. 临时打开自动发送开关，在同一条 Canary 内发送首条正常库存询问并等待卖家回复；
5. 卖家回复后由同一任务生成第二/第三轮安全追问；最多三条 AI 出站消息，问题已充分、遇到风险或
   达到上限后立即转人工并关闭自动发送；
6. 验收连续回复、重复回调、崩溃恢复和强制停止后，再讨论已付款订单。

单条 Canary 完成时必须在审计中同时看到 `request_observed=true` 和新增本人同文消息指纹；若前者
为 false，即使页面暂时显示文字也不能宣布发送成功。关闭自动发送后应重启 worker 并再次检查环境
中的开关值，过期或结果不确定的任务必须取消，不能复用。

任一阶段都不自动拍下、付款、填写地址、确认收货或处理验证码。

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
