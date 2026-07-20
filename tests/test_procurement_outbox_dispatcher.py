"""
本文件离线验证采购 Outbox 的任务内顺序、有限重试和永久失败边界。

测试使用 SQLite 与 fake HTTP 传输；不连接商城、不读取真实令牌，也不执行任何闲鱼聊天发送。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.item import Item
from app.models.procurement import (
    ConversationSession,
    ProcurementExecutionTask,
    ProcurementOutbox,
    ProcurementOutboxStatus,
)
from app.repositories.procurement_outbox import (
    ClaimedProcurementEvent,
    ProcurementOutboxRepository,
)
from app.services.procurement_outbox import (
    CallbackDeliveryError,
    ProcurementOutboxDispatcher,
)


class FakeCallbackTransport:
    """按预设结果投递事件并记录事件顺序的离线 HTTP 替身。"""

    def __init__(self, failures: list[CallbackDeliveryError | None]) -> None:
        """保存按调用顺序消费的失败列表；无外部副作用。"""

        self.failures = failures
        self.event_ids: list[str] = []

    async def send(
        self,
        *,
        callback_url: str,
        token: str,
        event: ClaimedProcurementEvent,
    ) -> None:
        """记录固定 URL、令牌和事件，并按预设抛出安全异常。"""

        assert callback_url == "http://c-shopping-web:3000/api/internal/v1/procurement-events"
        assert token == "t" * 32
        self.event_ids.append(event.event_id)
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure


def seed_ordered_events(session_factory: sessionmaker[Session]) -> tuple[str, str]:
    """创建同一任务 seq=1、seq=2 两条待投递 Outbox 事件。"""

    now = datetime.now(UTC)
    task_id = str(uuid4())
    first_id = str(uuid4())
    second_id = str(uuid4())
    with session_factory() as db:
        db.add(
            Item(
                item_id="998877",
                title="测试商品",
                price=1,
                item_url="https://www.goofish.com/item?id=998877",
                source="xianyu",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        task = ProcurementExecutionTask(
            task_id=task_id,
            source_item_id="998877",
            expected_title="测试商品",
            expected_price_cny_minor=100,
            objectives=["availability"],
            max_auto_rounds=1,
            response_deadline_at=now + timedelta(hours=1),
            request_idempotency_key=f"request-{task_id}",
            request_body_hash="a" * 64,
        )
        conversation = ConversationSession(
            task_id=task_id,
            source_item_id="998877",
            item_url="https://www.goofish.com/item?id=998877",
            event_seq=2,
        )
        db.add_all((task, conversation))
        db.flush()
        db.add_all(
            (
                ProcurementOutbox(
                    event_id=first_id,
                    task_id=task_id,
                    session_id=conversation.session_id,
                    event_seq=1,
                    event_type="task.accepted",
                    payload={"event_id": first_id, "event_seq": 1},
                    idempotency_key="b" * 64,
                    next_attempt_at=now,
                ),
                ProcurementOutbox(
                    event_id=second_id,
                    task_id=task_id,
                    session_id=conversation.session_id,
                    event_seq=2,
                    event_type="conversation.opened",
                    payload={"event_id": second_id, "event_seq": 2},
                    idempotency_key="c" * 64,
                    next_attempt_at=now,
                ),
            )
        )
        db.commit()
    return first_id, second_id


def make_dispatcher(
    session_factory: sessionmaker[Session],
    transport: FakeCallbackTransport,
) -> ProcurementOutboxDispatcher:
    """使用固定内部 URL 和测试令牌装配离线 Outbox 投递器。"""

    return ProcurementOutboxDispatcher(
        ProcurementOutboxRepository(session_factory),
        transport,
        callback_url="http://c-shopping-web:3000/api/internal/v1/procurement-events",
        token="t" * 32,
        max_attempts=3,
    )


@pytest.mark.asyncio
async def test_failed_first_event_blocks_later_sequence_until_delivered(
    session_factory: sessionmaker[Session],
) -> None:
    """验证 seq=1 退避时 seq=2 不能被提前投递或耗尽重试。"""

    first_id, second_id = seed_ordered_events(session_factory)
    transport = FakeCallbackTransport(
        [CallbackDeliveryError("callback_transport_error", retryable=True), None, None]
    )
    dispatcher = make_dispatcher(session_factory, transport)

    assert await dispatcher.dispatch_next("outbox-worker") is True
    assert transport.event_ids == [first_id]
    assert await dispatcher.dispatch_next("outbox-worker") is False
    assert transport.event_ids == [first_id]

    with session_factory() as db:
        first = db.scalar(select(ProcurementOutbox).where(ProcurementOutbox.event_id == first_id))
        assert first is not None
        first.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    assert await dispatcher.dispatch_next("outbox-worker") is True
    assert await dispatcher.dispatch_next("outbox-worker") is True
    assert transport.event_ids == [first_id, first_id, second_id]
    with session_factory() as db:
        states = list(
            db.scalars(select(ProcurementOutbox).order_by(ProcurementOutbox.event_seq))
        )
        assert [event.status for event in states] == [
            ProcurementOutboxStatus.DELIVERED,
            ProcurementOutboxStatus.DELIVERED,
        ]


@pytest.mark.asyncio
async def test_non_retryable_contract_error_stops_without_skipping_sequence(
    session_factory: sessionmaker[Session],
) -> None:
    """验证 401/403/422 类永久失败不重试，也不越过失败事件投递后续序号。"""

    first_id, _ = seed_ordered_events(session_factory)
    transport = FakeCallbackTransport(
        [CallbackDeliveryError("callback_contract_rejected", retryable=False)]
    )
    dispatcher = make_dispatcher(session_factory, transport)

    assert await dispatcher.dispatch_next("outbox-worker") is True
    assert await dispatcher.dispatch_next("outbox-worker") is False
    assert transport.event_ids == [first_id]
    with session_factory() as db:
        first = db.scalar(select(ProcurementOutbox).where(ProcurementOutbox.event_id == first_id))
        assert first is not None
        assert first.status is ProcurementOutboxStatus.FAILED
        assert first.next_attempt_at is None
        assert first.last_error_safe == "callback_contract_rejected"
