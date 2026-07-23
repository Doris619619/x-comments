"""
本文件定义闲鱼采购聊天会话、消息、审计和事务 Outbox 的 ORM 模型。

它属于 models 模块，为后续采购 API、LLM 草稿与安全发送 Worker 提供持久化边界；
不负责调用大模型、操作 Playwright、转换业务状态或投递 HTTP 回调。
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.crawl_job import utc_now


class ConversationSessionStatus(StrEnum):
    """
    定义采购聊天会话的有限状态。

    枚举仅描述持久化值；状态转换由后续服务层执行，无外部副作用。
    """

    PENDING_OPEN = "pending_open"
    ACTIVE = "active"
    WAITING_SELLER = "waiting_seller"
    COMPLETED = "completed"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConversationMessageDirection(StrEnum):
    """
    定义聊天消息相对本平台的方向。

    入站表示卖家消息，出站表示本平台草稿或已发送消息；枚举无副作用。
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ConversationSenderRole(StrEnum):
    """
    定义一条聊天记录的发送角色。

    角色用于审计和界面展示，不代表授权身份；枚举无副作用。
    """

    SELLER = "seller"
    BUYER = "buyer"
    SYSTEM = "system"


class ConversationMessageStatus(StrEnum):
    """
    定义入站消息和出站草稿共享的生命周期状态。

    状态只表示已确认事实；例如 `sent` 必须在页面发送结果得到确认后才能写入。
    """

    OBSERVED = "observed"
    RECEIVED = "received"
    ANALYZED = "analyzed"
    DRAFTED = "drafted"
    POLICY_CHECKING = "policy_checking"
    SEND_QUEUED = "send_queued"
    SENDING = "sending"
    SENT = "sent"
    POLICY_BLOCKED = "policy_blocked"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    SEND_FAILED = "send_failed"
    SUPERSEDED = "superseded"


class ProcurementPolicyResult(StrEnum):
    """
    定义确定性自动发送策略的持久化结论。

    未评估、允许和阻止互斥；最终发送仍需 Worker 重新校验页面上下文。
    """

    NOT_EVALUATED = "not_evaluated"
    ALLOWED = "allowed"
    BLOCKED = "blocked"


class ProcurementAuditActorType(StrEnum):
    """
    定义采购审计事件的发起者类别。

    该值用于追踪系统、模型、浏览器或人工动作，不保存账号凭据。
    """

    SYSTEM = "system"
    LLM = "llm"
    PLAYWRIGHT = "playwright"
    OPERATOR = "operator"


