# REST API

## GET /health

验证应用与数据库连接。成功：

```json
{"status":"ok","database":"ok"}
```

## POST /api/v1/crawl-jobs

请求：

```json
{"keyword":"女生发饰"}
```

立即返回 HTTP `202` 与完整任务对象，其中包含 `job_id` 和 `pending` 状态。Goal 1 只创建离线任务记录；Goal 2 接入后台 worker 后才会转换到运行终态。

## GET /api/v1/crawl-jobs/{job_id}

返回任务状态、时间、统计和安全错误。不存在返回 HTTP `404`。

## GET /api/v1/items

查询参数：

- `page`：默认 1，最小 1；
- `page_size`：默认 20，范围 1–100；
- `keyword`：可选，按规范化关键词关联过滤。

响应包含 `items`、`page`、`page_size`、`total` 和 `pages`，按 `last_seen_at DESC, item_id ASC` 稳定排序。

## GET /api/v1/items/{item_id}

返回单个数据库商品；不存在返回 HTTP `404`。

## OpenAPI

FastAPI 自动生成 `/docs` 和 `/openapi.json`。离线集成测试会生成 OpenAPI 并核对任务路径。

## POC 测试页面

`GET /` 返回 `POC internal demo`。页面只调用上述 REST API，最多轮询约 3 分钟；商品每页 12 条。静态资源位于 `/static/`。
