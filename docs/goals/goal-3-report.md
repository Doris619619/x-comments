# Goal 3 报告：最小前端闭环

## 目标与状态

完成只用于 POC 验收的最小页面，实现关键词输入、任务创建、有限轮询、统计/错误和数据库商品分页。

```text
PASSED
```

允许进入 Goal 4。

## 实际完成

- `/` 提供标记为 `POC internal demo` 的响应式页面；
- 默认关键词“女生发饰”，无需改代码即可更换；
- `POST /api/v1/crawl-jobs` 创建任务并显示 `pending/running`；
- 最多 120 次、每 1.5 秒有限轮询，不会无限转圈；
- 显示发现、新增、更新、重复和错误统计；
- 失败/风控状态显示后端明确错误；
- 完成后从 `/api/v1/items` 自动加载数据库商品；
- 卡片显示主图、标题、价格、地区和闲鱼原始链接；
- 每页 12 条，支持上一页/下一页；
- 页面初次加载和刷新均读取历史数据库商品；
- 静态 HTML/JS 没有硬编码商品，也不访问闲鱼。

## 修改文件

- `app/api/demo.py`
- `app/static/index.html`
- `app/static/app.js`
- `app/static/styles.css`
- `app/main.py`
- `tests/test_api.py`
- 本报告及 API/架构文档

## 自动测试

```text
python -m pytest -q
9 passed

python -m ruff check app tests alembic scripts
All checks passed

python -m mypy app
Success: no issues found
```

静态页面测试验证默认关键词、内部演示标记、任务/商品 API、有限轮询常量，以及真实商品 ID 不存在于静态文件中。

## 浏览器人工验收

使用实际 Uvicorn 服务和浏览器执行：

1. 打开页面即显示默认关键词和 50 条历史数据库商品；
2. 点击下一页，从第 1 页切换到第 2 页，显示 12 张卡片；
3. 点击“开始采集”，创建任务 `9442d976-d983-4de8-835c-e3dead4311c5`；
4. 页面显示任务 `running`；
5. 任务最终 `succeeded`，发现 50、新增 7、更新 43、错误 0；
6. 页面自动回到第 1 页并显示数据库共 57 条；
7. 刷新页面后仍显示默认关键词、12 张卡片和共 57 条数据库商品。

页面商品链接为真实 `https://www.goofish.com/item?id=...`，且卡片来自 API 响应。

## 硬验收结论

Goal 3 的输入、即时任务 ID、运行状态、成功刷新、数据库来源、真实链接、明确错误、有限轮询、刷新恢复、任意关键词和完整真实演示均通过。

最终状态 `PASSED`，允许启动 Goal 4。

