# Goal 1 报告：离线后端骨架

## 目标与状态

目标是在不访问闲鱼的情况下完成可测试后端、数据库迁移、任务模型、商品解析器和 REST API。

```text
PASSED
```

允许进入 Goal 2。

## 实际完成

- FastAPI 应用、健康检查和 OpenAPI；
- SQLAlchemy 2 商品、关键词关联和任务模型；
- Alembic 初始迁移与 SQLite 空库验证；
- PostgreSQL `psycopg` 驱动和环境变量 URL 预留；
- 任务创建/查询、商品分页/详情 API；
- 搜索响应纯 JSON 解析器及脱敏 fixture；
- 商品 ID/URL 交叉校验、全局去重、多关键词关联和 `last_seen_at` 更新；
- `.gitignore` 登录态保护；
- 架构、数据模型、API 和限制文档。

Goal 1 没有访问闲鱼，也没有用伪造执行器把任务标记成功。

## 修改文件

新增 `app/`、`alembic/`、`tests/`、`pyproject.toml`、`.env.example`、`.gitignore`，以及 `docs/architecture.md`、`docs/data-model.md`、`docs/api.md`、`docs/known-limitations.md`。

## 执行命令与结果

```text
python -m pytest -q
7 passed

python -m ruff check app tests alembic
All checks passed!

python -m mypy app
Success: no issues found in 23 source files

DATABASE_URL=sqlite:///./data/goal1-empty.sqlite3 alembic upgrade head
Running upgrade -> 20260711_0001

alembic current
20260711_0001 (head)
```

测试只使用内存 SQLite 和 `tests/fixtures/search_response.json`，没有真实网络请求。

## 人工验收

- `/health` 由 TestClient 实际请求并返回数据库正常；
- `/openapi.json` 实际生成且包含任务 API；
- 创建任务在测试中小于 1 秒返回 HTTP 202、`job_id` 和 `pending`；
- 空商品分页和 404 协议已实际请求；
- 从空 SQLite 文件实际执行迁移到 head。

## 硬验收核对

| 标准 | 结果 |
| --- | --- |
| 数据库迁移成功、SQLite 可运行 | 通过 |
| PostgreSQL 配置预留 | 通过 |
| `item_id` 唯一，重复不重复插入 | 通过 |
| 同一商品关联多个关键词 | 通过 |
| 创建任务立即返回 `job_id` | 通过 |
| 商品分页和关键词过滤 | 通过 |
| 测试不依赖真实闲鱼 | 通过 |
| pytest 全通过 | 通过 |
| `/docs`/OpenAPI 和 `/health` 正常 | 通过 |
| 未伪造真实爬虫执行 | 通过 |

## 未解决问题

真实 Playwright worker、登录态校验、两次真实采集和 10 条抽查属于 Goal 2。FastAPI TestClient 对当前 Starlette/httpx 组合产生一个上游弃用警告，不影响行为，后续依赖升级时复核。

## 返工与继承

没有失败项或需删除的复杂组件。Goal 2 可以继承模型、迁移、API、仓储、解析器和离线测试。若真实响应与 fixture 字段不一致，必须更新解析器和 fixture 后重新运行 Goal 1 全套回归。

## 最终结论

全部硬验收标准满足，最终状态 `PASSED`，允许启动 Goal 2。
