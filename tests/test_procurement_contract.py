"""
本文件离线验证采购 API、LLM 输出契约和数据库幂等约束。

测试只使用 Pydantic 与隔离 SQLite，不访问大模型、闲鱼页面、登录态或外部网络。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models.procurement import (
    ConversationMessage,
    ConversationMessageDirection,
    ConversationMessageStatus,
    ConversationSenderRole,
    ConversationSession,
    ProcurementOutbox,
)
from app.schemas.procurement import ProcurementEvent, ProcurementTaskCreate
from app.schemas.procurement_llm import ProcurementLlmOutput


def valid_task_payload() -> dict[str, object]:
    """
    创建不含客户个人信息的最小采购任务请求。

    无输入，返回新字典；无异常和外部副作用。
    """

    return {
        "contract_version": 1,
        "task_id": str(uuid.uuid4()),
        "source": {
            "platform": "xianyu",
            "item_id": "123456",
            "expected_seller_id": "seller-a",
        },
        "expected_listing": {
            "title": "格力空调遥控器",
            "price_cny_minor": 10800,
            "currency": "CNY",
            "verified_at": datetime.now(UTC).isoformat(),
        },
        "objectives": ["availability", "function", "shipping_time"],
        "policy": {
            "max_auto_rounds": 3,
            "response_deadline_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    }


def valid_llm_payload() -> dict[str, object]:
    """
    创建一份可以进入确定性发送策略的安全模型输出。

    无输入，返回新字典；无异常和外部副作用。
    """

    return {
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
        "questions_remaining": ["availability", "function", "shipping_time"],
        "confidence": 0.95,
        "risk_flags": [],
        "requires_human_review": False,
        "reason_code": "need_availability",
        "evidence_message_ids": [],
    }


def make_session(task_id: str | None = None) -> ConversationSession:
    """
    创建一条尚未打开页面的采购会话 ORM 对象。

    输入可选任务 UUID，返回未持久化对象；无外部副作用。
    """

    return ConversationSession(
        task_id=task_id or str(uuid.uuid4()),
        source_item_id="123456",
        item_url="https://www.goofish.com/item?id=123456",
        expected_seller_id="seller-a",
        account_key="account-001",
    )


def test_task_contract_rejects_customer_pii_and_caller_supplied_item_url() -> None:
    """
    验证采购任务不能夹带客户地址，也不能由调用方指定任何商品 URL。

    无输入；断言失败抛出 AssertionError；无外部副作用。
    """

    payload = valid_task_payload()
    payload["customer_address"] = "不应跨服务传递的地址"
    with pytest.raises(ValidationError):
        ProcurementTaskCreate.model_validate(payload)

    payload = valid_task_payload()
    source = payload["source"]
    assert isinstance(source, dict)
    source["item_url"] = "https://www.goofish.com/item?id=123456"
    with pytest.raises(ValidationError):
        ProcurementTaskCreate.model_validate(payload)


def test_llm_contract_rejects_invalid_json_extra_fields_and_conflicting_decision() -> None:
    """
    验证非法 JSON、未知字段及风险与自动对话冲突都会安全失败。

    无输入；断言失败抛出 AssertionError；无外部副作用。
    """

    with pytest.raises(ValidationError):
        ProcurementLlmOutput.model_validate_json("不是 JSON")

    payload = valid_llm_payload()
    payload["confirm_purchase"] = True
    with pytest.raises(ValidationError):
        ProcurementLlmOutput.model_validate(payload)

    payload = valid_llm_payload()
    payload["risk_flags"] = ["payment"]
    with pytest.raises(ValidationError):
        ProcurementLlmOutput.model_validate(payload)

    payload = valid_llm_payload()
    payload["reply_draft"] = None
    with pytest.raises(ValidationError):
        ProcurementLlmOutput.model_validate(payload)

    payload = valid_llm_payload()
    payload["reply_draft"] = "问" * 181
    with pytest.raises(ValidationError):
        ProcurementLlmOutput.model_validate(payload)


def test_llm_json_schema_is_closed_and_contains_no_purchase_action() -> None:
    """
    验证生成的 JSON Schema 禁止额外字段，且决策枚举不包含购买动作。

    无输入；断言失败抛出 AssertionError；无外部副作用。
    """

    schema = ProcurementLlmOutput.model_json_schema()
    serialized = json.dumps(schema, ensure_ascii=False)

    assert schema["additionalProperties"] is False
    assert schema["$id"] == "procurement-chat-v1"
    assert "confirm_purchase" not in serialized
    assert "ready_for_review" in serialized


def test_message_event_requires_message_id_and_rejects_human_purchase_status() -> None:
    """
    验证消息事件必须可定位证据，且执行服务不能上报人工购买状态。

    无输入；断言失败抛出 AssertionError；无外部副作用。
    """

    base_event = {
        "contract_version": 1,
        "event_id": str(uuid.uuid4()),
        "event_seq": 1,
        "event_type": "assistant.message_sent",
        "occurred_at": datetime.now(UTC).isoformat(),
        "task_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "data": {},
    }
    with pytest.raises(ValidationError):
        ProcurementEvent.model_validate(base_event)

    base_event["message_id"] = str(uuid.uuid4())
    base_event["task_status"] = "procured"
    with pytest.raises(ValidationError):
        ProcurementEvent.model_validate(base_event)


def test_task_id_is_unique_for_conversation_session(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证重复创建相同商城任务只能得到一个数据库会话。

    输入隔离会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    task_id = str(uuid.uuid4())
    with session_factory() as session:
        session.add(make_session(task_id))
        session.commit()
        session.add(make_session(task_id))
        with pytest.raises(IntegrityError):
            session.commit()


def test_message_idempotency_key_and_external_message_id_are_unique(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证监听重放不能重复保存同一卖家消息或重复发送同一逻辑回复。

    输入隔离会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    with session_factory() as session:
        conversation = make_session()
        session.add(conversation)
        session.flush()
        first = ConversationMessage(
            session_id=conversation.session_id,
            seq=1,
            direction=ConversationMessageDirection.INBOUND,
            sender_role=ConversationSenderRole.SELLER,
            external_message_id="external-001",
            content="还在",
            content_hash="a" * 64,
            status=ConversationMessageStatus.RECEIVED,
            idempotency_key="b" * 64,
        )
        session.add(first)
        session.commit()

        session.add(
            ConversationMessage(
                session_id=conversation.session_id,
                seq=2,
                direction=ConversationMessageDirection.INBOUND,
                sender_role=ConversationSenderRole.SELLER,
                external_message_id="external-001",
                content="还在",
                content_hash="a" * 64,
                status=ConversationMessageStatus.RECEIVED,
                idempotency_key="c" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_outbox_event_sequence_is_unique_per_task(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证同一任务事件序号不能重复写入 Outbox，确保回调可有序去重。

    输入隔离会话工厂；断言失败抛出 AssertionError；副作用仅限测试数据库。
    """

    task_id = str(uuid.uuid4())
    with session_factory() as session:
        first = ProcurementOutbox(
            event_id=str(uuid.uuid4()),
            task_id=task_id,
            event_seq=1,
            event_type="task.accepted",
            payload={"task_id": task_id},
            idempotency_key="d" * 64,
        )
        session.add(first)
        session.commit()
        session.add(
            ProcurementOutbox(
                event_id=str(uuid.uuid4()),
                task_id=task_id,
                event_seq=1,
                event_type="conversation.opened",
                payload={"task_id": task_id},
                idempotency_key="e" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
