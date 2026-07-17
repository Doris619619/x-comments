"""
本文件离线验证 Catalog Sync 的原子发布、缺失状态和服务间 API 契约。

它使用隔离内存数据库和 FastAPI 测试客户端，不启动浏览器、不读取登录态，也不访问真实网络。
"""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.models.catalog_keyword import CatalogKeyword
from app.models.catalog_sync import CatalogAvailability, CatalogChangeType, CatalogItemState
from app.repositories.catalog_sync import CatalogSyncRepository
from app.repositories.jobs import JobRepository
from app.schemas.item import ParsedItem

SYNC_HEADERS = {"Authorization": "Bearer offline-sync-token-0123456789abcdef"}


def make_item(item_id: str = "90001") -> ParsedItem:
    """
    创建一条最小公开商品，供离线同步测试使用。

    输入可选商品 ID；返回已校验解析模型；无外部副作用。
    """

    return ParsedItem(
        item_id=item_id,
        title=f"同步测试商品 {item_id}",
        price=Decimal("12.50"),
        image_url=None,
        item_url=f"https://www.goofish.com/item?id={item_id}",
        location="上海",
    )


def publish_run(
    session_factory: sessionmaker[Session], items: list[ParsedItem], keyword: str = "手办"
) -> tuple[int, str]:
    """
    创建并完整发布一次已配置关键词的离线采集批次。

    输入会话工厂和商品列表；返回 revision 与任务 ID；数据库异常向上抛出，副作用仅限测试库。
    """

    now = datetime.now(UTC)
    with session_factory() as session:
        job = JobRepository(session).create(keyword)
        repository = CatalogSyncRepository(session)
        run = repository.begin_run(job.job_id, keyword, now)
        published = repository.publish_complete_run(run.run_id, keyword, items, now, 2)
        session.commit()
        assert published.revision is not None
        return published.revision, job.job_id