class ProcurementOutboxStatus(StrEnum):
    """
    定义采购回调事件在事务 Outbox 中的投递状态。

    状态不触发网络请求；后续独立投递器负责领取和更新记录。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"


class ProcurementExecutionTaskStatus(StrEnum):
    """
    定义 x-comments 本地采购执行任务可以持久化的状态。

    枚举不包含人工批准后的购买、付款或确认收货状态，防止执行服务越权。
    """

    PENDING_SOURCE_VERIFICATION = "pending_source_verification"
    CONTACTING_SELLER = "contacting_seller"
    AWAITING_SELLER_REPLY = "awaiting_seller_reply"
    AWAITING_PROCUREMENT_REVIEW = "awaiting_procurement_review"
    SOURCE_SOLD = "source_sold"
    PRICE_CHANGED = "price_changed"
    SELLER_UNRESPONSIVE = "seller_unresponsive"
    SELLER_RISK = "seller_risk"
    VERIFICATION_TIMEOUT = "verification_timeout"
    PROCUREMENT_FAILED = "procurement_failed"
    BLOCKED_BY_AUTH_OR_RISK_CONTROL = "blocked_by_auth_or_risk_control"
    CANARY_COMPLETED = "canary_completed"
    CANCELLED = "cancelled"


class ProcurementExecutionMode(StrEnum):
    """
    定义采购任务属于真实已付款订单还是 Root 白名单测试。

    模式只控制执行边界和后台展示，不代表购买、付款或收货授权。
    """

    PAID_ORDER = "paid_order"
    OPERATOR_CANARY = "operator_canary"


class ProcurementAuthorizationSource(StrEnum):
    """
    定义单任务自动发送授权的可信来源。

    已付款任务只能来自已验证支付事件，Canary 只能来自 Root 人工授权。
    """

    VERIFIED_PAYMENT_EVENT = "verified_payment_event"
    OPERATOR_CANARY = "operator_canary"


class ProcurementNextAction(StrEnum):
    """
    定义本地执行任务等待后续受控组件完成的下一步。

    该枚举只包含核验、聊天和人工审核，不包含购买、付款或收货动作。
    """

    VERIFY_SOURCE = "verify_source"
    OPEN_CONVERSATION = "open_conversation"
    WAIT_SELLER = "wait_seller"
    GENERATE_DRAFT = "generate_draft"
    HUMAN_REVIEW = "human_review"
    NONE = "none"


class ProcurementExecutionTask(Base):
    """
    保存商城提交到 x-comments 的本地采购执行任务与幂等请求快照。

    任务只保存公开商品和执行参数，不保存日本客户资料，也不能表达购买或付款授权。
    """

    __tablename__ = "procurement_execution_tasks"
    __table_args__ = (
        CheckConstraint(
            "expected_price_cny_minor >= 0",
            name="ck_procurement_execution_tasks_price_nonnegative",
        ),
        CheckConstraint(
            "max_auto_rounds BETWEEN 1 AND 3",
            name="ck_procurement_execution_tasks_max_rounds",
        ),
    )

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contract_version: Mapped[int] = mapped_column(Integer, default=1)
    execution_mode: Mapped[ProcurementExecutionMode] = mapped_column(
        Enum(
            ProcurementExecutionMode,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=ProcurementExecutionMode.PAID_ORDER,
        index=True,
    )
    auto_send_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorization_source: Mapped[ProcurementAuthorizationSource | None] = mapped_column(
        Enum(
            ProcurementAuthorizationSource,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        )
    )
    source_item_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), index=True)
    expected_title: Mapped[str] = mapped_column(Text)
    expected_price_cny_minor: Mapped[int] = mapped_column(Integer)
    objectives: Mapped[list[str]] = mapped_column(JSON)
    max_auto_rounds: Mapped[int] = mapped_column(Integer)
    response_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_body_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[ProcurementExecutionTaskStatus] = mapped_column(
        Enum(
            ProcurementExecutionTaskStatus,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=ProcurementExecutionTaskStatus.PENDING_SOURCE_VERIFICATION,
        index=True,
    )
    next_action: Mapped[ProcurementNextAction] = mapped_column(
        Enum(
            ProcurementNextAction,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=ProcurementNextAction.VERIFY_SOURCE,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    reason_detail_safe: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationSession(Base):
    """
    保存一个商城采购任务对应的闲鱼聊天会话。

    商城任务 UUID 是唯一跨服务关联；本模型不保存日本客户资料、Cookie 或账号密码。
    """

    __tablename__ = "conversation_sessions"

    session_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("procurement_execution_tasks.task_id"), unique=True, index=True
    )
    source_item_id: Mapped[str] = mapped_column(String(64), index=True)
    item_url: Mapped[str] = mapped_column(Text)
    expected_seller_id: Mapped[str | None] = mapped_column(String(128))
    observed_seller_id: Mapped[str | None] = mapped_column(String(128))
    account_key: Mapped[str | None] = mapped_column(String(64))
    conversation_key: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[ConversationSessionStatus] = mapped_column(
        Enum(
            ConversationSessionStatus,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=ConversationSessionStatus.PENDING_OPEN,
        index=True,
    )
    round_count: Mapped[int] = mapped_column(Integer, default=0)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    seller_poll_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_inbound_message_id: Mapped[str | None] = mapped_column(String(36))
    latest_outbound_message_id: Mapped[str | None] = mapped_column(String(36))
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail_safe: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ConversationMessage(Base):
    """
    保存卖家入站消息或一条从草稿演进到发送结果的出站消息。

    同一逻辑出站回复只占一行；消息正文只进入受控数据库，不应写入普通日志。
    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_conversation_messages_session_seq"),
        UniqueConstraint(
            "session_id",
            "external_message_id",
            name="uq_conversation_messages_external_id",
        ),
    )

    message_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_sessions.session_id"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    direction: Mapped[ConversationMessageDirection] = mapped_column(
        Enum(
            ConversationMessageDirection,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        )
    )
    sender_role: Mapped[ConversationSenderRole] = mapped_column(
        Enum(
            ConversationSenderRole,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        )
    )
    external_message_id: Mapped[str | None] = mapped_column(String(128))
    reply_to_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.message_id")
    )
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    intent: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[ConversationMessageStatus] = mapped_column(
        Enum(
            ConversationMessageStatus,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        index=True,
    )
    llm_model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    llm_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    risk_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_version: Mapped[str | None] = mapped_column(String(64))
    policy_result: Mapped[ProcurementPolicyResult] = mapped_column(
        Enum(
            ProcurementPolicyResult,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=ProcurementPolicyResult.NOT_EVALUATED,
    )
    policy_reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    send_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ProcurementAuditLog(Base):
    """
    追加保存采购会话的重要状态变化和安全决策。

    元数据必须在写入前脱敏；本模型不负责修改或删除既有审计记录。
    """

    __tablename__ = "procurement_audit_logs"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "idempotency_key",
            "action",
            name="uq_procurement_audit_idempotent_action",
        ),
    )

    audit_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("procurement_execution_tasks.task_id"), index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_sessions.session_id"), index=True
    )
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.message_id")
    )
    actor_type: Mapped[ProcurementAuditActorType] = mapped_column(
        Enum(
            ProcurementAuditActorType,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        )
    )
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    from_status: Mapped[str | None] = mapped_column(String(64))
    to_status: Mapped[str | None] = mapped_column(String(64))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    metadata_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(36))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProcurementOutbox(Base):
    """
    保存与领域变更同事务创建、等待回调商城的采购事件。

    后续投递器按任务事件序号串行处理；本模型本身不发起网络请求。
    """

    __tablename__ = "procurement_outbox"
    __table_args__ = (
        UniqueConstraint("task_id", "event_seq", name="uq_procurement_outbox_task_seq"),
    )

    outbox_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("procurement_execution_tasks.task_id"), index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_sessions.session_id")
    )
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.message_id")
    )
    event_seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[ProcurementOutboxStatus] = mapped_column(
        Enum(
            ProcurementOutboxStatus,
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=ProcurementOutboxStatus.PENDING,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_safe: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
