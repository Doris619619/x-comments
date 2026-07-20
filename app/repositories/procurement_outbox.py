"""
本文件封装采购事务 Outbox 的领取、成功确认和退避重试持久化。

它属于 repositories 模块，只管理事件投递状态和短租约；不发起 HTTP、不读取回调令牌，
也不触发任何 Playwright 聊天发送、购买或付款动作。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.models.procurement import ProcurementOutbox, ProcurementOutboxStatus


@dataclass(frozen=True, slots=True)
class ClaimedProcurementEvent:
    """表示 Outbox 投递器已持有短租约的一条完整回调事件。"""

    outbox_id: str
    event_id: str
    payload: dict[str, Any]
    attempt_count: int


class ProcurementOutboxRepository:
    """
    为独立回调投递器提供可恢复、有限重试的短事务操作。

    每次操作独立打开会话，网络等待期间不会持有数据库事务。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """
        保存已绑定数据库引擎的会话工厂。

        输入会话工厂；无返回；初始化不连接数据库且无其他副作用。
        """

        self._session_factory = session_factory

    def claim_next(
        self,
        worker_id: str,
        now: datetime,
        locked_until: datetime,
        max_attempts: int,
    ) -> ClaimedProcurementEvent | None:
        """
        原子领取一条到期事件或恢复一条租约过期事件。

        输入 Worker、当前时间、锁期限和最大尝试次数；返回快照或 None；成功会把状态写为
        processing 并递增尝试次数，PostgreSQL 使用 ``SKIP LOCKED``。
        """

        with self._session_factory() as db:
            prior = aliased(ProcurementOutbox)
            no_undelivered_prior_event = (
                ~select(prior.outbox_id)
                .where(
                    prior.task_id == ProcurementOutbox.task_id,
                    prior.event_seq < ProcurementOutbox.event_seq,
                    prior.status != ProcurementOutboxStatus.DELIVERED,
                )
                .exists()
            )
            event = db.scalar(
                select(ProcurementOutbox)
                .where(
                    ProcurementOutbox.attempt_count < max_attempts,
                    no_undelivered_prior_event,
                    or_(
                        and_(
                            ProcurementOutbox.status == ProcurementOutboxStatus.PENDING,
                            or_(
                                ProcurementOutbox.next_attempt_at.is_(None),
                                ProcurementOutbox.next_attempt_at <= now,
                            ),
                        ),
                        and_(
                            ProcurementOutbox.status == ProcurementOutboxStatus.FAILED,
                            ProcurementOutbox.next_attempt_at.is_not(None),
                            ProcurementOutbox.next_attempt_at <= now,
                        ),
                        and_(
                            ProcurementOutbox.status == ProcurementOutboxStatus.PROCESSING,
                            ProcurementOutbox.locked_until <= now,
                        ),
                    ),
                )
                .order_by(ProcurementOutbox.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if event is None:
                return None
            event.status = ProcurementOutboxStatus.PROCESSING
            event.attempt_count += 1
            event.locked_by = worker_id
            event.locked_until = locked_until
            event.updated_at = now
            db.commit()
            return ClaimedProcurementEvent(
                outbox_id=event.outbox_id,
                event_id=event.event_id,
                payload=dict(event.payload),
                attempt_count=event.attempt_count,
            )

    def mark_delivered(self, outbox_id: str, worker_id: str, now: datetime) -> None:
        """
        将当前 Worker 成功投递的事件标记为 delivered。

        输入 Outbox ID、Worker 和确认时间；租约不匹配抛出 RuntimeError；提交成功终态。
        """

        with self._session_factory() as db:
            event = self._require_claim(db, outbox_id, worker_id)
            event.status = ProcurementOutboxStatus.DELIVERED
            event.delivered_at = now
            event.next_attempt_at = None
            event.locked_by = None
            event.locked_until = None
            event.last_error_safe = None
            event.updated_at = now
            db.commit()

    def mark_failed(
        self,
        outbox_id: str,
        worker_id: str,
        *,
        reason_code: str,
        next_attempt_at: datetime | None,
        now: datetime,
    ) -> None:
        """
        记录一次粗粒度投递失败并安排有限退避或停止重试。

        输入事件、Worker、稳定原因、可选下次时间和当前时间；提交 failed 状态；不保存响应
        正文、URL 查询参数或底层异常详情。
        """

        with self._session_factory() as db:
            event = self._require_claim(db, outbox_id, worker_id)
            event.status = ProcurementOutboxStatus.FAILED
            event.next_attempt_at = next_attempt_at
            event.locked_by = None
            event.locked_until = None
            event.last_error_safe = reason_code
            event.updated_at = now
            db.commit()

    @staticmethod
    def _require_claim(
        db: Session,
        outbox_id: str,
        worker_id: str,
    ) -> ProcurementOutbox:
        """
        返回仍由指定 Worker 持有的 processing 事件。

        输入数据库会话、Outbox ID 和 Worker；状态不匹配时抛出 RuntimeError；只读当前事务。
        """

        event = db.get(ProcurementOutbox, outbox_id)
        if (
            event is None
            or event.status is not ProcurementOutboxStatus.PROCESSING
            or event.locked_by != worker_id
        ):
            raise RuntimeError("采购 Outbox 投递租约已失效")
        return event
