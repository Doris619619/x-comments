# Goal 2 报告：真实闲鱼关键词采集

## 目标与最终状态

目标是使用用户合法人工登录态真实搜索“女生发饰”，有限采集、去重入库并完成两次运行与 10 条抽查。

```text
PASSED
```

允许启动 Goal 3。

## 已完成实现

- Playwright storage state 复用；
- 有头/无头配置；
- 单 worker 串行执行，禁止账号并发；
- 最多 3 页/50 条和页间限速；
- 搜索响应纯解析、ID/URL 交叉校验；
- 任务 `pending → running → 终态` 与统计落库；
- 登录页、验证码、403/429、访问频繁、非法访问和异常空结果安全停止；
- 两次真实访问记录和失败上下文；
- 登录态 Git 忽略和不打印策略。
- 第一次有效任务新增 50 条；第二次新增 0、更新 50，数据库仍为 50 条；
- 随机 10 条 ID、链接、标题、价格和主图全部正确。

## 执行命令

```text
python -m alembic upgrade head
python -m scripts.run_crawl_once --keyword "女生发饰"
XIANYU_HEADLESS=false python -m scripts.run_crawl_once --keyword "女生发饰"
python -m scripts.debug_search_access
XIANYU_HEADLESS=false python -m scripts.audit_live_items
```

## 实际结果

前期无头访问收到“非法访问”限制，初版客户端又误取了空的中间搜索响应；两次均未写入商品。修复后两次有效任务分别得到 `50 new` 和 `0 new / 50 updated`，随机十条抽查 `10/10`。详情见 `docs/live-test-report.md`。

## 验收与返工

数量、两次去重、`last_seen_at` 更新、任务统计和 10 条字段抽查均有数据库、任务记录、页面卡片及本地截图证据。Goal 2 全部硬验收满足。

## 下一 Goal

最终状态 `PASSED`，允许进入 Goal 3 最小前端闭环。
