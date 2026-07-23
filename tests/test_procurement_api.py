"""
本文件离线验证本地采购任务 API 的鉴权、目录校验、幂等创建、查询和取消。

测试使用隔离 SQLite 和已发布 Catalog fixture，不调用大模型、不启动 Playwright，
也不执行购买、付款或真实消息发送。
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.catalog_keyword import CatalogKeyword
from app.models.catalog_sync import CatalogAvailability, CatalogChange
from app.models.procurement import (
    ConversationMessage,
    ConversationMessageDirection,
    ConversationMessageStatus,
    ConversationSenderRole,
    ConversationSession,
    ProcurementExecutionTask,
)
from app.repositories.catalog_sync import CatalogSyncRepository
from app.repositories.jobs import JobRepository
from app.schemas.item import ParsedItem

PROCUREMENT_HEADERS = {
    "Authorization": "Bearer offline-procurement-token-0123456789abcdef",
    "Idempotency-Key": "procurement-test-key-00000001",
}


def procurement_payload(
    item_id: str = "81001",
    price_cny_minor: int = 1250,
    contract_version: int = 1,
) -> dict[str, object]:
    """
    创建不含客户资料和商品 URL 的最小采购任务请求。

    输入商品 ID、整数分价格和契约版本，返回新字典；无异常和外部副作用。
    """

    return {
        "contract_version": contract_version,
        "task_id": str(uuid.uuid4()),
        "source": {
            "platform": "xianyu",
            "item_id": item_id,
            "expected_seller_id": "seller-a",
        },
        "expected_listing": {
            "title": "采购测试遥控器",
            "price_cny_minor": price_cny_minor,
            "currency": "CNY",
            "verified_at": datetime.now(UTC).isoformat(),
        },
        "objectives": ["availability", "function", "shipping_time"],
        "policy": {
            "max_auto_rounds": 3,
            "response_deadline_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    }


def seed_active_catalog_item(
    session_factory: sessionmaker[Session],
    item_id: str = "81001",
    price: Decimal = Decimal("12.50"),
) -> str:
    """
    在隔离数据库写入 Item 和一条 active 的最新发布 Catalog 快照。

    输入会话工厂、商品 ID 和 CNY 价格，返回服务端 Item URL；副作用仅限测试数据库。
    """

    keyword = f"采购测试-{item_id}"
    item_url = f"https://www.goofish.com/item?id={item_id}"
    with session_factory() as session:
        session.add(CatalogKeyword(category="采购测试", keyword=keyword, interval_minutes=60))
        session.commit()
        job = JobRepository(session).create(keyword)
        repository = CatalogSyncRepository(session)
        now = datetime.now(UTC)
        run = repository.begin_run(job.job_id, keyword, now)
        published = repository.publish_complete_run(
            run.run_id,
            keyword,
            [
                ParsedItem(
                    item_id=item_id,
                    title="采购测试遥控器",
                    price=price,
                    image_url=None,
                    item_url=item_url,
                    location="上海",
                )
            ],
            now,
            2,
        )
        session.commit()
        assert published.revision is not None
    return item_url


def test_procurement_api_requires_independent_configured_token(client: TestClient) -> None:
    """
    验证采购 API 对缺失或错误令牌返回 401，未配置独立令牌时返回 503。

    输入测试客户端；断言失败抛出 AssertionError；不访问数据库外部资源。
    """

    payload = procurement_payload()
    missing = client.post(
        "/api/v1/procurement-tasks",
        json=payload,
        headers={"Idempotency-Key": "procurement-auth-test-000001"},
    )
    wrong = client.post(
        "/api/v1/procurement-tasks",
        json=payload,
        headers={
            "Authorization": "Bearer wrong-token",
            "Idempotency-Key": "procurement-auth-test-000002",
        },
    )
    application = cast(FastAPI, client.app)
    application.state.procurement_api_token = None
    disabled = client.post(
        "/api/v1/procurement-tasks",
        json=payload,
        headers=PROCUREMENT_HEADERS,
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert disabled.status_code == 503


def test_legacy_v1_create_fails_closed_without_source_allowlist(client: TestClient) -> None:
    """
    验证旧版 v1 商品白名单未配置时返回 503，而不是扩大旧调用方权限。

    输入测试客户端；断言失败抛出 AssertionError；校验发生在数据库写入和聊天前。
    """

    application = cast(FastAPI, client.app)
    application.state.procurement_source_item_allowlist = frozenset()

    response = client.post(
        "/api/v1/procurement-tasks",
        json=procurement_payload(contract_version=1),
        headers={
            **PROCUREMENT_HEADERS,
            "Idempotency-Key": "procurement-empty-allowlist-v1",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "procurement_allowlist_not_configured"


def test_legacy_v1_create_rejects_source_item_outside_allowlist(client: TestClient) -> None:
    """
    验证旧版 v1 未获运维批准的闲鱼商品返回 403。

    输入测试客户端；断言失败抛出 AssertionError；不查询商品、不创建任务或聊天会话。
    """

    response = client.post(
        "/api/v1/procurement-tasks",
        json=procurement_payload(item_id="99999", contract_version=1),
        headers={
            **PROCUREMENT_HEADERS,
            "Idempotency-Key": "procurement-denied-item-v1",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "source_item_not_allowlisted"


def test_v2_create_uses_task_authorization_instead_of_static_allowlist(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证 v2 可由商城逐任务授权，不要求每个商品预先写入服务器环境变量。

    输入隔离客户端和数据库；断言失败抛出 AssertionError；只创建测试任务，不运行聊天。
    """

    seed_active_catalog_item(session_factory)
    application = cast(FastAPI, client.app)
    application.state.procurement_source_item_allowlist = frozenset()

    response = client.post(
        "/api/v1/procurement-tasks",
        json=procurement_payload(contract_version=2),
        headers={
            **PROCUREMENT_HEADERS,
            "Idempotency-Key": "procurement-v2-task-authorization",
        },
    )

    assert response.status_code == 202
    assert response.json()["contract_version"] == 2


