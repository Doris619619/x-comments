"""
本文件定义商城与 x-comments 之间的采购任务、会话消息和回调事件契约。

它属于 schemas 模块，只校验跨服务数据；不访问数据库、调用大模型、操作页面或执行发送。
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.procurement import (
    ConversationMessageDirection,
    ConversationMessageStatus,
    ConversationSenderRole,
    ConversationSessionStatus,
    ProcurementExecutionTaskStatus,
    ProcurementNextAction,
)

ProcurementExecutionStatus = ProcurementExecutionTaskStatus


class StrictProcurementModel(BaseModel):
    """
    为采购跨服务契约提供禁止未知字段的共同基类。

    子类继承后会折叠首尾空白并拒绝额外字段；模型校验无外部副作用。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProcurementObjective(StrEnum):
    """
    定义 AI 可以协助核实的采购问题白名单。

    枚举不包含议价、购买、付款、地址或确认收货动作。
    """

    AVAILABILITY = "availability"
    FUNCTION = "function"
    CONDITION = "condition"
    ACCESSORIES = "accessories"
    SHIPPING_TIME = "shipping_time"


class ProcurementEventType(StrEnum):
    """
    定义 x-comments 可回调商城的采购事件白名单。

    每个事件都通过 Outbox 串行投递；任意未列出的事件类型会被校验拒绝。
    """

    TASK_ACCEPTED = "task.accepted"
    CONVERSATION_OPENED = "conversation.opened"
    SELLER_MESSAGE_RECEIVED = "seller.message_received"
    ASSISTANT_DRAFT_CREATED = "assistant.draft_created"
    ASSISTANT_MESSAGE_SENT = "assistant.message_sent"
    ASSISTANT_MESSAGE_BLOCKED = "assistant.message_blocked"
    CONVERSATION_SUMMARY_READY = "conversation.summary_ready"
    CONVERSATION_BLOCKED = "conversation.blocked"
    CONVERSATION_FAILED = "conversation.failed"
    CONVERSATION_TIMED_OUT = "conversation.timed_out"


class ProcurementSource(StrictProcurementModel):
    """
    表示商城下单时固定的闲鱼公开来源快照。

    只接受稳定商品 ID 和可选卖家快照；真实 URL 必须由服务端从 Item 表读取。
    """

    platform: Literal["xianyu"] = "xianyu"
    item_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")
    expected_seller_id: str | None = Field(default=None, max_length=128)


class ProcurementExpectedListing(StrictProcurementModel):
    """
    表示商城创建采购任务时已确认的商品快照。

    价格按人民币分保存，避免浮点误差；模型不含商城售价或客户信息。
    """

    title: str = Field(min_length=1, max_length=2000)
    price_cny_minor: int = Field(ge=0)
    currency: Literal["CNY"] = "CNY"
    verified_at: datetime


class ProcurementAutomationPolicy(StrictProcurementModel):
    """
    表示单个采购任务允许的自动对话上限。

    最大自动发送轮次被硬限制为三轮；该模型不负责决定全局自动发送开关。
    """

    max_auto_rounds: int = Field(default=3, ge=1, le=3)
    response_deadline_at: datetime


class ProcurementTaskCreate(StrictProcurementModel):
    """
    表示商城向 x-comments 创建采购聊天任务的请求。

    任务只含公开来源和目标，不允许携带日本客户地址、电话或支付信息。
    """

    contract_version: Literal[1] = 1
    task_id: UUID
    source: ProcurementSource
    expected_listing: ProcurementExpectedListing
    objectives: list[ProcurementObjective] = Field(min_length=1, max_length=5)
    policy: ProcurementAutomationPolicy

    @field_validator("objectives")
    @classmethod
    def require_unique_objectives(
        cls, value: list[ProcurementObjective]
    ) -> list[ProcurementObjective]:
        """
        拒绝重复采购目标，确保同一问题只进入一次状态跟踪。

        输入目标列表并返回原顺序；存在重复时抛出 ValueError，无副作用。
        """

        if len(value) != len(set(value)):
            raise ValueError("objectives 不能重复")
        return value


