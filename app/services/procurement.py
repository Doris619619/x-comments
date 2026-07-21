"""
本文件实现本地采购执行任务的幂等创建、目录校验、查询和取消规则。

它属于 services 模块，通过仓储协议访问数据；不构造 HTTP 响应、不调用大模型、
不操作 Playwright，也不执行购买、付款或真实消息发送。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from app.models.catalog_sync import CatalogAvailability
from app.models.procurement import (
    ConversationSession,
    ProcurementExecutionTask,
    ProcurementExecutionTaskStatus,
)
from app.repositories.procurement import (
    NewProcurementExecutionTask,
    ProcurementSourceSnapshot,
    ProcurementWriteConflictError,
)
from app.schemas.procurement import ProcurementTaskCreate


class ProcurementServiceError(RuntimeError):
    """
    为 API 可稳定映射的采购业务错误提供共同基类。

    子类通过固定 code 区分错误；异常不包含请求正文、幂等键或客户资料。
    """

    code = "procurement_error"


class ProcurementIdempotencyConflictError(ProcurementServiceError):
    """
    表示相同幂等键被用于不同规范化请求正文。

    API 应映射为 409；异常无额外副作用。
    """

    code = "idempotency_conflict"


class ProcurementTaskConflictError(ProcurementServiceError):
    """
    表示相同 task_id 已由另一个幂等请求占用。

    API 应映射为 409；异常无额外副作用。
    """

    code = "task_id_conflict"


class ProcurementSourceItemNotFoundError(ProcurementServiceError):
    """
    表示本地 Item 表不存在请求商品。

    API 应映射为 404；此错误发生时不会创建任务或访问网页。
    """

    code = "source_item_not_found"


class ProcurementSourceUnavailableError(ProcurementServiceError):
    """
    表示商品没有 active 的最新发布 Catalog 快照。

    API 应映射为 409；网络失败和无快照不能降级为可采购。
    """

    code = "source_not_active"


class ProcurementSourcePriceChangedError(ProcurementServiceError):
    """
    表示商城人民币价格快照与最新发布 Catalog 价格不一致。

    API 应映射为 409；执行服务不会接受新价格或创建任务。
    """

    code = "source_price_changed"


class ProcurementTaskNotFoundError(ProcurementServiceError):
    """
    表示查询或取消的本地采购执行任务不存在。

    API 应映射为 404；异常无写入副作用。
    """

    code = "procurement_task_not_found"


class ProcurementInvalidStateError(ProcurementServiceError):
    """
    表示终态任务不允许再执行取消转换。

    API 应映射为 409；原状态保持不变。
    """

    code = "invalid_procurement_state"


class ProcurementDataIntegrityError(ProcurementServiceError):
    """
    表示任务与唯一会话之间的本地数据不完整。

    API 应安全返回服务错误；异常不尝试自动修复或触发外部操作。
    """

    code = "procurement_data_integrity_error"


class ProcurementStore(Protocol):
    """
    定义采购服务所需的最小持久化能力。

    实现可使用 SQLAlchemy 或测试替身；协议本身不执行数据库操作。
    """

    def get_source_snapshot(self, item_id: str) -> ProcurementSourceSnapshot | None:
        """按商品 ID 返回服务端来源快照或 None；无写入副作用。"""

    def get_by_task_id(self, task_id: str) -> ProcurementExecutionTask | None:
        """按任务 UUID 返回执行任务或 None；无写入副作用。"""

    def get_by_idempotency_key(self, key: str) -> ProcurementExecutionTask | None:
        """按幂等键返回执行任务或 None；无写入副作用。"""

    def get_session_by_task_id(self, task_id: str) -> ConversationSession | None:
        """返回任务唯一会话或 None；无写入副作用。"""

    def create_with_session(
        self, command: NewProcurementExecutionTask
    ) -> tuple[ProcurementExecutionTask, ConversationSession]:
        """原子创建执行任务与会话；冲突时抛出明确异常。"""

    def cancel(
        self,
        task: ProcurementExecutionTask,
        conversation: ConversationSession,
        reason_code: str,
        cancelled_at: datetime,
    ) -> None:
        """原子取消任务与会话；数据库错误向上抛出。"""


@dataclass(frozen=True, slots=True)
class ProcurementTaskResult:
    """
    组合一个本地执行任务与其唯一聊天会话。

    服务方法以该对象返回读取结果；对象不可变且无副作用。
    """

    task: ProcurementExecutionTask
    conversation: ConversationSession


def hash_procurement_request(payload: ProcurementTaskCreate) -> str:
    """
    对 Pydantic 规范化后的完整创建请求计算稳定 SHA-256。

    输入已校验请求并返回十六进制摘要；序列化错误向上抛出，无外部副作用。
    """

    canonical = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProcurementExecutionService:
    """
    编排采购任务的幂等语义、发布快照校验和有限状态转换。

    输入符合协议的仓储；创建和取消会写数据库，但不会访问闲鱼或任何模型服务。
    """

    def __init__(self, repository: ProcurementStore) -> None:
        """
        注入可替换采购仓储。

        输入仓储实现；无返回和异常；副作用仅为保存引用。
        """

        self.repository = repository

    def create(
        self, payload: ProcurementTaskCreate, idempotency_key: str
    ) -> ProcurementTaskResult:
        """
        幂等创建已通过 active/CNY/价格检查的任务和聊天会话。

        输入严格请求和幂等键；返回原任务或新任务；冲突、缺失、非 active 或改价时抛出业务异常。
        """

        body_hash = hash_procurement_request(payload)
        existing = self.repository.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return self._resolve_idempotent_replay(existing, body_hash)

        task_id = str(payload.task_id)
        if self.repository.get_by_task_id(task_id) is not None:
            raise ProcurementTaskConflictError("task_id 已存在")

        snapshot = self.repository.get_source_snapshot(payload.source.item_id)
        if snapshot is None:
            raise ProcurementSourceItemNotFoundError("来源商品不存在")
        self._validate_source(snapshot, payload.expected_listing.price_cny_minor)

        command = NewProcurementExecutionTask(
            task_id=task_id,
            source_item_id=payload.source.item_id,
            expected_title=payload.expected_listing.title,
            expected_price_cny_minor=payload.expected_listing.price_cny_minor,
            objectives=[objective.value for objective in payload.objectives],
            max_auto_rounds=payload.policy.max_auto_rounds,
            response_deadline_at=payload.policy.response_deadline_at,
            request_idempotency_key=idempotency_key,
            request_body_hash=body_hash,
            expected_seller_id=payload.source.expected_seller_id,
            item_url=snapshot.item_url,
        )
        try:
            task, conversation = self.repository.create_with_session(command)
        except ProcurementWriteConflictError:
            concurrent = self.repository.get_by_idempotency_key(idempotency_key)
            if concurrent is not None:
                return self._resolve_idempotent_replay(concurrent, body_hash)
            raise ProcurementTaskConflictError("task_id 已存在") from None
        return ProcurementTaskResult(task=task, conversation=conversation)

    def get(self, task_id: str) -> ProcurementTaskResult:
        """
        查询一个本地执行任务及其唯一聊天会话。

        输入任务 UUID；返回组合结果；任务不存在或数据不完整时抛出明确业务异常，无写入副作用。
        """

        task = self.repository.get_by_task_id(task_id)
        if task is None:
            raise ProcurementTaskNotFoundError("采购任务不存在")
        conversation = self.repository.get_session_by_task_id(task_id)
        if conversation is None:
            raise ProcurementDataIntegrityError("采购任务缺少聊天会话")
        return ProcurementTaskResult(task=task, conversation=conversation)

    def cancel(self, task_id: str, reason_code: str) -> ProcurementTaskResult:
        """
        幂等取消一个仍可停止的本地采购执行任务及其会话。

        输入任务 UUID 和稳定原因码；返回取消后结果；其他终态抛出冲突，成功时写数据库。
        """

        result = self.get(task_id)
        if result.task.status is ProcurementExecutionTaskStatus.CANCELLED:
            return result
        terminal_states = {
            ProcurementExecutionTaskStatus.SOURCE_SOLD,
            ProcurementExecutionTaskStatus.PRICE_CHANGED,
            ProcurementExecutionTaskStatus.SELLER_UNRESPONSIVE,
            ProcurementExecutionTaskStatus.SELLER_RISK,
            ProcurementExecutionTaskStatus.VERIFICATION_TIMEOUT,
            ProcurementExecutionTaskStatus.PROCUREMENT_FAILED,
        }
        if result.task.status in terminal_states:
            raise ProcurementInvalidStateError("当前采购任务状态不允许取消")
        try:
            self.repository.cancel(
                result.task,
                result.conversation,
                reason_code,
                datetime.now(UTC),
            )
        except ProcurementWriteConflictError:
            raise ProcurementInvalidStateError("当前采购任务状态不允许取消") from None
        return result

    def _resolve_idempotent_replay(
        self, existing: ProcurementExecutionTask, body_hash: str
    ) -> ProcurementTaskResult:
        """
        比较既有任务 body 哈希并返回相同请求结果。

        输入任务和当前摘要；不同正文抛出幂等冲突，缺少会话抛出完整性错误；无写入副作用。
        """

        if existing.request_body_hash != body_hash:
            raise ProcurementIdempotencyConflictError("幂等键对应的请求正文不同")
        conversation = self.repository.get_session_by_task_id(existing.task_id)
        if conversation is None:
            raise ProcurementDataIntegrityError("采购任务缺少聊天会话")
        return ProcurementTaskResult(task=existing, conversation=conversation)

    @staticmethod
    def _validate_source(snapshot: ProcurementSourceSnapshot, expected_price_minor: int) -> None:
        """
        校验最新发布快照必须 active、CNY 且价格与商城整数分快照一致。

        输入服务端快照和预期价格；失败抛出不可用或价格变化错误，无写入副作用。
        """

        if (
            snapshot.availability is not CatalogAvailability.ACTIVE
            or snapshot.price is None
            or snapshot.currency != "CNY"
        ):
            raise ProcurementSourceUnavailableError("来源商品没有 active 的 CNY 发布快照")
        expected_price = Decimal(expected_price_minor) / Decimal(100)
        if snapshot.price != expected_price:
            raise ProcurementSourcePriceChangedError("来源商品价格已变化")
