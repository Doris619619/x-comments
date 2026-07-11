# 数据模型

## crawl_jobs

一次 API 创建对应一条任务。`job_id` 为 UUID 字符串主键；`keyword` 保存用户展示关键词；`status` 支持：

- `pending`
- `running`
- `succeeded`
- `partially_succeeded`
- `failed`
- `blocked_by_auth_or_risk_control`

统计字段为 `discovered_count`、`new_count`、`updated_count`、`duplicate_count`、`error_count`，并包含创建、开始、结束时间和安全错误消息。

## items

`item_id` 是闲鱼商品唯一 ID 和主键。模型包含：

- `title`
- `price`：`NUMERIC(12,2)`，避免浮点误差
- `image_url`
- `item_url`
- `location`
- `source`
- `first_seen_at`
- `last_seen_at`
- `created_at`
- `updated_at`

缺失的图片或地区保存 `NULL`，不得伪造。重复采集通过 `item_id` 更新同一行并推进 `last_seen_at`。

## keywords 与 item_keywords

`keywords.normalized_value` 唯一，另存 `display_value`。`item_keywords` 以 `(item_id, keyword_id)` 为复合主键，并记录该关联的首次和最近发现时间。

因此商品是全局去重的，同时同一商品可关联多个关键词，不需要在商品表中保存单一关键词字符串。

## 迁移

初始迁移为 `20260711_0001`。执行：

```bash
alembic upgrade head
```

迁移已从空 SQLite 数据库验证。生产 PostgreSQL 使用同一 SQLAlchemy 元数据和 Alembic 版本链；当前 Goal 未要求连接实际 PostgreSQL 服务。

