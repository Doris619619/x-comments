"""
本文件封装采购领域审计与事务 Outbox 的同事务追加操作。

它属于 repositories 模块，只接收已加载的 ORM 对象并向当前 SQLAlchemy 事务追加记录；
不提交事务、不投递 HTTP、不保存聊天正文，也不执行 Playwright、购买或付款动作。
"""

import hashlib
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.procurement import (
    ConversationSession,
    ProcurementAuditActorType,
    ProcurementAuditLog,
    ProcurementExecutionTask,
    ProcurementOutbox,
)
from app.schemas.procurement import ProcurementEvent, ProcurementEventType


def stable_event_key(task_id: str, event_seq: int, event_type: str) -> str:
    """
    为一个任务事件生成不含正文的稳定 SHA-256 幂等键。

    输入任务 ID、单调事件序号和事件类型；返回十六进制摘要；无数据库或网络副作用。
    """

    value = f"{task_id}:{event_seq}:{event_type}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def append_procurement_event(
    db: Session,
    task: ProcurementExecutionTask,
    conversation: ConversationSession,
    event_type: ProcurementEventType,
    occurred_at: datetime,
    *,
    message_id: str | None = None,
    reason_code: str | None = None,
    data: dict[str, Any] | None = None,
) -> ProcurementOutbox:
    """
    在当前领域事务中追加一条严格校验的商城回调事件。

    输入数据库会话、任务、会话、白名单事件及脱敏数据；返回未提交 Outbox 对象；
    契约或数据库错误向上抛出，调用方负责统一提交或回滚。
    """

    conversation.event_seq += 1
    event_id = str(uuid4())
    event = ProcurementEvent(
        contract_version=task.contract_version,
        event_id=event_id,
        event_seq=conversation.event_seq,
        event_type=event_type,
        occurred_at=occurred_at,
        task_id=task.task_id,
        session_id=conversation.session_id,
        message_id=message_id,
        task_status=task.status,
        reason_code=reason_code,
        data=data or {},
    )
    outbox = ProcurementOutbox(
        event_id=event_id,
        task_id=task.task_id,
        session_id=conversation.session_id,
        message_id=message_id,
        event_seq=conversation.event_seq,
        event_type=event_type.value,
        payload=event.model_dump(mode="json"),
        idempotency_key=stable_event_key(
            task.task_id,
            conversation.event_seq,
            event_type.value,
        ),
        next_attempt_at=occurred_at,
    )
    db.add(outbox)
    return outbox


def append_procurement_audit(
    db: Session,
    task: ProcurementExecutionTask,
    conversation: ConversationSession,
    *,
    actor_type: ProcurementAuditActorType,
    action: str,
    occurred_at: datetime,
    from_status: str | None = None,
    to_status: str | None = None,
    reason_code: str | None = None,
    message_id: str | None = None,
    metadata_redacted: dict[str, Any] | None = None,
    idempotency_suffix: str | None = None,
) -> ProcurementAuditLog:
    """
    在当前事务追加一条仅含粗粒度元数据的采购审计记录。

    输入状态变化和稳定动作码；返回未提交审计对象；不得把消息正文、Cookie、密钥或客户
    信息放入 ``metadata_redacted``，调用方负责统一提交或回滚。
    """

    suffix = idempotency_suffix or f"{conversation.version}:{conversation.event_seq}"
    idempotency_key = hashlib.sha256(f"{task.task_id}:{action}:{suffix}".encode()).hexdigest()
    audit = ProcurementAuditLog(
        task_id=task.task_id,
        session_id=conversation.session_id,
        message_id=message_id,
        actor_type=actor_type,
        actor_id=None,
        action=action,
        from_status=from_status,
        to_status=to_status,
        reason_code=reason_code,
        metadata_redacted=metadata_redacted or {},
        correlation_id=task.task_id,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
    )
    db.add(audit)
    return audit
