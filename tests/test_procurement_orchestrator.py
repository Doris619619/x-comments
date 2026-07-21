"""
本文件离线验证采购编排器的双开关、身份基线、单次发送与失败关闭行为。

测试只使用 SQLite、fake 核验器、fake 模型和 fake 聊天客户端；不访问闲鱼、DeepSeek、
商城回调或任何真实登录态。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.base import ProcurementDraftRequest
from app.crawler.chat_client import (
    ChatBinding,
    ChatMessageSnapshot,
    ChatSafetyError,
    PolicyAllowedDraft,
    SendEvidence,
)
from app.crawler.chat_runtime import OpenedXianyuChat
from app.models.item import Item
from app.models.procurement import (
    ConversationMessage,
    ConversationMessageDirection,
    ConversationMessageStatus,
    ConversationSenderRole,
    ConversationSession,
    ConversationSessionStatus,
    ProcurementExecutionTask,
    ProcurementExecutionTaskStatus,
    ProcurementNextAction,
    ProcurementOutbox,
    ProcurementOutboxStatus,
)
from app.repositories.procurement import ProcurementRepository
from app.repositories.procurement_runtime import (
    ProcurementRuntimeRepository,
    ProcurementSendNotAllowedError,
)
from app.schemas.procurement_llm import ProcurementLlmOutput
from app.services.item_verification import LiveVerificationResult, LiveVerificationStatus
from app.services.procurement_orchestrator import ProcurementConversationOrchestrator

ITEM_ID = "123456"
ITEM_URL = f"https://www.goofish.com/item?id={ITEM_ID}"
SELLER_ID = "seller-001"
ACCOUNT_ID = "account-001"
BASELINE = "a" * 64


class FakeVerifier:
    """返回调用方预设结果的离线单商品核验器。"""

    def __init__(self, result: LiveVerificationResult) -> None:
        """保存固定核验结果；不访问网络。"""

        self.result = result
        self.calls = 0

    async def verify(self, target: object) -> LiveVerificationResult:
        """记录调用并返回固定结果；输入目标仅用于接口兼容。"""

        del target
        self.calls += 1
        return self.result


class FakeDraftGenerator:
    """返回安全 availability 草稿并记录调用次数的离线模型替身。"""

    def __init__(self) -> None:
        """初始化调用计数；无外部副作用。"""

        self.calls = 0

    def generate(self, request: ProcurementDraftRequest) -> ProcurementLlmOutput:
        """根据请求目标返回严格结构化草稿，不访问 DeepSeek。"""

        self.calls += 1
        if request.summary_only:
            return ProcurementLlmOutput.model_validate(
                {
                    "schema_version": 1,
                    "decision": "ready_for_review",
                    "intent": "completion",
                    "reply_draft": None,
                    "facts": {
                        "available": "yes",
                        "functional_status": "working",
                        "condition_summary": None,
                        "defects": [],
                        "accessories_status": "unknown",
                        "shipping_days": 1,
                        "seller_price_cny_minor": None,
                    },
                    "questions_answered": [value.value for value in request.objectives],
                    "questions_remaining": [],
                    "confidence": 0.96,
                    "risk_flags": [],
                    "requires_human_review": True,
                    "reason_code": "round_limit_summary_ready",
                    "evidence_message_ids": [
                        str(message.message_id) for message in request.seller_messages
                    ],
                }
            )
        return ProcurementLlmOutput.model_validate(
            {
                "schema_version": 1,
                "decision": "continue_conversation",
                "intent": "availability_check",
                "reply_draft": "你好，请问这个商品目前还在吗？",
                "facts": {
                    "available": "unknown",
                    "functional_status": "unknown",
                    "condition_summary": None,
                    "defects": [],
                    "accessories_status": "unknown",
                    "shipping_days": None,
                    "seller_price_cny_minor": None,
                },
                "questions_answered": [],
                "questions_remaining": [value.value for value in request.objectives],
                "confidence": 0.96,
                "risk_flags": [],
                "requires_human_review": False,
                "reason_code": "need_availability",
                "evidence_message_ids": [],
            }
        )


class FakeChatClient:
    """模拟绑定会话读取和一次发送确认，不包含通用页面操作能力。"""

    def __init__(self, latest: ChatMessageSnapshot, *, fail_send: bool = False) -> None:
        """保存最新消息和可选发送失败开关；无网络副作用。"""

        self.latest = latest
        self.fail_send = fail_send
        self.send_calls = 0

    async def open_conversation(self) -> ChatMessageSnapshot:
        """返回固定最新消息；不点击真实页面。"""

        return self.latest

    async def read_latest_message(self) -> ChatMessageSnapshot:
        """返回相同快照，用于确定性 DOM 未变化校验。"""

        return self.latest

    async def send_policy_allowed_draft(
        self,
        draft: PolicyAllowedDraft,
        *,
        expected_latest_fingerprint: str,
        auto_send_enabled: bool,
    ) -> SendEvidence:
        """记录唯一发送尝试并返回假证据，或模拟点击后结果不确定。"""

        assert auto_send_enabled is True
        assert expected_latest_fingerprint == self.latest.fingerprint
        self.send_calls += 1
        if self.fail_send:
            raise ChatSafetyError("send_confirmation_missing", "离线模拟发送结果不确定")
        return SendEvidence(
            source_item_id=ITEM_ID,
            seller_id=SELLER_ID,
            account_id=ACCOUNT_ID,
            policy_decision_id=draft.policy_decision_id,
            draft_sha256="b" * 64,
            confirmed_message_fingerprint="c" * 64,
        )


class FakeChatFactory:
    """为订单绑定返回单个 fake 聊天上下文，可模拟登录或页面阻断。"""

    def __init__(
        self,
        client: FakeChatClient,
        *,
        error_code: str | None = None,
    ) -> None:
        """保存客户端与可选安全错误码；无外部副作用。"""

        self.client = client
        self.error_code = error_code
        self.open_calls = 0

    @asynccontextmanager
    async def open(
        self,
        *,
        item_url: str,
        source_item_id: str,
        expected_seller_id: str | None,
        expected_account_id: str,
    ) -> AsyncIterator[OpenedXianyuChat]:
        """校验订单绑定并产出 fake 客户端；配置错误时安全阻断。"""

        self.open_calls += 1
        assert item_url == ITEM_URL
        assert source_item_id == ITEM_ID
        assert expected_seller_id in {None, SELLER_ID}
        assert expected_account_id == ACCOUNT_ID
        if self.error_code is not None:
            raise ChatSafetyError(self.error_code, "离线模拟页面阻断")
        yield OpenedXianyuChat(
            binding=ChatBinding(ITEM_ID, SELLER_ID, ACCOUNT_ID),
            client=self.client,
        )


def empty_snapshot() -> ChatMessageSnapshot:
    """返回确定性空聊天快照；无外部副作用。"""

    return ChatMessageSnapshot(None, "none", "", None, BASELINE)


def seller_snapshot(text: str, fingerprint: str = "d" * 64) -> ChatMessageSnapshot:
    """返回指定文本的确定性卖家消息快照；无外部副作用。"""

    return ChatMessageSnapshot("seller-message", "seller", text, "1", fingerprint)


def seed_task(
    session_factory: sessionmaker[Session],
    *,
    task_status: ProcurementExecutionTaskStatus = (
        ProcurementExecutionTaskStatus.PENDING_SOURCE_VERIFICATION
    ),
    session_status: ConversationSessionStatus = ConversationSessionStatus.PENDING_OPEN,
    round_count: int = 0,
    expected_seller_id: str | None = None,
    baseline: str | None = None,
    with_sent_outbound: bool = False,
) -> str:
    """在 SQLite 中创建已获商城接受回调确认的订单绑定采购任务。"""

    now = datetime.now(UTC)
    task_id = str(uuid4())
    with session_factory() as db:
        db.add(
            Item(
                item_id=ITEM_ID,
                title="中国来源原始标题",
                price=Decimal("108.00"),
                item_url=ITEM_URL,
                source="xianyu",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        task = ProcurementExecutionTask(
            task_id=task_id,
            source_item_id=ITEM_ID,
            expected_title="日本語の商品タイトル",
            expected_price_cny_minor=10800,
            objectives=["availability", "function", "shipping_time"],
            max_auto_rounds=3,
            response_deadline_at=now + timedelta(hours=2),
            request_idempotency_key=f"request-{task_id}",
            request_body_hash="e" * 64,
            status=task_status,
            next_action=(
                ProcurementNextAction.WAIT_SELLER
                if task_status is ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY
                else ProcurementNextAction.VERIFY_SOURCE
            ),
        )
        conversation = ConversationSession(
            task_id=task_id,
            source_item_id=ITEM_ID,
            item_url=ITEM_URL,
            expected_seller_id=expected_seller_id,
            observed_seller_id=expected_seller_id,
            account_key=(
                ACCOUNT_ID
                if session_status is not ConversationSessionStatus.PENDING_OPEN
                else None
            ),
            conversation_key=baseline,
            status=session_status,
            round_count=round_count,
            event_seq=1,
            opened_at=now if session_status is not ConversationSessionStatus.PENDING_OPEN else None,
        )
        db.add_all((task, conversation))
        db.flush()
        if with_sent_outbound:
            outbound = ConversationMessage(
                session_id=conversation.session_id,
                seq=1,
                direction=ConversationMessageDirection.OUTBOUND,
                sender_role=ConversationSenderRole.BUYER,
                content="之前的安全询问",
                content_hash="f" * 64,
                status=ConversationMessageStatus.SENT,
                idempotency_key="1" * 64,
                send_attempt_count=1,
                sent_at=now,
            )
            db.add(outbound)
            db.flush()
            conversation.latest_outbound_message_id = outbound.message_id
        db.add(
            ProcurementOutbox(
                event_id=str(uuid4()),
                task_id=task_id,
                session_id=conversation.session_id,
                event_seq=1,
                event_type="task.accepted",
                payload={"task_id": task_id},
                idempotency_key="2" * 64,
                status=ProcurementOutboxStatus.DELIVERED,
                attempt_count=1,
                delivered_at=now,
            )
        )
        db.commit()
    return task_id


def mark_all_outbox_delivered(session_factory: sessionmaker[Session]) -> None:
    """把离线测试中的全部待投递事件标为 delivered，模拟商城幂等确认。"""

    with session_factory() as db:
        for event in db.scalars(select(ProcurementOutbox)):
            event.status = ProcurementOutboxStatus.DELIVERED
            event.delivered_at = datetime.now(UTC)
            event.next_attempt_at = None
        db.commit()


def make_orchestrator(
    session_factory: sessionmaker[Session],
    client: FakeChatClient,
    generator: FakeDraftGenerator,
    *,
    auto_send_enabled: bool,
    factory_error: str | None = None,
) -> ProcurementConversationOrchestrator:
    """装配完全离线的采购编排器；不访问真实服务。"""

    verifier = FakeVerifier(
        LiveVerificationResult(
            status=LiveVerificationStatus.AVAILABLE,
            current_price=Decimal("108.00"),
            reason_code="listing_available",
        )
    )
    return ProcurementConversationOrchestrator(
        ProcurementRuntimeRepository(session_factory),
        verifier,
        generator,
        FakeChatFactory(client, error_code=factory_error),
        chat_enabled=True,
        auto_send_enabled=auto_send_enabled,
        expected_account_id=ACCOUNT_ID,
        llm_model="fake-model",
        seller_poll_seconds=5,
    )


@pytest.mark.asyncio
async def test_success_waits_for_each_callback_then_sends_once(
    session_factory: sessionmaker[Session],
) -> None:
    """验证 opened 与 draft 事件送达后才发送，且成功发送只执行一次。"""

    task_id = seed_task(session_factory)
    client = FakeChatClient(empty_snapshot())
    generator = FakeDraftGenerator()
    orchestrator = make_orchestrator(
        session_factory,
        client,
        generator,
        auto_send_enabled=True,
    )

    assert await orchestrator.process_next("worker-1") is True
    assert generator.calls == 0
    assert client.send_calls == 0
    mark_all_outbox_delivered(session_factory)

    assert await orchestrator.process_next("worker-1") is True
    assert generator.calls == 1
    assert client.send_calls == 0
    mark_all_outbox_delivered(session_factory)

    assert await orchestrator.process_next("worker-1") is True
    assert client.send_calls == 1
    with session_factory() as db:
        task = db.get(ProcurementExecutionTask, task_id)
        conversation = db.scalar(
            select(ConversationSession).where(ConversationSession.task_id == task_id)
        )
        assert task is not None
        assert conversation is not None
        assert task.status is ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY
        assert conversation.round_count == 1
        assert conversation.expected_seller_id == SELLER_ID
        assert conversation.account_key == ACCOUNT_ID
        messages = list(db.scalars(select(ConversationMessage)))
        assert len(messages) == 1
        assert messages[0].status is ConversationMessageStatus.SENT
        assert messages[0].send_attempt_count == 1


@pytest.mark.asyncio
async def test_auto_send_disabled_persists_draft_without_sending(
    session_factory: sessionmaker[Session],
) -> None:
    """验证聊天可开启但自动发送关闭时，策略必定转人工且不点击。"""

    task_id = seed_task(session_factory)
    client = FakeChatClient(empty_snapshot())
    generator = FakeDraftGenerator()
    orchestrator = make_orchestrator(
        session_factory,
        client,
        generator,
        auto_send_enabled=False,
    )
    await orchestrator.process_next("worker-2")
    mark_all_outbox_delivered(session_factory)
    await orchestrator.process_next("worker-2")

    with session_factory() as db:
        task = db.get(ProcurementExecutionTask, task_id)
        message = db.scalar(select(ConversationMessage))
        assert task is not None
        assert message is not None
        assert task.status is ProcurementExecutionTaskStatus.AWAITING_PROCUREMENT_REVIEW
        assert message.status is ConversationMessageStatus.POLICY_BLOCKED
        assert "auto_send_disabled" in message.policy_reason_codes
        assert client.send_calls == 0


@pytest.mark.asyncio
async def test_send_crash_window_never_retries_same_message(
    session_factory: sessionmaker[Session],
) -> None:
    """验证点击后确认失败会永久转人工，后续轮询不会第二次发送。"""

    task_id = seed_task(session_factory)
    client = FakeChatClient(empty_snapshot(), fail_send=True)
    generator = FakeDraftGenerator()
    orchestrator = make_orchestrator(
        session_factory,
        client,
        generator,
        auto_send_enabled=True,
    )
    await orchestrator.process_next("worker-3")
    mark_all_outbox_delivered(session_factory)
    await orchestrator.process_next("worker-3")
    mark_all_outbox_delivered(session_factory)
    await orchestrator.process_next("worker-3")
    mark_all_outbox_delivered(session_factory)
    assert await orchestrator.process_next("worker-3") is False

    with session_factory() as db:
        task = db.get(ProcurementExecutionTask, task_id)
        message = db.scalar(select(ConversationMessage))
        assert task is not None
        assert message is not None
        assert task.status is ProcurementExecutionTaskStatus.AWAITING_PROCUREMENT_REVIEW
        assert message.status is ConversationMessageStatus.SEND_FAILED
        assert message.send_attempt_count == 1
        assert client.send_calls == 1


@pytest.mark.asyncio
async def test_historical_baseline_is_ignored_after_active_recovery(
    session_factory: sessionmaker[Session],
) -> None:
    """验证首次历史卖家消息及 opened 后崩溃重入都不会被当作本任务回复。"""

    historical = seller_snapshot("历史消息", BASELINE)
    task_id = seed_task(session_factory)
    client = FakeChatClient(historical)
    generator = FakeDraftGenerator()
    orchestrator = make_orchestrator(
        session_factory,
        client,
        generator,
        auto_send_enabled=False,
    )
    await orchestrator.process_next("worker-4")
    mark_all_outbox_delivered(session_factory)
    await orchestrator.process_next("worker-4")

    with session_factory() as db:
        inbound = list(
            db.scalars(
                select(ConversationMessage).where(
                    ConversationMessage.direction == ConversationMessageDirection.INBOUND
                )
            )
        )
        conversation = db.scalar(
            select(ConversationSession).where(ConversationSession.task_id == task_id)
        )
        assert inbound == []
        assert conversation is not None
        assert conversation.conversation_key == BASELINE
        assert generator.calls == 1


@pytest.mark.asyncio
async def test_sensitive_seller_reply_never_reaches_llm(
    session_factory: sessionmaker[Session],
) -> None:
    """验证电话、邮箱、地址或链接型卖家回复先转人工，不发送给模型。"""

    task_id = seed_task(
        session_factory,
        task_status=ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY,
        session_status=ConversationSessionStatus.WAITING_SELLER,
        round_count=1,
        expected_seller_id=SELLER_ID,
        baseline=BASELINE,
        with_sent_outbound=True,
    )
    client = FakeChatClient(seller_snapshot("电话 13800138000，请加微信"))
    generator = FakeDraftGenerator()
    orchestrator = make_orchestrator(
        session_factory,
        client,
        generator,
        auto_send_enabled=True,
    )
    await orchestrator.process_next("worker-5")
    mark_all_outbox_delivered(session_factory)
    await orchestrator.process_next("worker-5")

    with session_factory() as db:
        task = db.get(ProcurementExecutionTask, task_id)
        assert task is not None
        assert task.status is ProcurementExecutionTaskStatus.AWAITING_PROCUREMENT_REVIEW
        assert task.reason_code == "seller_message_risk"
        assert generator.calls == 0
        assert client.send_calls == 0


@pytest.mark.asyncio
async def test_third_round_final_reply_is_summarized_without_fourth_draft(
    session_factory: sessionmaker[Session],
) -> None:
    """验证第三次发送后的最终回复会入库并总结，但绝不会生成或发送第四条消息。"""

    task_id = seed_task(
        session_factory,
        task_status=ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY,
        session_status=ConversationSessionStatus.WAITING_SELLER,
        round_count=3,
        expected_seller_id=SELLER_ID,
        baseline=BASELINE,
        with_sent_outbound=True,
    )
    client = FakeChatClient(seller_snapshot("还在，可以正常使用，明天发货"))
    generator = FakeDraftGenerator()
    orchestrator = make_orchestrator(
        session_factory,
        client,
        generator,
        auto_send_enabled=True,
    )

    assert await orchestrator.process_next("worker-final-reply") is True
    assert generator.calls == 0
    mark_all_outbox_delivered(session_factory)
    assert await orchestrator.process_next("worker-final-reply") is True

    with session_factory() as db:
        task = db.get(ProcurementExecutionTask, task_id)
        messages = list(
            db.scalars(
                select(ConversationMessage).order_by(ConversationMessage.seq.asc())
            )
        )
        assert task is not None
        assert task.status is ProcurementExecutionTaskStatus.AWAITING_PROCUREMENT_REVIEW
        assert task.reason_code == "round_limit_reached"
        assert task.summary is not None
        assert generator.calls == 1
        assert client.send_calls == 0
        assert [message.direction for message in messages] == [
            ConversationMessageDirection.OUTBOUND,
            ConversationMessageDirection.INBOUND,
        ]


@pytest.mark.asyncio
async def test_login_or_dom_risk_returns_coarse_blocked_event(
    session_factory: sessionmaker[Session],
) -> None:
    """验证登录、验证码、403/429 或页面漂移统一阻断且不调用模型。"""

    task_id = seed_task(session_factory)
    client = FakeChatClient(empty_snapshot())
    generator = FakeDraftGenerator()
    orchestrator = make_orchestrator(
        session_factory,
        client,
        generator,
        auto_send_enabled=True,
        factory_error="http_risk_blocked",
    )
    await orchestrator.process_next("worker-6")

    with session_factory() as db:
        task = db.get(ProcurementExecutionTask, task_id)
        assert task is not None
        assert task.status is ProcurementExecutionTaskStatus.BLOCKED_BY_AUTH_OR_RISK_CONTROL
        assert task.reason_code == "blocked_by_auth_or_risk_control"
        assert generator.calls == 0
        assert client.send_calls == 0


def test_cancel_committed_before_send_guard_prevents_click(
    session_factory: sessionmaker[Session],
) -> None:
    """验证取消先提交会清除租约，使随后发送行锁拒绝页面点击。"""

    task_id = seed_task(
        session_factory,
        task_status=ProcurementExecutionTaskStatus.CONTACTING_SELLER,
        session_status=ConversationSessionStatus.ACTIVE,
        expected_seller_id=SELLER_ID,
        baseline=BASELINE,
    )
    runtime = ProcurementRuntimeRepository(session_factory)
    now = datetime.now(UTC)
    claimed = runtime.claim_next("worker-7", now, now + timedelta(seconds=90))
    assert claimed is not None
    with session_factory() as db:
        task = db.get(ProcurementExecutionTask, task_id)
        conversation = db.scalar(
            select(ConversationSession).where(ConversationSession.task_id == task_id)
        )
        assert task is not None
        assert conversation is not None
        ProcurementRepository(db).cancel(task, conversation, "cancelled_by_shopping", now)

    with pytest.raises(ProcurementSendNotAllowedError):
        with runtime.hold_send_transaction(task_id, "worker-7", str(uuid4())):
            raise AssertionError("取消后不应进入发送事务")
