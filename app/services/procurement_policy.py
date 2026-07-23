"""
本文件实现采购聊天草稿的确定性自动发送安全策略。

它属于 services 模块，只根据已校验模型输出和页面上下文快照给出允许或阻止结论；
不调用大模型、不访问数据库、不操作 Playwright，也不执行真实发送。
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from app.models.procurement import ConversationSessionStatus
from app.schemas.procurement_llm import (
    ProcurementDecision,
    ProcurementIntent,
    ProcurementLlmOutput,
    ProcurementRiskFlag,
)

MAX_HARD_AUTO_ROUNDS = 3
MAX_DRAFT_LENGTH = 180
MAX_DRAFT_NEWLINES = 2

ALLOWED_AUTO_SEND_INTENTS = frozenset(
    {
        ProcurementIntent.AVAILABILITY_CHECK,
        ProcurementIntent.FUNCTION_CHECK,
        ProcurementIntent.CONDITION_CHECK,
        ProcurementIntent.ACCESSORY_CHECK,
        ProcurementIntent.SHIPPING_CHECK,
    }
)

EXTERNAL_LINK_PATTERN = re.compile(r"(?:https?://|www\.|[\w-]+\.(?:com|cn|net|jp)(?:/|\b))", re.I)
PHONE_OR_EMAIL_PATTERN = re.compile(
    r"(?:\b1[3-9]\d{9}\b|\b0\d{1,4}-?\d{6,9}\b|\b\d{3}-\d{4}-\d{4}\b|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
    re.I,
)
OFF_PLATFORM_PATTERN = re.compile(r"(?:微信|微\s*信|vx|v信|QQ|Line|ライン|加我|私聊)", re.I)
PAYMENT_PATTERN = re.compile(r"(?:支付宝|银行卡|转账|汇款|二维码|付款链接|定金|私下交易)", re.I)
ADDRESS_PATTERN = re.compile(r"(?:收货地址|详细地址|邮编|邮政编码|住所|送付先|電話番号)", re.I)
PURCHASE_COMMITMENT_PATTERN = re.compile(
    r"(?:(?:我|我们).{0,5}(?:要了|买了|购买|拍下|下单|付款)|"
    r"确认购买|马上付款|现在付款|接受加价|确认收货|给我发货)",
    re.I,
)
NEGOTIATION_PATTERN = re.compile(r"(?:最低价|便宜点|优惠点|砍价|降价|包邮吗|改价)", re.I)
CREDENTIAL_PATTERN = re.compile(
    r"(?:验证码|短信码|登录码|安全验证|人机验证|captcha|verification\s*code)",
    re.I,
)


class AutoSendReason(StrEnum):
    """
    定义确定性策略拒绝自动发送的稳定原因码。

    原因码可进入脱敏审计；它不包含消息正文或敏感数据。
    """

    AUTO_SEND_DISABLED = "auto_send_disabled"
    TASK_AUTO_SEND_NOT_AUTHORIZED = "task_auto_send_not_authorized"
    INVALID_POLICY_LIMIT = "invalid_policy_limit"
    SESSION_NOT_ACTIVE = "session_not_active"
    ROUND_LIMIT_REACHED = "round_limit_reached"
    MODEL_DECISION_NOT_SEND = "model_decision_not_send"
    INTENT_NOT_ALLOWED = "intent_not_allowed"
    LOW_CONFIDENCE = "low_confidence"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    MODEL_RISK_FLAGGED = "model_risk_flagged"
    DRAFT_EMPTY = "draft_empty"
    DRAFT_TOO_LONG = "draft_too_long"
    DRAFT_TOO_MANY_LINES = "draft_too_many_lines"
    DRAFT_RISK_PATTERN = "draft_risk_pattern"
    STALE_REPLY_CONTEXT = "stale_reply_context"
    OUTBOUND_ALREADY_PENDING = "outbound_already_pending"
    ITEM_MISMATCH = "item_mismatch"
    SELLER_MISMATCH = "seller_mismatch"
    ACCOUNT_MISMATCH = "account_mismatch"
    PRICE_CHANGED = "price_changed"
    LISTING_UNAVAILABLE = "listing_unavailable"
    AUTH_OR_RISK_BLOCKED = "auth_or_risk_blocked"
    RESPONSE_DEADLINE_EXPIRED = "response_deadline_expired"
    WRITE_LOCK_REQUIRED = "write_lock_required"
    DOM_CHANGED = "dom_changed"
    COOLDOWN_NOT_ELAPSED = "cooldown_not_elapsed"
    OBJECTIVE_ALREADY_RESOLVED = "objective_already_resolved"


@dataclass(frozen=True, slots=True)
class AutoSendContext:
    """
    保存一次发送前由服务层和页面适配器确认的只读事实。

    所有安全条件默认关闭或不匹配，因此忘记传配置时结论必然为阻止；实例无副作用。
    """

    enabled: bool = False
    task_auto_send_authorized: bool = False
    session_status: ConversationSessionStatus = ConversationSessionStatus.PENDING_OPEN
    round_count: int = 0
    max_auto_rounds: int = MAX_HARD_AUTO_ROUNDS
    min_confidence: float = 0.85
    is_initial_outreach: bool = False
    latest_inbound_message_id: str | None = None
    reply_to_message_id: str | None = None
    has_pending_outbound: bool = False
    item_matches: bool = False
    seller_matches: bool = False
    account_matches: bool = False
    price_matches: bool = False
    listing_available: bool = False
    auth_and_risk_clear: bool = False
    within_response_deadline: bool = False
    write_lock_acquired: bool = False
    latest_dom_unchanged: bool = False
    cooldown_elapsed: bool = False
    objective_still_open: bool = False


@dataclass(frozen=True, slots=True)
class AutoSendPolicyDecision:
    """
    返回确定性自动发送策略的完整审计结果。

    `allowed` 只有在原因和检测风险都为空时为真；对象不执行发送或状态更新。
    """

    allowed: bool
    reason_codes: tuple[AutoSendReason, ...]
    detected_risk_flags: tuple[ProcurementRiskFlag, ...]
    policy_version: str = "auto_send_v1"


def scan_draft_risks(content: str) -> tuple[ProcurementRiskFlag, ...]:
    """
    使用固定正则扫描草稿中的链接、个人信息、站外交易和购买承诺。

    输入草稿正文，返回去重后的风险标志；正则异常会向上抛出，无外部副作用。
    """

    checks = (
        (EXTERNAL_LINK_PATTERN, ProcurementRiskFlag.EXTERNAL_LINK),
        (PHONE_OR_EMAIL_PATTERN, ProcurementRiskFlag.PII),
        (OFF_PLATFORM_PATTERN, ProcurementRiskFlag.OFF_PLATFORM),
        (PAYMENT_PATTERN, ProcurementRiskFlag.PAYMENT),
        (ADDRESS_PATTERN, ProcurementRiskFlag.ADDRESS_REQUEST),
        (PURCHASE_COMMITMENT_PATTERN, ProcurementRiskFlag.PURCHASE_COMMITMENT),
        (NEGOTIATION_PATTERN, ProcurementRiskFlag.PRICE_NEGOTIATION),
        (CREDENTIAL_PATTERN, ProcurementRiskFlag.CREDENTIAL_OR_CAPTCHA),
    )
    return tuple(flag for pattern, flag in checks if pattern.search(content))


def evaluate_auto_send(
    output: ProcurementLlmOutput, context: AutoSendContext | None = None
) -> AutoSendPolicyDecision:
    """
    对模型草稿和发送前上下文执行全部确定性安全检查。

    输入已通过 Pydantic 校验的模型输出与上下文，返回可审计结论；不修改状态或执行发送。
    """

    safe_context = context or AutoSendContext()
    reasons: list[AutoSendReason] = []
    draft = output.reply_draft or ""
    detected_risks = scan_draft_risks(draft)

    if not safe_context.enabled:
        reasons.append(AutoSendReason.AUTO_SEND_DISABLED)
    if not safe_context.task_auto_send_authorized:
        reasons.append(AutoSendReason.TASK_AUTO_SEND_NOT_AUTHORIZED)
    if not 1 <= safe_context.max_auto_rounds <= MAX_HARD_AUTO_ROUNDS:
        reasons.append(AutoSendReason.INVALID_POLICY_LIMIT)
    if safe_context.session_status is not ConversationSessionStatus.ACTIVE:
        reasons.append(AutoSendReason.SESSION_NOT_ACTIVE)
    if safe_context.round_count >= min(
        max(safe_context.max_auto_rounds, 0), MAX_HARD_AUTO_ROUNDS
    ):
        reasons.append(AutoSendReason.ROUND_LIMIT_REACHED)
    if output.decision is not ProcurementDecision.CONTINUE_CONVERSATION:
        reasons.append(AutoSendReason.MODEL_DECISION_NOT_SEND)
    if output.intent not in ALLOWED_AUTO_SEND_INTENTS:
        reasons.append(AutoSendReason.INTENT_NOT_ALLOWED)
    if output.confidence < safe_context.min_confidence:
        reasons.append(AutoSendReason.LOW_CONFIDENCE)
    if output.requires_human_review:
        reasons.append(AutoSendReason.HUMAN_REVIEW_REQUIRED)
    if output.risk_flags:
        reasons.append(AutoSendReason.MODEL_RISK_FLAGGED)
    if not draft.strip():
        reasons.append(AutoSendReason.DRAFT_EMPTY)
    if len(draft) > MAX_DRAFT_LENGTH:
        reasons.append(AutoSendReason.DRAFT_TOO_LONG)
    if draft.count("\n") > MAX_DRAFT_NEWLINES:
        reasons.append(AutoSendReason.DRAFT_TOO_MANY_LINES)
    if detected_risks:
        reasons.append(AutoSendReason.DRAFT_RISK_PATTERN)

    reply_context_matches = (
        safe_context.is_initial_outreach
        and safe_context.latest_inbound_message_id is None
        and safe_context.reply_to_message_id is None
    ) or (
        not safe_context.is_initial_outreach
        and safe_context.latest_inbound_message_id is not None
        and safe_context.reply_to_message_id == safe_context.latest_inbound_message_id
    )
    if not reply_context_matches:
        reasons.append(AutoSendReason.STALE_REPLY_CONTEXT)
    if safe_context.has_pending_outbound:
        reasons.append(AutoSendReason.OUTBOUND_ALREADY_PENDING)
    if not safe_context.item_matches:
        reasons.append(AutoSendReason.ITEM_MISMATCH)
    if not safe_context.seller_matches:
        reasons.append(AutoSendReason.SELLER_MISMATCH)
    if not safe_context.account_matches:
        reasons.append(AutoSendReason.ACCOUNT_MISMATCH)
    if not safe_context.price_matches:
        reasons.append(AutoSendReason.PRICE_CHANGED)
    if not safe_context.listing_available:
        reasons.append(AutoSendReason.LISTING_UNAVAILABLE)
    if not safe_context.auth_and_risk_clear:
        reasons.append(AutoSendReason.AUTH_OR_RISK_BLOCKED)
    if not safe_context.within_response_deadline:
        reasons.append(AutoSendReason.RESPONSE_DEADLINE_EXPIRED)
    if not safe_context.write_lock_acquired:
        reasons.append(AutoSendReason.WRITE_LOCK_REQUIRED)
    if not safe_context.latest_dom_unchanged:
        reasons.append(AutoSendReason.DOM_CHANGED)
    if not safe_context.cooldown_elapsed:
        reasons.append(AutoSendReason.COOLDOWN_NOT_ELAPSED)
    if not safe_context.objective_still_open:
        reasons.append(AutoSendReason.OBJECTIVE_ALREADY_RESOLVED)

    return AutoSendPolicyDecision(
        allowed=not reasons and not detected_risks,
        reason_codes=tuple(reasons),
        detected_risk_flags=detected_risks,
    )