class ProcurementTaskAccepted(StrictProcurementModel):
    """
    表示 x-comments 幂等接受采购任务后的最小响应。

    相同任务与幂等键必须返回相同 session_id；序列化无副作用。
    """

    contract_version: Literal[1] = 1
    task_id: UUID
    session_id: UUID
    status: ProcurementExecutionStatus
    next_action: ProcurementNextAction
    created_at: datetime


class ProcurementExecutionTaskRead(StrictProcurementModel):
    """
    表示商城服务器可查询的本地采购执行任务详情。

    响应不返回请求幂等键、body 哈希、内部租约或商品 URL；序列化无副作用。
    """

    task_id: UUID
    session_id: UUID
    source_item_id: str
    expected_title: str
    expected_price_cny_minor: int
    currency: Literal["CNY"] = "CNY"
    objectives: list[ProcurementObjective]
    max_auto_rounds: int
    response_deadline_at: datetime
    status: ProcurementExecutionStatus
    next_action: ProcurementNextAction
    session_status: ConversationSessionStatus
    summary: dict[str, Any] | None
    reason_code: str | None
    reason_detail_safe: str | None
    created_at: datetime
    updated_at: datetime
    cancelled_at: datetime | None
    completed_at: datetime | None


class ProcurementTaskCancel(StrictProcurementModel):
    """
    表示商城主动停止尚未完成采购执行任务的请求。

    只接受稳定原因码，不接受自由文本、客户资料或付款信息；模型无副作用。
    """

    reason_code: str = Field(
        default="cancelled_by_shopping",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9_]+$",
    )


class ConversationSessionRead(StrictProcurementModel):
    """
    表示供商城内部查询的采购聊天会话状态。

    响应不包含账号凭据、Cookie 或页面存储状态；序列化无副作用。
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    session_id: UUID
    task_id: UUID
    source_item_id: str
    status: ConversationSessionStatus
    round_count: int
    event_seq: int
    error_code: str | None
    error_detail_safe: str | None
    opened_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConversationMessageRead(StrictProcurementModel):
    """
    表示仅供两个服务内部读取的一条聊天记录。

    正文不得进入公开用户 API 或普通日志；序列化本身无副作用。
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    message_id: UUID
    session_id: UUID
    seq: int
    direction: ConversationMessageDirection
    sender_role: ConversationSenderRole
    reply_to_message_id: UUID | None
    content: str
    intent: str | None
    status: ConversationMessageStatus
    risk_flags: list[str]
    requires_human_review: bool
    policy_reason_codes: list[str]
    observed_at: datetime | None
    generated_at: datetime | None
    queued_at: datetime | None
    sent_at: datetime | None


class ProcurementMessagePage(StrictProcurementModel):
    """
    表示按会话序号增量读取的内部聊天消息页。

    `next_seq` 是调用方下次读取游标；模型不执行查询或保存游标。
    """

    messages: list[ConversationMessageRead]
    next_seq: int = Field(ge=0)
    has_more: bool


class ProcurementEvent(StrictProcurementModel):
    """
    表示 x-comments 通过 Outbox 回调商城的一条有序采购事件。

    data 只能放脱敏摘要、消息序号和策略码，不得放完整聊天、客户信息或登录态。
    """

    contract_version: Literal[1] = 1
    event_id: UUID
    event_seq: int = Field(ge=1)
    event_type: ProcurementEventType
    occurred_at: datetime
    task_id: UUID
    session_id: UUID | None = None
    message_id: UUID | None = None
    task_status: ProcurementExecutionStatus | None = None
    reason_code: str | None = Field(default=None, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_message_reference_for_message_events(self) -> "ProcurementEvent":
        """
        要求消息类事件携带 message_id，避免商城无法定位证据。

        输入已校验事件并返回自身；缺少消息 ID 时抛出 ValueError，无副作用。
        """

        message_events = {
            ProcurementEventType.SELLER_MESSAGE_RECEIVED,
            ProcurementEventType.ASSISTANT_DRAFT_CREATED,
            ProcurementEventType.ASSISTANT_MESSAGE_SENT,
            ProcurementEventType.ASSISTANT_MESSAGE_BLOCKED,
        }
        if self.event_type in message_events and self.message_id is None:
            raise ValueError("消息类采购事件必须包含 message_id")
        return self