def test_create_rejects_sensitive_text_hidden_in_listing_title(client: TestClient) -> None:
    """
    验证商品标题疑似夹带邮箱、电话或支付资料时统一返回安全错误。

    输入测试客户端；断言失败抛出 AssertionError；响应不回显命中的敏感正文。
    """

    unsafe_titles = (
        "采购测试遥控器 customer@example.com",
        "采购测试遥控器 电话 13800138000",
        "采购测试遥控器 卡号 4111 1111 1111 1111",
    )
    for index, title in enumerate(unsafe_titles, start=1):
        payload = procurement_payload()
        expected_listing = payload["expected_listing"]
        assert isinstance(expected_listing, dict)
        expected_listing["title"] = title
        response = client.post(
            "/api/v1/procurement-tasks",
            json=payload,
            headers={
                **PROCUREMENT_HEADERS,
                "Idempotency-Key": f"procurement-sensitive-title-{index:04d}",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == {"code": "unsafe_procurement_payload"}
        assert title not in response.text


def test_create_returns_202_and_persists_server_side_source_snapshot(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证 active 且同价商品返回 202，并持久化任务参数及 Item 表中的服务端 URL。

    输入客户端和会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    item_url = seed_active_catalog_item(session_factory)
    payload = procurement_payload()

    response = client.post("/api/v1/procurement-tasks", json=payload, headers=PROCUREMENT_HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["task_id"] == payload["task_id"]
    assert body["status"] == "pending_source_verification"
    assert body["next_action"] == "verify_source"
    with session_factory() as session:
        task = session.get(ProcurementExecutionTask, str(payload["task_id"]))
        conversation = session.scalar(
            select(ConversationSession).where(
                ConversationSession.task_id == str(payload["task_id"])
            )
        )
        assert task is not None
        assert conversation is not None
        assert task.expected_price_cny_minor == 1250
        assert task.objectives == ["availability", "function", "shipping_time"]
        assert task.max_auto_rounds == 3
        assert task.request_idempotency_key == PROCUREMENT_HEADERS["Idempotency-Key"]
        assert len(task.request_body_hash) == 64
        assert conversation.item_url == item_url
        assert conversation.account_key is None


def test_create_is_idempotent_and_conflicting_body_returns_409(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证同键同正文返回原任务，同键不同正文返回 409 且不会新增任务或会话。

    输入客户端和会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    seed_active_catalog_item(session_factory, item_id="81002")
    payload = procurement_payload(item_id="81002")
    headers = {
        **PROCUREMENT_HEADERS,
        "Idempotency-Key": "procurement-idempotent-test-0001",
    }

    first = client.post("/api/v1/procurement-tasks", json=payload, headers=headers)
    repeated = client.post("/api/v1/procurement-tasks", json=payload, headers=headers)
    conflicting_payload = {**payload, "expected_listing": dict(payload["expected_listing"])}
    expected_listing = conflicting_payload["expected_listing"]
    assert isinstance(expected_listing, dict)
    expected_listing["title"] = "同一幂等键的不同标题"
    conflicting = client.post(
        "/api/v1/procurement-tasks", json=conflicting_payload, headers=headers
    )

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert repeated.json() == first.json()
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"]["code"] == "idempotency_conflict"
    with session_factory() as session:
        task_count = session.scalar(select(func.count()).select_from(ProcurementExecutionTask))
        session_count = session.scalar(select(func.count()).select_from(ConversationSession))
    assert task_count == 1
    assert session_count == 1


def test_same_source_item_allows_only_one_active_task(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证同一闲鱼商品同时只能存在一个活动订单或 Canary 任务。

    输入测试客户端和隔离数据库；第二个任务返回 409，不并行创建聊天会话。
    """

    seed_active_catalog_item(session_factory, item_id="81006")
    first = client.post(
        "/api/v1/procurement-tasks",
        json=procurement_payload(item_id="81006"),
        headers={
            **PROCUREMENT_HEADERS,
            "Idempotency-Key": "procurement-source-lock-000001",
        },
    )
    second = client.post(
        "/api/v1/procurement-tasks",
        json=procurement_payload(item_id="81006"),
        headers={
            **PROCUREMENT_HEADERS,
            "Idempotency-Key": "procurement-source-lock-000002",
        },
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "source_item_has_active_procurement"


def test_create_rejects_missing_item_and_changed_price(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证 Item 不存在返回 404，最新发布价格不同返回 409 且均不创建任务。

    输入客户端和会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    missing = client.post(
        "/api/v1/procurement-tasks",
        json=procurement_payload(item_id="89999"),
        headers={**PROCUREMENT_HEADERS, "Idempotency-Key": "procurement-missing-test-0001"},
    )
    seed_active_catalog_item(session_factory, item_id="81003", price=Decimal("12.50"))
    changed = client.post(
        "/api/v1/procurement-tasks",
        json=procurement_payload(item_id="81003", price_cny_minor=1300),
        headers={**PROCUREMENT_HEADERS, "Idempotency-Key": "procurement-price-test-000001"},
    )

    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "source_item_not_found"
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "source_price_changed"
    with session_factory() as session:
        task_count = session.scalar(select(func.count()).select_from(ProcurementExecutionTask))
    assert task_count == 0


def test_create_rejects_latest_published_snapshot_that_is_not_active(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证最新发布快照不是 active 时返回 409，不以 Item 仍存在为由创建任务。

    输入客户端和会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    seed_active_catalog_item(session_factory, item_id="81005")
    with session_factory() as session:
        latest = session.scalar(
            select(CatalogChange)
            .where(CatalogChange.item_id == "81005")
            .order_by(CatalogChange.revision.desc())
            .limit(1)
        )
        assert latest is not None
        latest.availability = CatalogAvailability.OFF_SHELF
        session.commit()

    response = client.post(
        "/api/v1/procurement-tasks",
        json=procurement_payload(item_id="81005"),
        headers={**PROCUREMENT_HEADERS, "Idempotency-Key": "procurement-inactive-test-0001"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_not_active"
    with session_factory() as session:
        task_count = session.scalar(select(func.count()).select_from(ProcurementExecutionTask))
    assert task_count == 0


def test_create_rejects_extra_customer_pii_and_item_url(client: TestClient) -> None:
    """
    验证创建契约对额外客户地址和调用方商品 URL 返回 422。

    输入测试客户端；断言失败抛出 AssertionError；校验在数据库和外部访问前完成。
    """

    payload = procurement_payload()
    payload["customer_address"] = "東京都内の住所"
    pii_response = client.post(
        "/api/v1/procurement-tasks", json=payload, headers=PROCUREMENT_HEADERS
    )
    payload = procurement_payload()
    source = payload["source"]
    assert isinstance(source, dict)
    source["item_url"] = "https://www.goofish.com/item?id=81001"
    url_response = client.post(
        "/api/v1/procurement-tasks", json=payload, headers=PROCUREMENT_HEADERS
    )

    assert pii_response.status_code == 422
    assert url_response.status_code == 422


def test_get_and_cancel_update_only_local_execution_state(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证查询返回持久化参数，取消只关闭本地任务与会话且重复取消幂等。

    输入客户端和会话工厂；断言失败抛出 AssertionError；不触发页面、模型、购买或付款。
    """

    seed_active_catalog_item(session_factory, item_id="81004")
    payload = procurement_payload(item_id="81004")
    headers = {**PROCUREMENT_HEADERS, "Idempotency-Key": "procurement-cancel-test-00001"}
    created = client.post("/api/v1/procurement-tasks", json=payload, headers=headers)
    task_id = created.json()["task_id"]

    read = client.get(f"/api/v1/procurement-tasks/{task_id}", headers=PROCUREMENT_HEADERS)
    cancelled = client.post(
        f"/api/v1/procurement-tasks/{task_id}/cancel",
        json={"reason_code": "cancelled_by_shopping"},
        headers=PROCUREMENT_HEADERS,
    )
    repeated = client.post(
        f"/api/v1/procurement-tasks/{task_id}/cancel",
        json={"reason_code": "cancelled_by_shopping"},
        headers=PROCUREMENT_HEADERS,
    )

    assert read.status_code == 200
    assert read.json()["expected_price_cny_minor"] == 1250
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["session_status"] == "cancelled"
    assert cancelled.json()["next_action"] == "none"
    assert repeated.status_code == 200
    assert repeated.json() == cancelled.json()


def test_messages_endpoint_returns_full_plain_text_incrementally(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证内部消息接口按序返回卖家原文和 AI 草稿，并支持 after_seq 游标。

    输入客户端和隔离数据库；正文只在受令牌保护的内部接口返回。
    """

    seed_active_catalog_item(session_factory, item_id="81007")
    payload = procurement_payload(item_id="81007")
    created = client.post(
        "/api/v1/procurement-tasks",
        json=payload,
        headers={
            **PROCUREMENT_HEADERS,
            "Idempotency-Key": "procurement-message-page-0001",
        },
    )
    task_id = created.json()["task_id"]
    with session_factory() as session:
        conversation = session.scalar(
            select(ConversationSession).where(ConversationSession.task_id == task_id)
        )
        assert conversation is not None
        session.add_all(
            [
                ConversationMessage(
                    session_id=conversation.session_id,
                    seq=1,
                    direction=ConversationMessageDirection.INBOUND,
                    sender_role=ConversationSenderRole.SELLER,
                    external_message_id="seller-message-1",
                    content="还在，可以正常使用",
                    content_hash="a" * 64,
                    status=ConversationMessageStatus.RECEIVED,
                    idempotency_key="b" * 64,
                    risk_flags=[],
                    policy_reason_codes=[],
                ),
                ConversationMessage(
                    session_id=conversation.session_id,
                    seq=2,
                    direction=ConversationMessageDirection.OUTBOUND,
                    sender_role=ConversationSenderRole.BUYER,
                    content="请问近期可以发货吗？",
                    content_hash="c" * 64,
                    intent="shipping_check",
                    status=ConversationMessageStatus.DRAFTED,
                    idempotency_key="d" * 64,
                    risk_flags=[],
                    policy_reason_codes=[],
                ),
            ]
        )
        session.commit()

    response = client.get(
        f"/api/v1/procurement-tasks/{task_id}/messages?after_seq=0&limit=1",
        headers=PROCUREMENT_HEADERS,
    )
    second = client.get(
        f"/api/v1/procurement-tasks/{task_id}/messages?after_seq=1&limit=100",
        headers=PROCUREMENT_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["messages"][0]["content"] == "还在，可以正常使用"
    assert response.json()["has_more"] is True
    assert second.status_code == 200
    assert second.json()["messages"][0]["content"] == "请问近期可以发货吗？"
