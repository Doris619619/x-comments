"""
本文件封装本地采购执行任务、聊天会话及目录快照的数据库访问。

它属于 repositories 模块，负责短事务和唯一约束映射；不构造 HTTP 响应、不调用大模型、
不操作 Playwright，也不决定购买或付款。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.catalog_sync import CatalogAvailability, CatalogChange, CatalogRevision
from app.models.item import Item
from app.models.procurement import (
    ConversationMessage,
    ConversationSession,
    ConversationSessionStatus,
    ProcurementAuthorizationSource,
    ProcurementExecutionMode,
    ProcurementExecutionTask,
    ProcurementExecutionTaskStatus,
    ProcurementNextAction,
)
from app.repositories.procurement_events import append_procurement_event
from app.schemas.procurement import ProcurementEventType


class ProcurementWriteConflictError(RuntimeError):
    """
    表示任务 ID 或幂等键触发数据库唯一约束冲突。

    仓储在回滚事务后抛出该异常；异常本身不包含原始请求或敏感信息。
    """


@dataclass(frozen=True, slots=True)
class ProcurementSourceSnapshot:
    """
    表示创建任务时从本地 Item 和最新发布 Catalog 读取的服务端快照。

    无发布快照时 availability、price 和 currency 为 None；对象不可变且无副作用。
    """

    item_id: str
    item_url: str
    availability: CatalogAvailability | None
    price: Decimal | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class NewProcurementExecutionTask:
    """
    表示服务层完成校验后交给仓储原子创建的任务参数。

    参数不含客户资料或付款信息；对象只传递数据，无外部副作用。
    """

    task_id: str
    contract_version: int
    execution_mode: ProcurementExecutionMode
    auto_send_authorized: bool
    authorized_at: datetime | None
    authorization_source: ProcurementAuthorizationSource | None
    source_item_id: str
    expected_title: str
    expected_price_cny_minor: int
    objectives: list[str]
    max_auto_rounds: int
    response_deadline_at: datetime
    request_idempotency_key: str
    request_body_hash: str
    expected_seller_id: str | None
    item_url: str


class ProcurementRepository:
    """
    提供采购任务幂等查询、原子创建、读取和取消所需数据库操作。

    输入请求级 SQLAlchemy 会话；写方法自行提交或回滚，数据库异常会明确向上抛出。
    """

    def __init__(self, session: Session) -> None:
        """
        保存请求级数据库会话。

        输入有效 Session；无返回和异常；副作用仅为保存引用。
        """

        self.session = session

    def get_source_snapshot(self, item_id: str) -> ProcurementSourceSnapshot | None:
        """
        读取 Item 中的服务端 URL 和该商品最近一次已发布 Catalog 快照。

        输入商品 ID；Item 不存在返回 None，未发布则返回空发布字段；无写入副作用。
        """

        item = self.session.get(Item, item_id)
        if item is None:
            return None
        change = self.session.scalar(
            select(CatalogChange)
            .join(CatalogRevision, CatalogRevision.revision == CatalogChange.revision)
            .where(
                CatalogChange.item_id == item_id,
                CatalogRevision.status == "published",
            )
            .order_by(CatalogChange.revision.desc())
            .limit(1)
        )
        return ProcurementSourceSnapshot(
            item_id=item.item_id,
            item_url=item.item_url,
            availability=change.availability if change is not None else None,
            price=change.price if change is not None else None,
            currency=change.currency if change is not None else None,
        )

    def get_by_task_id(self, task_id: str) -> ProcurementExecutionTask | None:
        """
        按商城任务 UUID 查询本地执行任务。

        输入任务 ID，返回 ORM 对象或 None；数据库异常向上抛出，无写入副作用。
        """

        return self.session.get(ProcurementExecutionTask, task_id)

    def get_by_idempotency_key(self, key: str) -> ProcurementExecutionTask | None:
        """
        按请求幂等键查询已持久化任务。

        输入不记录日志的幂等键，返回 ORM 对象或 None；无写入副作用。
        """

        return self.session.scalar(
            select(ProcurementExecutionTask).where(
                ProcurementExecutionTask.request_idempotency_key == key
            )
        )

    def get_active_by_source_item_id(
        self,
        source_item_id: str,
    ) -> ProcurementExecutionTask | None:
        """
        查询同一来源商品当前是否已有活动采购任务。

        输入闲鱼商品 ID；返回活动任务或 None；只读数据库，不暴露消息正文。
        """

        active_statuses = (
            ProcurementExecutionTaskStatus.PENDING_SOURCE_VERIFICATION,
            ProcurementExecutionTaskStatus.CONTACTING_SELLER,
            ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY,
            ProcurementExecutionTaskStatus.AWAITING_PROCUREMENT_REVIEW,
        )
        return self.session.scalar(
            select(ProcurementExecutionTask)
            .where(
                ProcurementExecutionTask.source_item_id == source_item_id,
                ProcurementExecutionTask.status.in_(active_statuses),
            )
            .limit(1)
        )

    def get_session_by_task_id(self, task_id: str) -> ConversationSession | None:
        """
        返回任务唯一关联的聊天会话。

        输入任务 ID，返回会话或 None；数据库异常向上抛出，无写入副作用。
        """

        return self.session.scalar(
            select(ConversationSession).where(ConversationSession.task_id == task_id)
        )

    def create_with_session(
        self, command: NewProcurementExecutionTask
    ) -> tuple[ProcurementExecutionTask, ConversationSession]:
        """
        在同一事务中创建本地执行任务和未分配账号的聊天会话。

        输入已校验命令，返回两个 ORM 对象；唯一约束冲突时回滚并抛出
        ProcurementWriteConflictError，副作用为成功时提交数据库事务。
        """

        task = ProcurementExecutionTask(
            task_id=command.task_id,
            contract_version=command.contract_version,
            execution_mode=command.execution_mode,
            auto_send_authorized=command.auto_send_authorized,
            authorized_at=command.authorized_at,
            authorization_source=command.authorization_source,
            source_item_id=command.source_item_id,
            expected_title=command.expected_title,
            expected_price_cny_minor=command.expected_price_cny_minor,
            objectives=list(command.objectives),
            max_auto_rounds=command.max_auto_rounds,
            response_deadline_at=command.response_deadline_at,
            request_idempotency_key=command.request_idempotency_key,
            request_body_hash=command.request_body_hash,
            status=ProcurementExecutionTaskStatus.PENDING_SOURCE_VERIFICATION,
            next_action=ProcurementNextAction.VERIFY_SOURCE,
        )
        conversation = ConversationSession(
            task_id=command.task_id,
            source_item_id=command.source_item_id,
            item_url=command.item_url,
            expected_seller_id=command.expected_seller_id,
            account_key=None,
            status=ConversationSessionStatus.PENDING_OPEN,
        )
        self.session.add_all((task, conversation))
        try:
            self.session.flush()
            append_procurement_event(
                self.session,
                task,
                conversation,
                ProcurementEventType.TASK_ACCEPTED,
                task.created_at,
            )
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ProcurementWriteConflictError("采购任务唯一约束冲突") from exc
        self.session.refresh(task)
        self.session.refresh(conversation)
        return task, conversation

    def list_messages(
        self,
        task_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[list[ConversationMessage], bool]:
        """
        按会话序号分页读取完整采购聊天记录。

        输入任务、游标和上限；返回有序消息及是否还有下一页；只读数据库且不写日志。
        """

        conversation = self.get_session_by_task_id(task_id)
        if conversation is None:
            return [], False
        rows = list(
            self.session.scalars(
                select(ConversationMessage)
                .where(
                    ConversationMessage.session_id == conversation.session_id,
                    ConversationMessage.seq > after_seq,
                )
                .order_by(ConversationMessage.seq.asc())
                .limit(limit + 1)
            )
        )
        return rows[:limit], len(rows) > limit

    def cancel(
        self,
        task: ProcurementExecutionTask,
        conversation: ConversationSession,
        reason_code: str,
        cancelled_at: datetime,
    ) -> None:
        """
        将任务和关联会话在同一事务中标记为取消。

        输入已校验对象、稳定原因码和 UTC 时间；数据库异常回滚后向上抛出；副作用为提交更新。
        """

        try:
            locked_task = self.session.scalar(
                select(ProcurementExecutionTask)
                .where(ProcurementExecutionTask.task_id == task.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            locked_conversation = self.session.scalar(
                select(ConversationSession)
                .where(ConversationSession.session_id == conversation.session_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if locked_task is None or locked_conversation is None:
                raise ProcurementWriteConflictError("取消时采购任务或会话已不存在")
            if locked_task.status is ProcurementExecutionTaskStatus.CANCELLED:
                self.session.commit()
                return
            terminal_states = {
                ProcurementExecutionTaskStatus.SOURCE_SOLD,
                ProcurementExecutionTaskStatus.PRICE_CHANGED,
                ProcurementExecutionTaskStatus.SELLER_UNRESPONSIVE,
                ProcurementExecutionTaskStatus.SELLER_RISK,
                ProcurementExecutionTaskStatus.VERIFICATION_TIMEOUT,
                ProcurementExecutionTaskStatus.PROCUREMENT_FAILED,
                ProcurementExecutionTaskStatus.BLOCKED_BY_AUTH_OR_RISK_CONTROL,
            }
            if locked_task.status in terminal_states:
                raise ProcurementWriteConflictError("采购任务已进入不可取消终态")
            locked_task.status = ProcurementExecutionTaskStatus.CANCELLED
            locked_task.next_action = ProcurementNextAction.NONE
            locked_task.reason_code = reason_code
            locked_task.reason_detail_safe = None
            locked_task.cancelled_at = cancelled_at
            locked_task.updated_at = cancelled_at
            locked_task.lease_owner = None
            locked_task.lease_until = None
            locked_conversation.status = ConversationSessionStatus.CANCELLED
            locked_conversation.closed_at = cancelled_at
            locked_conversation.updated_at = cancelled_at
            locked_conversation.lease_owner = None
            locked_conversation.lease_until = None
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(task)
        self.session.refresh(conversation)
