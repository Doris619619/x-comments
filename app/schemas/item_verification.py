"""
本文件定义商城结算前单商品核验的请求与响应结构。

它属于 schemas 模块，只约束 HTTP 数据形状和状态枚举，不访问数据库、页面或外部网络。
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ItemVerificationStatus(StrEnum):
    """
    定义商城能够稳定处理的五种核验状态。

    枚举值直接用于 JSON 响应；构造非法值时抛出 ValueError，没有副作用。
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    PRICE_CHANGED = "price_changed"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ItemVerifyRequest(BaseModel):
    """
    表示商城发起的一次结算核验请求。

    输入人工确认时保存的人民币价格、币种和调用场景；字段非法时抛出校验异常，无副作用。
    """

    expected_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    currency: Literal["CNY"]
    context: Literal["checkout"]


class ItemVerifyResponse(BaseModel):
    """
    表示单次实时详情核验的标准响应。

    输入状态、当前人民币价格、时间和追踪字段；字段非法时抛出校验异常，无副作用。
    """

    status: ItemVerificationStatus
    current_price: Decimal | None
    verified_at: datetime
    reason_code: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=64)
