"""
本文件负责在采购任务进入 x-comments 前检查可能混入商品标题的客户隐私或支付资料。

它属于 services 安全边界，由采购 API 调用；不修改契约模型、不访问数据库或网络，
也不尝试识别或保存客户身份。
"""

import re

from app.schemas.procurement import ProcurementTaskCreate

_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)),
    (
        "phone",
        re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:0\d{1,4}[\s-]\d{2,4}[\s-]\d{3,4}|1[3-9]\d{9})"),
    ),
    ("payment_card", re.compile(r"(?:\d[\s-]?){13,19}")),
    (
        "forbidden_customer_field",
        re.compile(
            r"(?:customer|address|phone|payment|card|cookie|客户|顾客|地址|电话|支付|"
            r"银行卡|密码|氏名|住所|電話|決済)",
            re.IGNORECASE,
        ),
    ),
)


class UnsafeProcurementPayloadError(ValueError):
    """
    表示采购商品文本疑似包含客户隐私或支付资料。

    异常只携带稳定原因码，不回显命中的敏感文本。
    """

    code = "unsafe_procurement_payload"


def assert_procurement_payload_safe(payload: ProcurementTaskCreate) -> None:
    """
    对进入 AI 与聊天链路的自由文本执行失败关闭隐私扫描。

    输入已通过严格 Schema 的采购任务；发现邮箱、电话、卡号或客户字段词时抛出
    UnsafeProcurementPayloadError；成功无返回值且无副作用。
    """

    text = payload.expected_listing.title
    for reason_code, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            raise UnsafeProcurementPayloadError(reason_code)
