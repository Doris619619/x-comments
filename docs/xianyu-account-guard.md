# 闲鱼账号跨进程串行保护

## 目标

同一份闲鱼登录态只能同时被一个 Playwright 操作占用。API 容器中的结算核验与
`scheduler-worker` 容器中的定时采集虽然属于不同进程，但必须共享同一互斥边界。

## 实现

`app/services/xianyu_account_guard.py` 提供统一的 `AccountAccessGuard` 协议：

1. 每个进程先获取一个 `asyncio.Lock`，消除本事件循环内的并发竞争；
2. PostgreSQL 环境再使用稳定资源名生成的有符号 64 位 key，轮询
   `pg_try_advisory_lock`；
3. 锁使用专用数据库 session 持有，退出时执行 `pg_advisory_unlock` 后关闭连接；
4. 解锁失败时连接会被 invalidate，避免带有 session lock 的连接返回连接池；
5. 获取或释放期间收到协程取消时，会等待清理线程完成再传播取消；连接断开时 PostgreSQL
   也会自动释放该 session 持有的锁；
6. SQLite 离线测试不执行 PostgreSQL SQL，只保留进程内串行语义。

API verifier 与 scheduler worker 都在 `app/main.py` 中构造相同类型的 guard，并使用固定
资源名 `x-comments:xianyu:primary-account`。因此它们连接同一 PostgreSQL 数据库时会竞争
同一个 advisory key。

## 兼容性与限制

爬虫和核验器仍允许测试传入原生 `asyncio.Lock`；适配器只提供进程内互斥。生产环境不得
用该适配方式替代数据库 guard。当前系统只支持一个闲鱼账号；若未来增加账号，必须为每个
账号分配显式、稳定且不含凭据的资源名，并禁止把 Cookie、Token 或登录态写入资源名或日志。
