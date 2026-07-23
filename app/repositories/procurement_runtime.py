"""
本文件封装采购对话 Worker 所需的短事务、租约、消息状态和 Outbox 写入。

它属于 repositories 模块，负责把编排器的确定性命令映射为 SQLAlchemy 操作；不调用
DeepSeek、不操作 Playwright、不投递 HTTP，也不决定草稿内容或自动发送策略。
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.item import Item
from app.models.procurement import (
    ConversationMessage,
    ConversationMessageDirection,
    ConversationMessageStatus,
    ConversationSenderRole,
    ConversationSession,
    ConversationSessionStatus,
    ProcurementAuditActorType,
    ProcurementAuthorizationSource,
    ProcurementExecutionMode,
    ProcurementExecutionTask,
    ProcurementExecutionTaskStatus,
    ProcurementNextAction,
    ProcurementOutbox,
    ProcurementOutboxStatus,
    ProcurementPolicyResult,
)
from app.repositories.procurement_events import (
    append_procurement_audit,
    append_procurement_event,
)
from app.schemas.procurement import ProcurementEventType

RUNNABLE_TASK_STATUSES = (
    ProcurementExecutionTaskStatus.PENDING_SOURCE_VERIFICATION,
    ProcurementExecutionTaskStatus.CONTACTING_SELLER,
    ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY,
)
PENDING_OUTBOUND_STATUSES = (
    ConversationMessageStatus.SEND_QUEUED,
    ConversationMessageStatus.SENDING,
)


@dataclass(frozen=True, slots=True)
class ProcurementRuntimeTask:
    """
    表示 Worker 已租用任务的只读、无敏感信息执行快照。

    快照包含绑定校验、草稿上下文和状态机所需字段；不携带 Cookie、令牌或客户资料。
    """

    task_id: str
    session_id: str
    contract_version: int
    execution_mode: ProcurementExecutionMode
    auto_send_authorized: bool
    authorized_at: datetime | None
    authorization_source: ProcurementAuthorizationSource | None
    source_item_id: str
    item_url: str
    expected_seller_id: str | None
    expected_title: str
    expected_price_cny_minor: int
    objectives: tuple[str, ...]
    max_auto_rounds: int
    response_deadline_at: datetime
    task_status: ProcurementExecutionTaskStatus
    next_action: ProcurementNextAction
    session_status: ConversationSessionStatus
    round_count: int
    seller_poll_attempt_count: int
    latest_inbound_message_id: str | None
    latest_outbound_message_id: str | None
    conversation_baseline_fingerprint: str | None
    summary: dict[str, Any] | None
    current_title: str | None
    current_price: Decimal | None
    current_item_url: str | None
    has_pending_outbound: bool
    has_uncertain_send: bool


@dataclass(frozen=True, slots=True)
class RuntimeSellerMessage:
    """表示从数据库读取、准备交给 AI 的单条不可信卖家消息。"""

    message_id: str
    content: str


@dataclass(frozen=True, slots=True)
class RuntimeQueuedOutbound:
    """
    表示已通过策略、等待回调确认后执行唯一发送尝试的出站草稿。

    ``expected_latest_fingerprint`` 来自本任务首屏基线或被回复入站消息，不含登录态。
    """

    message_id: str
    content: str
    expected_latest_fingerprint: str


@dataclass(frozen=True, slots=True)
class DraftPersistenceCommand:
    """
    表示编排器已经完成模型校验和策略判断后要原子保存的草稿命令。

    命令只携带模型结构化元数据和正文；仓储不会重新判断是否允许发送。
    """

    content: str
    content_hash: str
    intent: str
    llm_model: str
    prompt_version: str
    confidence: Decimal
    risk_flags: tuple[str, ...]
    requires_human_review: bool
    policy_version: str
    policy_allowed: bool
    policy_reason_codes: tuple[str, ...]
    reply_to_message_id: str | None
    idempotency_key: str
    summary: dict[str, Any]


class ProcurementSendNotAllowedError(RuntimeError):
    """
    表示发送事务锁定后发现任务已取消、租约失效或消息状态不允许点击。

    编排器必须在收到该异常时停止发送，不能自动重试或覆盖取消状态。
    """


class ProcurementSendTransaction:
    """
    在任务行锁存续期间完成一次发送结果的数据库最终化。

    实例只由 ``hold_send_transaction`` 创建；调用方必须在退出前选择确认发送或标记不确定。
    """

    def __init__(
        self,
        db: Session,
        task: ProcurementExecutionTask,
        conversation: ConversationSession,
        message: ConversationMessage,
    ) -> None:
        """
        保存持锁事务中的 ORM 对象。

        输入当前数据库事务与三条已锁定记录；无返回；不提交、回滚或执行外部动作。
        """

        self._db = db
        self._task = task
        self._conversation = conversation
        self._message = message
        self.finalized = False

    def confirm_sent(self, now: datetime, confirmed_fingerprint: str) -> None:
        """
        在页面出现本人同文消息证据后，于持锁事务中确认 sent。

        输入确认时间与页面消息指纹；无返回；更新消息、轮次、页面游标、审计和 Outbox，
        但提交由上下文负责。
        """

        if self.finalized:
            raise RuntimeError("发送事务已经最终化")
        if len(confirmed_fingerprint) != 64:
            raise RuntimeError("发送确认消息指纹无效")
        self._message.status = ConversationMessageStatus.SENT
        self._message.external_message_id = confirmed_fingerprint
        self._message.sent_at = now
        self._message.updated_at = now
        self._conversation.round_count += 1
        self._conversation.seller_poll_attempt_count = 0
        self._conversation.conversation_key = confirmed_fingerprint
        self._conversation.last_outbound_at = now
        self._conversation.status = ConversationSessionStatus.WAITING_SELLER
        self._conversation.version += 1
        self._conversation.updated_at = now
        self._task.status = ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY
        self._task.next_action = ProcurementNextAction.WAIT_SELLER
        self._task.updated_at = now
        append_procurement_audit(
            self._db,
            self._task,
            self._conversation,
            actor_type=ProcurementAuditActorType.PLAYWRIGHT,
            action="assistant_message_sent",
            occurred_at=now,
            message_id=self._message.message_id,
            idempotency_suffix=self._message.idempotency_key,
            metadata_redacted={"send_attempt_count": self._message.send_attempt_count},
        )
        append_procurement_event(
            self._db,
            self._task,
            self._conversation,
            ProcurementEventType.ASSISTANT_MESSAGE_SENT,
            now,
            message_id=self._message.message_id,
            data={
                "round_count": self._conversation.round_count,
                "response_deadline_at": self._task.response_deadline_at.isoformat(),
            },
        )
        self.finalized = True

    def mark_uncertain(self, reason_code: str, now: datetime) -> None:
        """
        在页面发送结果无法确认时，于持锁事务中永久转人工审核。

        输入稳定原因码和时间；无返回；不保存底层异常，不允许后续自动重试。
        """

        if self.finalized:
            raise RuntimeError("发送事务已经最终化")
        self._message.status = ConversationMessageStatus.SEND_FAILED
        self._message.requires_human_review = True
        self._message.updated_at = now
        self._task.status = ProcurementExecutionTaskStatus.AWAITING_PROCUREMENT_REVIEW
        self._task.next_action = ProcurementNextAction.HUMAN_REVIEW
        self._task.reason_code = reason_code
        self._task.reason_detail_safe = None
        self._task.updated_at = now
        self._conversation.status = ConversationSessionStatus.HUMAN_REVIEW_REQUIRED
        self._conversation.error_code = reason_code
        self._conversation.error_detail_safe = None
        self._conversation.version += 1
        self._conversation.updated_at = now
        append_procurement_audit(
            self._db,
            self._task,
            self._conversation,
            actor_type=ProcurementAuditActorType.SYSTEM,
            action="assistant_send_uncertain",
            occurred_at=now,
            message_id=self._message.message_id,
            reason_code=reason_code,
            idempotency_suffix=self._message.idempotency_key,
        )
        append_procurement_event(
            self._db,
            self._task,
            self._conversation,
            ProcurementEventType.ASSISTANT_MESSAGE_BLOCKED,
            now,
            message_id=self._message.message_id,
            reason_code=reason_code,
            data={"policy_reason_codes": [reason_code]},
        )
        self.finalized = True


class ProcurementRuntimeRepository:
    """
    为单个采购 Worker 提供可恢复的短事务状态机存储。

    普通公开方法使用短事务；唯独 ``hold_send_transaction`` 会在单次 Playwright 点击与结果
    最终化期间持有任务行锁，用于严格串行化取消与发送。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """
        保存已绑定引擎的会话工厂。

        输入会话工厂；无返回和外部副作用；数据库错误在具体方法调用时向上抛出。
        """

        self._session_factory = session_factory

    def claim_next(
        self,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> ProcurementRuntimeTask | None:
        """
        原子领取一条到期且未被其他 Worker 持有的采购任务。

        输入 Worker 标识、当前时间和租约期限；返回任务快照或 None；成功会提交任务与会话
        租约，PostgreSQL 使用 ``SKIP LOCKED``，SQLite 离线测试由单线程保证。
        """

        with self._session_factory() as db:
            accepted_callback_delivered = (
                select(ProcurementOutbox.outbox_id)
                .where(
                    ProcurementOutbox.task_id == ProcurementExecutionTask.task_id,
                    ProcurementOutbox.event_type == ProcurementEventType.TASK_ACCEPTED.value,
                    ProcurementOutbox.status == ProcurementOutboxStatus.DELIVERED,
                )
                .exists()
            )
            no_undelivered_callback = ~(
                select(ProcurementOutbox.outbox_id)
                .where(
                    ProcurementOutbox.task_id == ProcurementExecutionTask.task_id,
                    ProcurementOutbox.status != ProcurementOutboxStatus.DELIVERED,
                )
                .exists()
            )
            task = db.scalar(
                select(ProcurementExecutionTask)
                .where(
                    ProcurementExecutionTask.status.in_(RUNNABLE_TASK_STATUSES),
                    accepted_callback_delivered,
                    no_undelivered_callback,
                    or_(
                        ProcurementExecutionTask.lease_until.is_(None),
                        ProcurementExecutionTask.lease_until <= now,
                    ),
                )
                .order_by(
                    ProcurementExecutionTask.lease_until.asc().nullsfirst(),
                    ProcurementExecutionTask.created_at.asc(),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if task is None:
                return None
            conversation = self._require_conversation(db, task.task_id)
            task.lease_owner = worker_id
            task.lease_until = lease_until
            conversation.lease_owner = worker_id
            conversation.lease_until = lease_until
            db.commit()
            return self._snapshot(db, task, conversation)

    def release_claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        not_before: datetime | None = None,
    ) -> None:
        """
        释放当前 Worker 的任务租约，并可设置下一次允许领取时间。

        输入任务、Worker 和可选延迟时间；租约已失效或被接管时不修改；成功提交短事务。
        """

        with self._session_factory() as db:
            task = db.get(ProcurementExecutionTask, task_id)
            if task is None or task.lease_owner != worker_id:
                return
            conversation = self._require_conversation(db, task_id)
            if (
                not_before is not None
                and task.status is ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY
            ):
                # 每次进入等待卖家回复阶段只增加一次退避计数；收到回复后会在保存消息时归零。
                conversation.seller_poll_attempt_count += 1
            elif task.status is not ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY:
                conversation.seller_poll_attempt_count = 0
            task.lease_owner = None
            task.lease_until = not_before
            conversation.lease_owner = None
            conversation.lease_until = not_before
            db.commit()

    def mark_source_verified(self, task_id: str, worker_id: str, now: datetime) -> None:
        """
        将成功完成商品与价格核验的任务推进到打开会话。

        输入已租用任务、Worker 和时间；租约不匹配抛出 RuntimeError；提交状态与脱敏审计。
        """

        with self._session_factory() as db:
            task, conversation = self._require_claim(db, task_id, worker_id)
            previous = task.status.value
            task.status = ProcurementExecutionTaskStatus.CONTACTING_SELLER
            task.next_action = ProcurementNextAction.OPEN_CONVERSATION
            task.updated_at = now
            append_procurement_audit(
                db,
                task,
                conversation,
                actor_type=ProcurementAuditActorType.SYSTEM,
                action="source_verified",
                occurred_at=now,
                from_status=previous,
                to_status=task.status.value,
                idempotency_suffix="source_verified",
            )
            db.commit()

    def mark_conversation_opened(
        self,
        task_id: str,
        worker_id: str,
        *,
        seller_id: str,
        account_id: str,
        baseline_fingerprint: str,
        now: datetime,
    ) -> None:
        """
        在页面完成三方身份校验后记录会话已打开并创建 Outbox 事件。

        输入已租用任务、可信绑定身份、首屏消息基线指纹和时间；提交状态、审计与事件；
        不保存登录凭据，且后续不会把首屏历史消息误当成本任务回复。
        """

        with self._session_factory() as db:
            if len(baseline_fingerprint) != 64:
                raise RuntimeError("聊天首屏基线指纹无效")
            task, conversation = self._require_claim(db, task_id, worker_id)
            if (
                conversation.expected_seller_id is not None
                and conversation.expected_seller_id != seller_id
            ):
                raise RuntimeError("页面卖家身份与会话已锁定卖家不一致")
            if conversation.account_key is not None and conversation.account_key != account_id:
                raise RuntimeError("页面账号身份与会话已锁定账号不一致")
            conversation.expected_seller_id = seller_id
            conversation.observed_seller_id = seller_id
            conversation.account_key = account_id
            if conversation.status is ConversationSessionStatus.PENDING_OPEN:
                conversation.status = ConversationSessionStatus.ACTIVE
                conversation.opened_at = now
                conversation.conversation_key = baseline_fingerprint
                conversation.version += 1
                conversation.updated_at = now
                task.next_action = ProcurementNextAction.GENERATE_DRAFT
                task.updated_at = now
                append_procurement_audit(
                    db,
                    task,
                    conversation,
                    actor_type=ProcurementAuditActorType.PLAYWRIGHT,
                    action="conversation_opened",
                    occurred_at=now,
                    from_status=ConversationSessionStatus.PENDING_OPEN.value,
                    to_status=ConversationSessionStatus.ACTIVE.value,
                    idempotency_suffix="conversation_opened",
                )
                append_procurement_event(
                    db,
                    task,
                    conversation,
                    ProcurementEventType.CONVERSATION_OPENED,
                    now,
                    data={"round_count": conversation.round_count},
                )
            db.commit()

    def record_inbound_message(
        self,
        task_id: str,
        worker_id: str,
        *,
        external_message_id: str,
        content: str,
        content_hash: str,
        observed_at: datetime,
    ) -> tuple[str, bool]:
        """
        幂等保存绑定会话最新卖家消息并推进到生成草稿。

        输入稳定外部消息标识、正文摘要和观察时间；返回本地消息 ID 与是否新建；新建时
        同事务更新游标、审计和 Outbox，重复监听不会产生第二条记录。
        """

        idempotency_key = hashlib.sha256(
            f"inbound:{task_id}:{external_message_id}:{content_hash}".encode()
        ).hexdigest()
        with self._session_factory() as db:
            task, conversation = self._require_claim(db, task_id, worker_id)
            existing = db.scalar(
                select(ConversationMessage).where(
                    ConversationMessage.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return existing.message_id, False
            message = ConversationMessage(
                session_id=conversation.session_id,
                seq=self._next_message_seq(db, conversation.session_id),
                direction=ConversationMessageDirection.INBOUND,
                sender_role=ConversationSenderRole.SELLER,
                external_message_id=external_message_id,
                content=content,
                content_hash=content_hash,
                status=ConversationMessageStatus.RECEIVED,
                idempotency_key=idempotency_key,
                observed_at=observed_at,
            )
            db.add(message)
            db.flush()
            conversation.latest_inbound_message_id = message.message_id
            conversation.conversation_key = external_message_id
            conversation.seller_poll_attempt_count = 0
            conversation.last_inbound_at = observed_at
            conversation.status = ConversationSessionStatus.ACTIVE
            conversation.version += 1
            conversation.updated_at = observed_at
            task.status = ProcurementExecutionTaskStatus.CONTACTING_SELLER
            task.next_action = ProcurementNextAction.GENERATE_DRAFT
            task.updated_at = observed_at
            append_procurement_audit(
                db,
                task,
                conversation,
                actor_type=ProcurementAuditActorType.PLAYWRIGHT,
                action="seller_message_received",
                occurred_at=observed_at,
                message_id=message.message_id,
                idempotency_suffix=idempotency_key,
                metadata_redacted={"message_sha256": content_hash},
            )
            append_procurement_event(
                db,
                task,
                conversation,
                ProcurementEventType.SELLER_MESSAGE_RECEIVED,
                observed_at,
                message_id=message.message_id,
            )
            db.commit()
            return message.message_id, True

    def list_seller_messages(self, task_id: str, limit: int = 20) -> list[RuntimeSellerMessage]:
        """
        按会话序号读取最近的有限条卖家消息供模型作为不可信证据。

        输入任务 ID 和上限；返回按时间正序的消息；只读数据库且不写日志。
        """

        with self._session_factory() as db:
            conversation = self._require_conversation(db, task_id)
            rows = list(
                db.scalars(
                    select(ConversationMessage)
                    .where(
                        ConversationMessage.session_id == conversation.session_id,
                        ConversationMessage.direction == ConversationMessageDirection.INBOUND,
                    )
                    .order_by(ConversationMessage.seq.desc())
                    .limit(limit)
                )
            )
            rows.reverse()
            return [RuntimeSellerMessage(row.message_id, row.content) for row in rows]

    def get_queued_outbound(self, task_id: str) -> RuntimeQueuedOutbound | None:
        """
        读取任务唯一 ``send_queued`` 草稿及其发送前消息指纹。

        输入任务 ID；无排队草稿返回 None；上下文缺失抛出 RuntimeError；只读数据库。
        """

        with self._session_factory() as db:
            conversation = self._require_conversation(db, task_id)
            queued_rows = list(
                db.scalars(
                    select(ConversationMessage)
                    .where(
                        ConversationMessage.session_id == conversation.session_id,
                        ConversationMessage.status == ConversationMessageStatus.SEND_QUEUED,
                    )
                    .limit(2)
                )
            )
            if not queued_rows:
                return None
            if len(queued_rows) != 1:
                raise RuntimeError("同一会话存在多个排队草稿")
            queued = queued_rows[0]
            if queued.reply_to_message_id is not None:
                inbound = db.get(ConversationMessage, queued.reply_to_message_id)
                fingerprint = inbound.external_message_id if inbound is not None else None
            else:
                fingerprint = conversation.conversation_key
            if fingerprint is None or len(fingerprint) != 64:
                raise RuntimeError("排队草稿缺少稳定发送上下文指纹")
            return RuntimeQueuedOutbound(
                message_id=queued.message_id,
                content=queued.content,
                expected_latest_fingerprint=fingerprint,
            )

    def save_draft(
        self,
        task_id: str,
        worker_id: str,
        command: DraftPersistenceCommand,
        now: datetime,
    ) -> tuple[str, bool]:
        """
        幂等保存 AI 草稿及确定性策略结论，并创建对应 Outbox 事件。

        输入已租用任务、经上层校验的命令和时间；返回消息 ID 与是否允许发送；策略阻止时
        同事务进入人工审核，仓储本身绝不执行页面发送。
        """

        with self._session_factory() as db:
            task, conversation = self._require_claim(db, task_id, worker_id)
            existing = db.scalar(
                select(ConversationMessage).where(
                    ConversationMessage.idempotency_key == command.idempotency_key
                )
            )
            if existing is not None:
                return (
                    existing.message_id,
                    existing.policy_result is ProcurementPolicyResult.ALLOWED,
                )
            status = (
                ConversationMessageStatus.SEND_QUEUED
                if command.policy_allowed
                else ConversationMessageStatus.POLICY_BLOCKED
            )
            message = ConversationMessage(
                session_id=conversation.session_id,
                seq=self._next_message_seq(db, conversation.session_id),
                direction=ConversationMessageDirection.OUTBOUND,
                sender_role=ConversationSenderRole.BUYER,
                reply_to_message_id=command.reply_to_message_id,
                content=command.content,
                content_hash=command.content_hash,
                intent=command.intent,
                status=status,
                llm_model=command.llm_model,
                prompt_version=command.prompt_version,
                llm_confidence=command.confidence,
                risk_flags=list(command.risk_flags),
                requires_human_review=command.requires_human_review,
                policy_version=command.policy_version,
                policy_result=(
                    ProcurementPolicyResult.ALLOWED
                    if command.policy_allowed
                    else ProcurementPolicyResult.BLOCKED
                ),
                policy_reason_codes=list(command.policy_reason_codes),
                idempotency_key=command.idempotency_key,
                generated_at=now,
                queued_at=now if command.policy_allowed else None,
            )
            db.add(message)
            db.flush()
            task.summary = command.summary
            task.updated_at = now
            conversation.latest_outbound_message_id = message.message_id
            conversation.version += 1
            conversation.updated_at = now
            append_procurement_audit(
                db,
                task,
                conversation,
                actor_type=ProcurementAuditActorType.LLM,
                action="assistant_draft_created",
                occurred_at=now,
                message_id=message.message_id,
                reason_code=command.policy_reason_codes[0] if command.policy_reason_codes else None,
                idempotency_suffix=command.idempotency_key,
                metadata_redacted={"draft_sha256": command.content_hash},
            )
            append_procurement_event(
                db,
                task,
                conversation,
                ProcurementEventType.ASSISTANT_DRAFT_CREATED,
                now,
                message_id=message.message_id,
            )
            if not command.policy_allowed:
                task.status = ProcurementExecutionTaskStatus.AWAITING_PROCUREMENT_REVIEW
                task.next_action = ProcurementNextAction.HUMAN_REVIEW
                task.reason_code = (
                    command.policy_reason_codes[0]
                    if command.policy_reason_codes
                    else "policy_blocked"
                )
                conversation.status = ConversationSessionStatus.HUMAN_REVIEW_REQUIRED
                append_procurement_event(
                    db,
                    task,
                    conversation,
                    ProcurementEventType.ASSISTANT_MESSAGE_BLOCKED,
                    now,
                    message_id=message.message_id,
                    reason_code=task.reason_code,
                    data={
                        "policy_reason_codes": list(command.policy_reason_codes),
                        "risk_flags": list(command.risk_flags),
                    },
                )
            db.commit()
            return message.message_id, command.policy_allowed

    def prepare_single_send(
        self,
        task_id: str,
        worker_id: str,
        message_id: str,
        now: datetime,
    ) -> bool:
        """
        在 Playwright 点击前将唯一发送尝试原子标记为 ``sending``。

        输入任务、Worker、消息和时间；仅 ``send_queued`` 且从未尝试时返回 True；其他状态
        返回 False，确保进程崩溃恢复后不会再次执行同一发送动作。
        """

        with self._session_factory() as db:
            task, conversation = self._require_claim(db, task_id, worker_id)
            message = db.get(ConversationMessage, message_id)
            if (
                message is None
                or message.session_id != conversation.session_id
                or message.status is not ConversationMessageStatus.SEND_QUEUED
                or message.send_attempt_count != 0
            ):
                return False
            message.status = ConversationMessageStatus.SENDING
            message.send_attempt_count = 1
            message.updated_at = now
            append_procurement_audit(
                db,
                task,
                conversation,
                actor_type=ProcurementAuditActorType.SYSTEM,
                action="assistant_send_started",
                occurred_at=now,
                message_id=message.message_id,
                idempotency_suffix=message.idempotency_key,
            )
            db.commit()
            return True

    @contextmanager
    def hold_send_transaction(
        self,
        task_id: str,
        worker_id: str,
        message_id: str,
    ) -> Iterator[ProcurementSendTransaction]:
        """
        在 Playwright 点击和数据库最终化期间持有任务行锁，串行化取消与发送。

        输入任务、Worker 和已提交为 ``sending`` 的消息；产出最终化对象。若取消先提交、
        租约失效或状态不符则抛出 ``ProcurementSendNotAllowedError``，绝不执行页面动作；
        上下文正常退出且已最终化时提交，否则回滚，进程崩溃后保留先前已提交的 sending。
        """

        db = self._session_factory()
        try:
            task = db.scalar(
                select(ProcurementExecutionTask)
                .where(ProcurementExecutionTask.task_id == task_id)
                .with_for_update()
            )
            if (
                task is None
                or task.lease_owner != worker_id
                or task.status is ProcurementExecutionTaskStatus.CANCELLED
                or task.auto_send_authorized is not True
            ):
                raise ProcurementSendNotAllowedError("取消、授权撤销或租约失效后禁止发送")
            conversation = db.scalar(
                select(ConversationSession)
                .where(ConversationSession.task_id == task_id)
                .with_for_update()
            )
            message = db.scalar(
                select(ConversationMessage)
                .where(ConversationMessage.message_id == message_id)
                .with_for_update()
            )
            if (
                conversation is None
                or conversation.lease_owner != worker_id
                or conversation.status is ConversationSessionStatus.CANCELLED
                or message is None
                or message.session_id != conversation.session_id
                or message.status is not ConversationMessageStatus.SENDING
                or message.send_attempt_count != 1
            ):
                raise ProcurementSendNotAllowedError("发送消息状态不再允许点击")
            transaction = ProcurementSendTransaction(db, task, conversation, message)
            yield transaction
            if not transaction.finalized:
                raise RuntimeError("发送事务退出前必须明确最终化")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def require_human_review_after_send_uncertainty(
        self,
        task_id: str,
        worker_id: str,
        *,
        message_id: str | None,
        reason_code: str,
        now: datetime,
    ) -> None:
        """
        将发送结果不确定或恢复到 ``sending`` 的任务永久转人工，不自动重试。

        输入粗粒度原因和可选消息；提交失败关闭状态及回调事件；不记录原始异常或页面内容。
        """

        with self._session_factory() as db:
            task, conversation = self._require_claim(db, task_id, worker_id)
            message = db.get(ConversationMessage, message_id) if message_id else None
            if message is not None and message.status in PENDING_OUTBOUND_STATUSES:
                message.status = ConversationMessageStatus.SEND_FAILED
                message.requires_human_review = True
                message.updated_at = now
            task.status = ProcurementExecutionTaskStatus.AWAITING_PROCUREMENT_REVIEW
            task.next_action = ProcurementNextAction.HUMAN_REVIEW
            task.reason_code = reason_code
            task.reason_detail_safe = None
            task.updated_at = now
            conversation.status = ConversationSessionStatus.HUMAN_REVIEW_REQUIRED
            conversation.error_code = reason_code
            conversation.error_detail_safe = None
            conversation.version += 1
            conversation.updated_at = now
            append_procurement_audit(
                db,
                task,
                conversation,
                actor_type=ProcurementAuditActorType.SYSTEM,
                action="assistant_send_uncertain",
                occurred_at=now,
                message_id=message.message_id if message is not None else None,
                reason_code=reason_code,
                idempotency_suffix=message.idempotency_key if message is not None else reason_code,
            )
            if message is not None:
                append_procurement_event(
                    db,
                    task,
                    conversation,
                    ProcurementEventType.ASSISTANT_MESSAGE_BLOCKED,
                    now,
                    message_id=message.message_id,
                    reason_code=reason_code,
                    data={"policy_reason_codes": [reason_code]},
                )
            else:
                append_procurement_event(
                    db,
                    task,
                    conversation,
                    ProcurementEventType.CONVERSATION_BLOCKED,
                    now,
                    reason_code=reason_code,
                )
            db.commit()

    def mark_review_ready(
        self,
        task_id: str,
        worker_id: str,
        *,
        summary: dict[str, Any],
        reason_code: str,
        now: datetime,
    ) -> None:
        """
        保存结构化摘要并把模型停止建议交给人工采购审核。

        输入脱敏摘要、原因码和时间；提交人工审核状态与 Outbox；不执行购买或付款。
        """

        with self._session_factory() as db:
            task, conversation = self._require_claim(db, task_id, worker_id)
            task.summary = summary
            is_canary = task.execution_mode is ProcurementExecutionMode.OPERATOR_CANARY
            task.status = (
                ProcurementExecutionTaskStatus.CANARY_COMPLETED
                if is_canary
                else ProcurementExecutionTaskStatus.AWAITING_PROCUREMENT_REVIEW
            )
            task.next_action = (
                ProcurementNextAction.NONE if is_canary else ProcurementNextAction.HUMAN_REVIEW
            )
            task.reason_code = reason_code
            task.updated_at = now
            if is_canary:
                task.completed_at = now
                conversation.status = ConversationSessionStatus.COMPLETED
                conversation.closed_at = now
            else:
                conversation.status = ConversationSessionStatus.HUMAN_REVIEW_REQUIRED
            conversation.version += 1
            conversation.updated_at = now
            append_procurement_audit(
                db,
                task,
                conversation,
                actor_type=ProcurementAuditActorType.LLM,
                action="conversation_summary_ready",
                occurred_at=now,
                reason_code=reason_code,
                idempotency_suffix=f"summary:{conversation.version}",
            )
            append_procurement_event(
                db,
                task,
                conversation,
                ProcurementEventType.CONVERSATION_SUMMARY_READY,
                now,
                reason_code=reason_code,
                data={"result": summary},
            )
            db.commit()

    def mark_terminal(
        self,
        task_id: str,
        worker_id: str,
        *,
        status: ProcurementExecutionTaskStatus,
        reason_code: str,
        event_type: ProcurementEventType,
        now: datetime,
    ) -> None:
        """
        将核验、认证、风控或页面漂移失败写为粗粒度终态。

        输入白名单任务终态、稳定原因和事件类型；提交状态、审计及 Outbox；不保存原始异常。
        """

        with self._session_factory() as db:
            task, conversation = self._require_claim(db, task_id, worker_id)
            previous = task.status.value
            task.status = status
            task.next_action = ProcurementNextAction.NONE
            task.reason_code = reason_code
            task.reason_detail_safe = None
            task.completed_at = now
            task.updated_at = now
            conversation.status = (
                ConversationSessionStatus.BLOCKED
                if status is ProcurementExecutionTaskStatus.BLOCKED_BY_AUTH_OR_RISK_CONTROL
                else ConversationSessionStatus.FAILED
            )
            conversation.error_code = reason_code
            conversation.error_detail_safe = None
            conversation.closed_at = now
            conversation.version += 1
            conversation.updated_at = now
            append_procurement_audit(
                db,
                task,
                conversation,
                actor_type=ProcurementAuditActorType.SYSTEM,
                action="conversation_terminal",
                occurred_at=now,
                from_status=previous,
                to_status=status.value,
                reason_code=reason_code,
                idempotency_suffix=f"terminal:{status.value}:{reason_code}",
            )
            append_procurement_event(
                db,
                task,
                conversation,
                event_type,
                now,
                reason_code=reason_code,
            )
            db.commit()

    def _snapshot(
        self,
        db: Session,
        task: ProcurementExecutionTask,
        conversation: ConversationSession,
    ) -> ProcurementRuntimeTask:
        """
        将当前 ORM 行转换为离开事务后仍可安全使用的不可变快照。

        输入数据库会话、任务和会话；返回值不含秘密；只执行本地数据库读取。
        """

        item = db.get(Item, task.source_item_id)
        pending_rows = list(
            db.scalars(
                select(ConversationMessage).where(
                    ConversationMessage.session_id == conversation.session_id,
                    ConversationMessage.status.in_(PENDING_OUTBOUND_STATUSES),
                )
            )
        )
        return ProcurementRuntimeTask(
            task_id=task.task_id,
            session_id=conversation.session_id,
            contract_version=task.contract_version,
            execution_mode=task.execution_mode,
            auto_send_authorized=task.auto_send_authorized,
            authorized_at=task.authorized_at,
            authorization_source=task.authorization_source,
            source_item_id=task.source_item_id,
            item_url=conversation.item_url,
            expected_seller_id=conversation.expected_seller_id,
            expected_title=task.expected_title,
            expected_price_cny_minor=task.expected_price_cny_minor,
            objectives=tuple(task.objectives),
            max_auto_rounds=task.max_auto_rounds,
            response_deadline_at=task.response_deadline_at,
            task_status=task.status,
            next_action=task.next_action,
            session_status=conversation.status,
            round_count=conversation.round_count,
            seller_poll_attempt_count=conversation.seller_poll_attempt_count,
            latest_inbound_message_id=conversation.latest_inbound_message_id,
            latest_outbound_message_id=conversation.latest_outbound_message_id,
            conversation_baseline_fingerprint=conversation.conversation_key,
            summary=dict(task.summary) if task.summary is not None else None,
            current_title=item.title if item is not None else None,
            current_price=item.price if item is not None else None,
            current_item_url=item.item_url if item is not None else None,
            has_pending_outbound=bool(pending_rows),
            has_uncertain_send=any(
                row.status is ConversationMessageStatus.SENDING for row in pending_rows
            ),
        )

    @staticmethod
    def _require_conversation(db: Session, task_id: str) -> ConversationSession:
        """
        读取任务唯一会话，不存在时抛出完整性错误。

        输入数据库会话和任务 ID；返回 ORM 会话；只读数据库且无提交副作用。
        """

        conversation = db.scalar(
            select(ConversationSession).where(ConversationSession.task_id == task_id)
        )
        if conversation is None:
            raise RuntimeError("采购任务缺少唯一聊天会话")
        return conversation

    @classmethod
    def _require_claim(
        cls,
        db: Session,
        task_id: str,
        worker_id: str,
    ) -> tuple[ProcurementExecutionTask, ConversationSession]:
        """
        校验当前短事务仍由指定 Worker 持有租约。

        输入数据库会话、任务和 Worker；返回任务与会话；不匹配时抛出 RuntimeError。
        """

        task = db.get(ProcurementExecutionTask, task_id)
        if task is None or task.lease_owner != worker_id:
            raise RuntimeError("采购任务租约已失效")
        conversation = cls._require_conversation(db, task_id)
        if conversation.lease_owner != worker_id:
            raise RuntimeError("采购会话租约已失效")
        return task, conversation

    @staticmethod
    def _next_message_seq(db: Session, session_id: str) -> int:
        """
        返回会话内下一条单调消息序号。

        输入数据库会话和会话 ID；返回最大序号加一；只读当前事务，唯一约束处理并发冲突。
        """

        current = db.scalar(
            select(func.max(ConversationMessage.seq)).where(
                ConversationMessage.session_id == session_id
            )
        )
        return int(current or 0) + 1