def test_complete_runs_publish_active_then_missing_and_off_shelf_changes(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证完整成功的连续缺失两次才将商品从 active 变为 off_shelf。

    输入隔离会话工厂；断言失败抛出 AssertionError；只写入测试数据库。
    """

    with session_factory() as session:
        session.add(CatalogKeyword(category="潮玩", keyword="手办", interval_minutes=10))
        session.commit()

    first_revision, _ = publish_run(session_factory, [make_item()])
    second_revision, _ = publish_run(session_factory, [])
    third_revision, _ = publish_run(session_factory, [])

    assert [first_revision, second_revision, third_revision] == [1, 2, 3]
    with session_factory() as session:
        page = CatalogSyncRepository(session).list_changes(0, 500)
        assert page.to_revision == 3
        assert page.has_more is False
        assert [change.availability for change in page.changes] == [
            CatalogAvailability.ACTIVE,
            CatalogAvailability.SUSPECTED_MISSING,
            CatalogAvailability.OFF_SHELF,
        ]
        assert [change.change_type for change in page.changes] == [
            CatalogChangeType.UPSERT,
            CatalogChangeType.AVAILABILITY_CHANGED,
            CatalogChangeType.AVAILABILITY_CHANGED,
        ]


def test_catalog_sync_api_requires_token_and_returns_incremental_contract(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证同步 API 拒绝匿名调用并返回不含原始链接的稳定增量字段。

    输入客户端与会话工厂；断言失败抛出 AssertionError；不访问真实网络。
    """

    with session_factory() as session:
        session.add(CatalogKeyword(category="潮玩", keyword="手办", interval_minutes=10))
        session.commit()
    revision, _ = publish_run(session_factory, [make_item("90002")])

    assert client.get("/api/v1/catalog-sync/revisions/latest").status_code == 401
    latest = client.get("/api/v1/catalog-sync/revisions/latest", headers=SYNC_HEADERS)
    assert latest.status_code == 200
    assert latest.json()["revision"] == revision

    changes = client.get(
        "/api/v1/catalog-sync/changes?after_revision=0&limit=500", headers=SYNC_HEADERS
    )
    assert changes.status_code == 200
    body = changes.json()
    assert body["to_revision"] == revision
    assert body["changes"][0]["availability"] == "active"
    assert body["changes"][0]["currency"] == "CNY"
    assert "item_url" not in body["changes"][0]

    snapshot = client.get("/api/v1/catalog-sync/items?page=1&page_size=100", headers=SYNC_HEADERS)
    assert snapshot.status_code == 200
    assert snapshot.json()["total"] == 1
    assert snapshot.json()["items"][0]["item_id"] == "90002"

    item = client.get("/api/v1/catalog-sync/items/90002", headers=SYNC_HEADERS)
    assert item.status_code == 200
    assert item.json()["revision"] == revision

    repeated_changes = client.get(
        "/api/v1/catalog-sync/changes?after_revision=0&limit=500", headers=SYNC_HEADERS
    )
    assert repeated_changes.status_code == 200
    assert repeated_changes.json() == body


def test_partial_run_does_not_publish_or_increment_missing_count(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证部分成功只保存已见商品，既不发布 revision 也不改变未见商品可用状态。

    输入隔离会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    with session_factory() as session:
        session.add(CatalogKeyword(category="潮玩", keyword="手办", interval_minutes=10))
        session.commit()
    revision, _ = publish_run(session_factory, [make_item("91001")])

    with session_factory() as session:
        job = JobRepository(session).create("手办")
        repository = CatalogSyncRepository(session)
        run = repository.begin_run(job.job_id, "手办", datetime.now(UTC))
        repository.finish_incomplete_run(
            run.run_id,
            "手办",
            [],
            datetime.now(UTC),
            "页面响应不完整",
        )
        session.commit()
        changes = repository.list_changes(0, 500)
        state = session.get(
            CatalogItemState,
            {"item_id": "91001", "catalog_keyword_id": run.catalog_keyword_id},
        )

    assert revision == 1
    assert changes.to_revision == 1
    assert len(changes.changes) == 1
    assert state is not None
    assert state.availability is CatalogAvailability.ACTIVE
    assert state.missing_count == 0


def test_multi_keyword_item_remains_active_when_one_keyword_becomes_off_shelf(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证同一商品仍命中另一个 active 关键词时，不会因单个关键词缺失而全局下架。

    输入隔离会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    with session_factory() as session:
        session.add(CatalogKeyword(category="潮玩", keyword="手办", interval_minutes=10))
        session.add(CatalogKeyword(category="潮玩", keyword="模型", interval_minutes=10))
        session.commit()

    publish_run(session_factory, [make_item("92001")], "手办")
    publish_run(session_factory, [make_item("92001")], "模型")
    publish_run(session_factory, [], "手办")
    final_revision, _ = publish_run(session_factory, [], "手办")

    with session_factory() as session:
        latest = CatalogSyncRepository(session).get_latest_item_change("92001")

    assert final_revision == 4
    assert latest is not None
    assert latest.availability is CatalogAvailability.ACTIVE


def test_catalog_sync_rejects_cursor_newer_than_latest(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证失效的未来游标返回 409，shopping 可据此执行全量重建而非误判下架。

    输入客户端与会话工厂；断言失败抛出 AssertionError；只使用测试数据库。
    """

    with session_factory() as session:
        session.add(CatalogKeyword(category="潮玩", keyword="手办", interval_minutes=10))
        session.commit()
    revision, _ = publish_run(session_factory, [make_item("93001")])

    response = client.get(
        f"/api/v1/catalog-sync/changes?after_revision={revision + 1}&limit=500",
        headers=SYNC_HEADERS,
    )

    assert response.status_code == 409


def test_health_reports_latest_published_revision_and_success_time(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证健康检查输出告警所需的最近成功采集时间与已发布 revision。

    输入客户端和会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    with session_factory() as session:
        session.add(CatalogKeyword(category="潮玩", keyword="手办", interval_minutes=10))
        session.commit()
    revision, _ = publish_run(session_factory, [make_item("94001")])

    health = client.get("/health")

    assert health.status_code == 200
    assert health.json()["last_published_revision"] == revision
    assert health.json()["last_published_at"] is not None
    assert health.json()["last_successful_crawl_at"] is not None
    assert health.json()["consecutive_failed_runs"] == 0
