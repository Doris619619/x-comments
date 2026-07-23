"""
本文件保留人工诊断 API 使用的单商品实时核验。

它属于 services 模块，连接只读商品仓储与可注入核验器，不执行 Playwright 或构造 HTTP 异常。
商城结算和采购聊天编排不再调用本服务。
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from app.schemas.item_verification import (
    ItemVerificationStatus,
    ItemVerifyRequest,
    ItemVerifyResponse,
)

logger = logging.getLogger(__name__)


class LiveVerificationStatus(StrEnum):
    """
    定义详情访问层能够直接确认的状态。

    价格变化由服务层根据当前价格和商城快照比较得出；构造非法值时抛出 ValueError。
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VerificationTarget:
    """
    表示详情访问所需的最小商品标识。

    输入数据库中的闲鱼商品 ID；返回不可变目标对象；无异常和副作用。
    """

    item_id: str


@dataclass(frozen=True)
class LiveVerificationResult:
    """
    表示一次详情访问得到的安全分类结果。

    输入状态、可选当前价格和稳定原因码；返回不可变结果；无外部副作用。
    """

    status: LiveVerificationStatus
    current_price: Decimal | None
    reason_code: str


class ItemLookup(Protocol):
    """
    定义核验服务所需的只读商品查询能力。

    实现接收商品 ID 并返回是否存在；数据库异常可向上抛出，协议本身无副作用。
    """

    def exists(self, item_id: str) -> bool:
        """按商品 ID 返回存在性；数据库异常向上抛出，无写入副作用。"""


class LiveItemVerifier(Protocol):
    """
    定义可替换的单次实时详情核验能力。

    实现接收目标并异步返回分类结果；允许抛出异常，服务层会安全降级为 unknown。
    """

    async def verify(self, target: VerificationTarget) -> LiveVerificationResult:
        """只访问一次目标详情并返回结果；不得自动重试。"""


class ItemVerificationService:
    """
    编排商品存在性检查、实时访问和价格快照比较。

    输入只读仓储与可注入核验器；每次调用最多执行一次核验器；不写数据库。
    """

    def __init__(self, repository: ItemLookup, verifier: LiveItemVerifier) -> None:
        """
        注入只读仓储和实时核验器。

        输入符合协议的依赖；无返回和异常；副作用仅为保存引用。
        """

        self.repository = repository
        self.verifier = verifier

    async def verify(self, item_id: str, payload: ItemVerifyRequest) -> ItemVerifyResponse | None:
        """
        对一个已入库商品执行一次失败关闭的结算核验。

        输入商品 ID 与商城价格快照；商品不存在返回 None；核验异常转为 unknown；不写数据库。
        """

        if not self.repository.exists(item_id):
            return None

        request_id = str(uuid4())
        try:
            live_result = await self.verifier.verify(VerificationTarget(item_id=item_id))
        except Exception as exc:
            logger.warning(
                "商品实时核验异常 item_id=%s request_id=%s error_type=%s",
                item_id,
                request_id,
                type(exc).__name__,
            )
            live_result = LiveVerificationResult(
                status=LiveVerificationStatus.UNKNOWN,
                current_price=None,
                reason_code="verification_internal_error",
            )

        status = ItemVerificationStatus(live_result.status.value)
        reason_code = live_result.reason_code
        current_price = live_result.current_price
        if live_result.status is LiveVerificationStatus.AVAILABLE:
            if current_price is None:
                status = ItemVerificationStatus.UNKNOWN
                reason_code = "listing_price_not_confirmed"
            elif current_price != payload.expected_price:
                status = ItemVerificationStatus.PRICE_CHANGED
                reason_code = "listing_price_changed"

        response = ItemVerifyResponse(
            status=status,
            current_price=current_price,
            verified_at=datetime.now(UTC),
            reason_code=reason_code,
            request_id=request_id,
        )
        logger.info(
            "商品实时核验完成 item_id=%s request_id=%s status=%s reason_code=%s",
            item_id,
            request_id,
            response.status.value,
            response.reason_code,
        )
        return response
