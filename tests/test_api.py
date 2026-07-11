"""
本文件测试健康检查、任务立即返回、OpenAPI 与商品 API。

它使用内存数据库，不访问真实闲鱼或本地登录态。
"""

import time

from fastapi.testclient import TestClient


def test_health_and_openapi(client: TestClient) -> None:
    """
    验证健康检查与 `/docs` 所需 OpenAPI 可生成。

    输入测试客户端；断言失败抛出 AssertionError；只执行内存查询。
    """

    assert client.get("/health").json() == {"status": "ok", "database": "ok"}
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/v1/crawl-jobs" in schema.json()["paths"]


def test_create_job_returns_immediately(client: TestClient) -> None:
    """
    验证创建任务不等待爬虫并能查询状态。

    输入测试客户端；断言失败抛出 AssertionError；副作用仅为内存任务记录。
    """

    started = time.perf_counter()
    response = client.post("/api/v1/crawl-jobs", json={"keyword": " 女生发饰 "})
    elapsed = time.perf_counter() - started
    assert response.status_code == 202
    assert elapsed < 1
    body = response.json()
    assert body["keyword"] == "女生发饰"
    assert body["status"] == "pending"
    assert client.get(f"/api/v1/crawl-jobs/{body['job_id']}").status_code == 200


def test_empty_items_page_and_missing_detail(client: TestClient) -> None:
    """
    验证空商品分页和不存在详情的协议行为。

    输入客户端；断言失败抛出 AssertionError；无写入副作用。
    """

    response = client.get("/api/v1/items?page=1&page_size=10&keyword=女生发饰")
    assert response.status_code == 200
    assert response.json() == {"items": [], "page": 1, "page_size": 10, "total": 0, "pages": 0}
    assert client.get("/api/v1/items/missing").status_code == 404


def test_demo_page_uses_api_without_hardcoded_items(client: TestClient) -> None:
    """
    验证 POC 页面存在默认关键词和 API 脚本且没有硬编码商品。

    输入客户端；断言失败抛出 AssertionError；只读取静态文件。
    """

    page = client.get("/")
    script = client.get("/static/app.js")
    assert page.status_code == 200
    assert "女生发饰" in page.text
    assert "POC internal demo" in page.text
    assert "/api/v1/crawl-jobs" in script.text
    assert "/api/v1/items" in script.text
    assert "MAX_POLL_ATTEMPTS" in script.text
    assert "900641866637" not in page.text + script.text
