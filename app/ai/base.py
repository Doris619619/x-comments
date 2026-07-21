"""
本文件定义采购聊天草稿生成器的供应商无关契约与失败关闭校验。

它属于 ai 模块，负责描述不可信卖家消息、草稿请求、生成器协议和安全异常；
不读取环境变量、不发起网络请求、不访问数据库，也不执行页面发送、购买或付款。
"""

from typing import ClassVar, Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.procurement import ProcurementObjective, StrictProcurementModel
from app.schemas.procurement_llm import (
    ProcurementDecision,
    ProcurementIntent,
    ProcurementLlmOutput,
)
from app.services.procurement_policy import scan_draft_risks

ALLOWED_DRAFT_INTENTS = frozenset(
    {
        ProcurementIntent.AVAILABILITY_CHECK,
        ProcurementIntent.FUNCTION_CHECK,
        ProcurementIntent.CONDITION_CHECK,
        ProcurementIntent.ACCESSORY_CHECK,
        ProcurementIntent.SHIPPING_CHECK,
    }
)

INTENT_OBJECTIVES = {
    ProcurementIntent.AVAILABILITY_CHECK: ProcurementObjective.AVAILABILITY,
    ProcurementIntent.FUNCTION_CHECK: ProcurementObjective.FUNCTION,
    ProcurementIntent.CONDITION_CHECK: ProcurementObjective.CONDITION,
    ProcurementIntent.ACCESSORY_CHECK: ProcurementObjective.ACCESSORIES,
    ProcurementIntent.SHIPPING_CHECK: ProcurementObjective.SHIPPING_TIME,
}


class UntrustedSellerMessage(StrictProcurementModel):
    """
    表示一条只能作为证据、绝不能作为系统指令的卖家消息。

    正文会发送给模型用于分析，但本对象不记录日志、不保存数据库，也不改变消息内容。
    """

    message_id: UUID
    content: str = Field(min_length=1, max_length=2000)


class ProcurementDraftRequest(StrictProcurementModel):
    """
    表示一次采购信息问询草稿所需的完整、只读上下文。

    商品标题与卖家消息都来自外部并按不可信数据处理；模型只能处理调用方明确列出的五类目标。
    """

    product_title: str = Field(min_length=1, max_length=2000)
    objectives: list[ProcurementObjective] = Field(min_length=1, max_length=5)
    questions_answered: list[ProcurementObjective] = Field(max_length=5)
    questions_remaining: list[ProcurementObjective] = Field(max_length=5)
    seller_messages: list[UntrustedSellerMessage] = Field(default_factory=list, max_length=20)
    round_count: int = Field(ge=0, le=3)
    max_auto_rounds: int = Field(ge=1, le=3)
    summary_only: bool = False

    @model_validator(mode="after")
    def validate_conversation_state(self) -> Self:
        """
        校验目标分区、消息 ID 和剩余轮次，避免把不完整状态交给模型。

        输入已完成字段校验的请求并返回自身；状态冲突时抛出 ValueError，无外部副作用。
        """

        objectives = set(self.objectives)
        answered = set(self.questions_answered)
        remaining = set(self.questions_remaining)
        message_ids = [message.message_id for message in self.seller_messages]
        if len(self.objectives) != len(objectives):
            raise ValueError("objectives 不能重复")
        if len(self.questions_answered) != len(answered):
            raise ValueError("questions_answered 不能重复")
        if len(self.questions_remaining) != len(remaining):
            raise ValueError("questions_remaining 不能重复")
        if answered & remaining or answered | remaining != objectives:
            raise ValueError("已回答与待回答问题必须恰好组成 objectives")
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("seller_messages 的 message_id 不能重复")
        at_round_limit = self.round_count >= self.max_auto_rounds
        if self.summary_only != at_round_limit:
            raise ValueError("达到最大轮次时必须且只能请求只读总结")
        return self


class ProcurementAiError(RuntimeError):
    """
    表示可安全向上层报告、且不包含模型输入或响应正文的 AI 失败。

    子类只暴露稳定错误码与固定消息，调用方不得用原始异常替代该边界。
    """

    code: ClassVar[str] = "procurement_ai_error"
    safe_message: ClassVar[str] = "采购草稿生成失败"

    def __init__(self) -> None:
        """
        使用类级固定消息创建安全异常。

        无输入，异常文本不含密钥、提示词或卖家原文；除构造异常外无副作用。
        """

        super().__init__(self.safe_message)


class ProcurementAiTimeoutError(ProcurementAiError):
    """表示模型请求超时且未获得可验证草稿，固定失败关闭。"""

    code = "procurement_ai_timeout"
    safe_message = "采购草稿模型请求超时"


class ProcurementAiTransportError(ProcurementAiError):
    """表示模型网络传输失败且没有可信响应，固定失败关闭。"""

    code = "procurement_ai_transport_error"
    safe_message = "采购草稿模型网络请求失败"


class ProcurementAiHttpError(ProcurementAiError):
    """表示模型服务返回非成功 HTTP 状态，响应正文不会进入异常。"""

    code = "procurement_ai_http_error"
    safe_message = "采购草稿模型返回非成功状态"


class ProcurementAiOutputError(ProcurementAiError):
    """表示模型响应、JSON、Schema 或安全语义偏离，固定拒绝该草稿。"""

    code = "procurement_ai_output_invalid"
    safe_message = "采购草稿模型输出不符合安全契约"


class ProcurementDraftGenerator(Protocol):
    """
    定义可替换的同步采购草稿生成器接口。

    实现必须只返回已验证结构，不得在该接口内发送闲鱼消息或执行资金动作。
    """

    def generate(self, request: ProcurementDraftRequest) -> ProcurementLlmOutput:
        """
        根据只读对话上下文生成一份严格结构化草稿。

        输入草稿请求并返回已验证输出；供应商或安全失败时抛出 ProcurementAiError。
        """

        ...


def validate_procurement_draft_output(
    output: ProcurementLlmOutput,
    request: ProcurementDraftRequest,
) -> ProcurementLlmOutput:
    """
    对已通过 Schema 的模型输出执行请求范围和危险文本的二次失败关闭校验。

    输入结构化输出与原请求并返回同一输出；任何越权或矛盾抛出安全输出异常，无外部副作用。
    """

    objectives = set(request.objectives)
    output_answered = set(output.questions_answered)
    output_remaining = set(output.questions_remaining)
    evidence_ids = {message.message_id for message in request.seller_messages}

    if output_answered | output_remaining != objectives:
        raise ProcurementAiOutputError
    if not set(output.evidence_message_ids) <= evidence_ids:
        raise ProcurementAiOutputError
    if output.reply_draft and scan_draft_risks(output.reply_draft):
        raise ProcurementAiOutputError
    if request.summary_only and output.decision is ProcurementDecision.CONTINUE_CONVERSATION:
        raise ProcurementAiOutputError

    if output.decision is ProcurementDecision.CONTINUE_CONVERSATION:
        if output.intent not in ALLOWED_DRAFT_INTENTS:
            raise ProcurementAiOutputError
        target = INTENT_OBJECTIVES[output.intent]
        if target not in request.questions_remaining or target not in output_remaining:
            raise ProcurementAiOutputError
    elif output.reply_draft is not None:
        raise ProcurementAiOutputError

    return output
