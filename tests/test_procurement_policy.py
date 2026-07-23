"""
本文件离线验证采购聊天确定性自动发送策略的允许和安全阻止分支。

测试不创建浏览器、不调用大模型、不访问数据库，也不会执行真实消息发送。
"""

from dataclasses import replace

import pytest

from app.models.procurement import ConversationSessionStatus
from app.schemas.procurement_llm import ProcurementLlmOutput, ProcurementRiskFlag
from app.services.procurement_policy import (
    AutoSendContext,
    AutoSendReason,
    evaluate_auto_send,
    scan_draft_risks,
)


def safe_output(content: str = "你好，请问这个商品目前还在吗？") -> ProcurementLlmOutput:
    """
    创建一份可供策略测试的已校验安全模型输出。

    输入可选草稿正文，返回 Pydantic 模型；字段不合法时抛出 ValidationError，无副作用。
    """

    return ProcurementLlmOutput.model_validate(
        {
            "schema_version": 1,
            "decision": "continue_conversation",
            "intent": "availability_check",
            "reply_draft": content,
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
            "questions_remaining": ["availability"],
            "confidence": 0.95,
            "risk_flags": [],
            "requires_human_review": False,
            "reason_code": "need_availability",
            "evidence_message_ids": [],
        }
    )


def safe_context() -> AutoSendContext:
    """
    创建一份所有页面前置条件均已满足的首轮发送上下文。

    无输入，返回不可变上下文；无异常和外部副作用。
    """

    return AutoSendContext(
        enabled=True,
        task_auto_send_authorized=True,
        session_status=ConversationSessionStatus.ACTIVE,
        round_count=0,
        max_auto_rounds=3,
        min_confidence=0.85,
        is_initial_outreach=True,
        latest_inbound_message_id=None,
        reply_to_message_id=None,
        has_pending_outbound=False,
        item_matches=True,
        seller_matches=True,
        account_matches=True,
        price_matches=True,
        listing_available=True,
        auth_and_risk_clear=True,
        within_response_deadline=True,
        write_lock_acquired=True,
        latest_dom_unchanged=True,
        cooldown_elapsed=True,
        objective_still_open=True,
    )


def test_auto_send_is_closed_by_default() -> None:
    """
    验证调用方遗漏配置或上下文时策略必然阻止发送。

    无输入；断言失败抛出 AssertionError；无外部副作用。
    """

    decision = evaluate_auto_send(safe_output())

    assert decision.allowed is False
    assert AutoSendReason.AUTO_SEND_DISABLED in decision.reason_codes
    assert AutoSendReason.TASK_AUTO_SEND_NOT_AUTHORIZED in decision.reason_codes


def test_safe_whitelisted_draft_is_allowed_when_all_guards_pass() -> None:
    """
    验证显式开启且全部确定性条件满足时返回唯一允许结论。

    无输入；断言失败抛出 AssertionError；不会执行真实发送。
    """

    decision = evaluate_auto_send(safe_output(), safe_context())

    assert decision.allowed is True
    assert decision.reason_codes == ()
    assert decision.detected_risk_flags == ()


@pytest.mark.parametrize(
    ("content", "expected_flag"),
    [
        ("详情看 https://example.com/a", ProcurementRiskFlag.EXTERNAL_LINK),
        ("电话是 090-1234-5678", ProcurementRiskFlag.PII),
        ("请加微信继续聊", ProcurementRiskFlag.OFF_PLATFORM),
        ("请用支付宝转账", ProcurementRiskFlag.PAYMENT),
        ("请把收货地址发给我", ProcurementRiskFlag.ADDRESS_REQUEST),
        ("好的，我现在拍下", ProcurementRiskFlag.PURCHASE_COMMITMENT),
        ("最低价还能便宜点吗", ProcurementRiskFlag.PRICE_NEGOTIATION),
    ],
)
def test_risky_draft_patterns_are_blocked(
    content: str, expected_flag: ProcurementRiskFlag
) -> None:
    """
    验证链接、PII、站外交易、付款、地址、购买承诺和议价文本均被阻止。

    输入参数化草稿与预期风险；断言失败抛出 AssertionError；无外部副作用。
    """

    decision = evaluate_auto_send(safe_output(content), safe_context())

    assert decision.allowed is False
    assert AutoSendReason.DRAFT_RISK_PATTERN in decision.reason_codes
    assert expected_flag in decision.detected_risk_flags
    assert expected_flag in scan_draft_risks(content)


@pytest.mark.parametrize(
    ("context", "expected_reason"),
    [
        (replace(safe_context(), round_count=3), AutoSendReason.ROUND_LIMIT_REACHED),
        (replace(safe_context(), item_matches=False), AutoSendReason.ITEM_MISMATCH),
        (replace(safe_context(), seller_matches=False), AutoSendReason.SELLER_MISMATCH),
        (replace(safe_context(), price_matches=False), AutoSendReason.PRICE_CHANGED),
        (
            replace(safe_context(), listing_available=False),
            AutoSendReason.LISTING_UNAVAILABLE,
        ),
        (
            replace(safe_context(), auth_and_risk_clear=False),
            AutoSendReason.AUTH_OR_RISK_BLOCKED,
        ),
        (
            replace(safe_context(), within_response_deadline=False),
            AutoSendReason.RESPONSE_DEADLINE_EXPIRED,
        ),
        (replace(safe_context(), latest_dom_unchanged=False), AutoSendReason.DOM_CHANGED),
        (
            replace(safe_context(), write_lock_acquired=False),
            AutoSendReason.WRITE_LOCK_REQUIRED,
        ),
        (
            replace(safe_context(), has_pending_outbound=True),
            AutoSendReason.OUTBOUND_ALREADY_PENDING,
        ),
        (
            replace(
                safe_context(),
                is_initial_outreach=False,
                latest_inbound_message_id="message-new",
                reply_to_message_id="message-old",
            ),
            AutoSendReason.STALE_REPLY_CONTEXT,
        ),
    ],
)
def test_any_stale_or_mismatched_page_context_blocks_send(
    context: AutoSendContext, expected_reason: AutoSendReason
) -> None:
    """
    验证轮次、商品、卖家、价格、DOM、写锁和消息上下文任一不符都会阻止发送。

    输入参数化上下文与原因码；断言失败抛出 AssertionError；无外部副作用。
    """

    decision = evaluate_auto_send(safe_output(), context)

    assert decision.allowed is False
    assert expected_reason in decision.reason_codes


def test_low_confidence_model_output_is_blocked() -> None:
    """
    验证低于配置阈值的模型草稿即使文本安全也不能自动发送。

    无输入；断言失败抛出 AssertionError；无外部副作用。
    """

    output = safe_output().model_copy(update={"confidence": 0.84})
    decision = evaluate_auto_send(output, safe_context())

    assert decision.allowed is False
    assert AutoSendReason.LOW_CONFIDENCE in decision.reason_codes
