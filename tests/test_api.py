"""
本文件测试健康检查、任务立即返回、OpenAPI 与商品 API。

它使用内存数据库，不访问真实闲鱼或本地登录态。
"""

import time
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.models.catalog_keyword import CatalogKeyword
from app.repositories.items import ItemRepository
from app.schemas.item import ParsedItem


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


def test_item_api_contract_with_nullable_display_fields(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证商品列表和详情使用同一条记录，并稳定表示缺图和无地区。

    输入内存客户端与会话工厂；断言失败抛出 AssertionError；副作用仅为内存数据库写入。
    """

    expected_item = ParsedItem(
        item_id="10001",
        title="测试发饰",
        price=Decimal("12.50"),
        image_url=None,
        item_url="https://www.goofish.com/item?id=10001",
        location=None,
        source="xianyu",
    )
    with session_factory() as session:
        ItemRepository(session).upsert_many("女生发饰", [expected_item], datetime.now(UTC))

    listed = client.get("/api/v1/items?page=1&page_size=1&keyword=女生发饰")
    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert item["item_id"] == "10001"
    assert item["title"] == "测试发饰"
    assert item["price"] == "12.50"
    assert item["image_url"] is None
    assert item["location"] is None
    assert item["source"] == "xianyu"
    assert item["last_seen_at"]
    assert client.get("/api/v1/items/10001").json() == item

    assert client.get("/api/v1/items?page=0").status_code == 422
    assert client.get("/api/v1/items?page_size=101").status_code == 422


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


def test_catalog_keywords_api_returns_enabled_search_list(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证杂货铺搜索清单 API 只公开启用项及其调度信息。

    输入内存客户端与会话工厂；断言失败抛出 AssertionError；副作用仅为内存数据写入。
    """

    with session_factory() as session:
        session.add(CatalogKeyword(category="潮玩收藏", keyword="手办", interval_minutes=60))
        session.add(CatalogKeyword(category="内部", keyword="禁用词", is_enabled=False))
        session.commit()

    response = client.get("/api/v1/catalog-keywords")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 1,
            "category": "潮玩收藏",
            "keyword": "手办",
            "interval_minutes": 60,
            "last_scheduled_at": None,
            "note": None,
        }
    ]


def test_items_api_filters_by_catalog_category(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证首页可按持久化杂货铺分类查询关联商品。

    输入内存客户端与会话工厂；断言失败抛出 AssertionError；副作用仅为内存数据库写入。
    """

    item = ParsedItem(
        item_id="20001",
        title="测试手办",
        price=Decimal("28.00"),
        image_url=None,
        item_url="https://www.goofish.com/item?id=20001",
        location="上海",
    )
    with session_factory() as session:
        session.add(CatalogKeyword(category="潮玩手办", keyword="手办"))
        session.commit()
        ItemRepository(session).upsert_many("手办", [item], datetime.now(UTC))

    response = client.get("/api/v1/items?category=潮玩手办")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["item_id"] == "20001"
