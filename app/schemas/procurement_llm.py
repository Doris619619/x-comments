"""
本文件定义采购对话大模型必须返回的严格结构化输出。

它属于 schemas 模块，只负责 JSON 解析和语义一致性校验；不构造提示词、不调用模型，
也不根据模型结论执行发送、购买或付款。
"""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas.procurement import ProcurementObjective, StrictProcurementModel


class ProcurementDecision(StrEnum):
    """
    定义模型对下一步非资金动作的建议。

    枚举刻意不包含购买或付款；任何建议仍须通过确定性策略或人工审核。
    """

    CONTINUE_CONVERSATION = "continue_conversation"
    READY_FOR_REVIEW = "ready_for_review"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    STOP = "stop"


class ProcurementIntent(StrEnum):
    """
    定义模型草稿可表达的白名单意图。

    该枚举不含议价、承诺购买、付款、地址交换或确认收货。
    """

    AVAILABILITY_CHECK = "availability_check"
    FUNCTION_CHECK = "function_check"
    CONDITION_CHECK = "condition_check"
    ACCESSORY_CHECK = "accessory_check"
    SHIPPING_CHECK = "shipping_check"
    CLARIFICATION = "clarification"
    COMPLETION = "completion"
    NO_REPLY = "no_reply"


class ProcurementRiskFlag(StrEnum):
    """
    定义模型可以报告、但不能自行解除的对话风险。

    任一风险标志都会阻止自动发送并要求人工检查。
    """

    PII = "pii"
    PAYMENT = "payment"
    OFF_PLATFORM = "off_platform"
    EXTERNAL_LINK = "external_link"
    PRICE_NEGOTIATION = "price_negotiation"
    PURCHASE_COMMITMENT = "purchase_commitment"
    ADDRESS_REQUEST = "address_request"
    CREDENTIAL_OR_CAPTCHA = "credential_or_captcha"
    PROMPT_INJECTION = "prompt_injection"
    ABUSE_OR_ILLEGAL = "abuse_or_illegal"
    CHAT_MISMATCH = "chat_mismatch"
    UNKNOWN = "unknown"


class ProcurementAvailability(StrEnum):
    """
    定义模型从卖家证据中提取的库存状态。

    未获得明确证据时必须使用 unknown，不能猜测商品可售。
    """

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class ProcurementFunctionalStatus(StrEnum):
    """
    定义模型从对话中提取的功能状态。

    该状态只概括卖家陈述，不等价于仓库验货结论。
    """

    WORKING = "working"
    NOT_WORKING = "not_working"
    UNKNOWN = "unknown"


class ProcurementAccessoriesStatus(StrEnum):
    """
    定义商品配件完整性的结构化结果。

    未明确说明配件时必须使用 unknown；枚举无副作用。
    """

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class ProcurementFacts(StrictProcurementModel):
    """
    表示从卖家消息中提取、等待人工确认的采购事实。

    所有未知信息显式保留 unknown/null；模型不会把事实转换为购买决定。
    """

    available: ProcurementAvailability
    functional_status: ProcurementFunctionalStatus
    condition_summary: str | None = Field(default=None, max_length=200)
    defects: list[str] = Field(default_factory=list, max_length=10)
    accessories_status: ProcurementAccessoriesStatus
    shipping_days: int | None = Field(default=None, ge=0, le=30)
    seller_price_cny_minor: int | None = Field(default=None, ge=0)

    @field_validator("defects")
    @classmethod
    def require_unique_defects(cls, value: list[str]) -> list[str]:
        """
        清理缺陷描述并拒绝空白或重复项目。

        输入缺陷列表并返回清理值；不合法时抛出 ValueError，无副作用。
        """

        cleaned = [" ".join(item.split()) for item in value]
        if any(not item for item in cleaned):
            raise ValueError("defects 不能包含空白项")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("defects 不能重复")
        if any(len(item) > 100 for item in cleaned):
            raise ValueError("单条 defects 不能超过 100 字符")
        return cleaned


class ProcurementLlmOutput(StrictProcurementModel):
    """
    表示采购模型一次调用必须返回的完整 JSON 对象。

    非法 JSON、未知字段或语义冲突会抛出 Pydantic ValidationError；通过校验仍不代表允许发送。
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"$id": "procurement-chat-v1"},
    )

    schema_version: Literal[1] = 1
    decision: ProcurementDecision
    intent: ProcurementIntent
    reply_draft: str | None = Field(default=None, max_length=180)
    facts: ProcurementFacts
    questions_answered: list[ProcurementObjective] = Field(default_factory=list, max_length=5)
    questions_remaining: list[ProcurementObjective] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0, le=1)
    risk_flags: list[ProcurementRiskFlag] = Field(default_factory=list, max_length=12)
    requires_human_review: bool
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    evidence_message_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_decision_consistency(self) -> "ProcurementLlmOutput":
        """
        校验回复建议、风险标志和待确认问题之间的语义一致性。

        输入已完成字段校验的模型并返回自身；冲突时抛出 ValueError，无副作用。
        """

        if (
            len(self.questions_answered) != len(set(self.questions_answered))
            or len(self.questions_remaining) != len(set(self.questions_remaining))
            or len(self.risk_flags) != len(set(self.risk_flags))
            or len(self.evidence_message_ids) != len(set(self.evidence_message_ids))
        ):
            raise ValueError("结构化列表不能包含重复值")
        answered = set(self.questions_answered)
        remaining = set(self.questions_remaining)
        if answered & remaining:
            raise ValueError("同一问题不能同时标记为已回答和待回答")
        if self.risk_flags and not self.requires_human_review:
            raise ValueError("存在风险标志时必须要求人工审核")
        if self.decision is ProcurementDecision.CONTINUE_CONVERSATION:
            if not self.reply_draft:
                raise ValueError("继续对话必须提供非空 reply_draft")
            if self.requires_human_review or self.risk_flags:
                raise ValueError("需要人工审核或存在风险时不能建议继续自动对话")
            if self.intent in {ProcurementIntent.COMPLETION, ProcurementIntent.NO_REPLY}:
                raise ValueError("继续对话必须使用可发送的询问意图")
        return self
