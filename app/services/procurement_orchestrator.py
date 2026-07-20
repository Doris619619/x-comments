"""
本文件实现订单绑定闲鱼采购对话的最小、失败关闭编排器。

它属于 services 模块，把来源核验、身份发现、DeepSeek 草稿、确定性策略、受限聊天客户端
和短事务仓储串成最多三轮的状态机。它不接收客户资料、不议价、不购买、不付款、不填写
地址；真实聊天和自动发送分别由两个默认关闭的全局开关控制。
"""

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.ai.base import (
    ProcurementAiError,
    ProcurementDraftGenerator,
    ProcurementDraftRequest,
    UntrustedSellerMessage,
    validate_procurement_draft_output,
)
from app.crawler.chat_client import (
    ChatMessageSnapshot,
    ChatSafetyError,
    PolicyAllowedDraft,
    item_url_matches_binding,
    normalize_chat_text,
)
from app.crawler.chat_runtime import ProcurementChatClient, ProcurementChatFactory
from app.models.procurement import (
    ConversationSessionStatus,
    ProcurementExecutionTaskStatus,
)
from app.repositories.procurement_runtime import (
    DraftPersistenceCommand,
    ProcurementRuntimeRepository,
    ProcurementRuntimeTask,
    ProcurementSendNotAllowedError,
    RuntimeQueuedOutbound,
    RuntimeSellerMessage,
)
from app.schemas.procurement import ProcurementEventType, ProcurementObjective
from app.schemas.procurement_llm import ProcurementDecision, ProcurementLlmOutput
from app.services.item_verification import (
    LiveItemVerifier,
    LiveVerificationStatus,
    VerificationTarget,
)
from app.services.procurement_policy import AutoSendContext, evaluate_auto_send, scan_draft_risks

MAX_AUTO_ROUNDS = 3
PROMPT_INJECTION_PATTERN = re.compile(
    r"(?:忽略.{0,12}(?:指令|规则|系统)|system\s+prompt|developer\s+message|"
    r"ignore.{0,20}(?:instruction|previous)|你现在是.{0,20}(?:助手|系统))",
    re.I,
)


class ProcurementConversationOrchestrator:
    """
    每次领取并推进一个采购任务的一小步或一次完整安全发送。

    外部依赖均可替换为 fake；编排器从不自动重试 Playwright 发送，任何 ``sending`` 恢复
    都直接转人工审核。
    """

    def __init__(
        self,
        repository: ProcurementRuntimeRepository,
        verifier: LiveItemVerifier,
        draft_generator: ProcurementDraftGenerator,
        chat_factory: ProcurementChatFactory,
        *,
        chat_enabled: bool = False,
        auto_send_enabled: bool = False,
        expected_account_id: str,
        llm_model: str,
        min_confidence: float = 0.85,
        global_max_auto_rounds: int = 3,
        lease_seconds: int = 90,
        seller_poll_seconds: int = 15,
    ) -> None:
        """
        保存状态机依赖与双重默认关闭开关。

        输入仓储、核验器、草稿器、聊天工厂及安全配置；参数越界抛出 ValueError；初始化不
        访问数据库、闲鱼或 DeepSeek。
        """

        if auto_send_enabled and not chat_enabled:
            raise ValueError("自动发送开启时必须先开启采购聊天")
        if not expected_account_id.strip():
            raise ValueError("采购聊天必须配置预期闲鱼账号 ID")
        if not 0.8 <= min_confidence <= 1.0:
            raise ValueError("采购草稿最低置信度必须在 0.8 至 1.0")
        if not 1 <= global_max_auto_rounds <= MAX_AUTO_ROUNDS:
            raise ValueError("采购自动对话最多三轮")
        if lease_seconds < 30:
            raise ValueError("采购任务租约至少需要 30 秒")
        if seller_poll_seconds < 5:
            raise ValueError("卖家回复轮询间隔至少需要 5 秒")
        self._repository = repository
        self._verifier = verifier
        self._draft_generator = draft_generator
        self._chat_factory = chat_factory
        self._chat_enabled = chat_enabled
        self._auto_send_enabled = auto_send_enabled
        self._expected_account_id = expected_account_id.strip()
        self._llm_model = llm_model
        self._min_confidence = min_confidence
        self._global_max_auto_rounds = global_max_auto_rounds
        self._lease_seconds = lease_seconds
        self._seller_poll_seconds = seller_poll_seconds

    async def process_next(self, worker_id: str) -> bool:
        """
        领取并处理一个到期采购任务。

        输入稳定 Worker ID；聊天关闭或无任务返回 False，有任务返回 True；所有外部失败转换
        为粗粒度持久状态，最后释放租约，不泄露原始页面、模型响应或异常正文。
        """

        if not self._chat_enabled:
            return False
        now = datetime.now(UTC)
        task = self._repository.claim_next(
            worker_id,
            now,
            now + timedelta(seconds=self._lease_seconds),
        )
        if task is None:
            return False

        not_before: datetime | None = None
        try:
            not_before = await self._process_claimed(task, worker_id)
        except ChatSafetyError as exc:
            self._mark_chat_blocked(task, worker_id, exc.code)
        except ProcurementAiError as exc:
            self._repository.mark_terminal(
                task.task_id,
                worker_id,
                status=ProcurementExecutionTaskStatus.PROCUREMENT_FAILED,
                reason_code=exc.code,
                event_type=ProcurementEventType.CONVERSATION_FAILED,
                now=datetime.now(UTC),
            )
        except TimeoutError:
            self._repository.mark_terminal(
                task.task_id,
                worker_id,
                status=ProcurementExecutionTaskStatus.VERIFICATION_TIMEOUT,
                reason_code="procurement_cycle_timeout",
                event_type=ProcurementEventType.CONVERSATION_TIMED_OUT,
                now=datetime.now(UTC),
            )
        except Exception:
            self._repository.mark_terminal(
                task.task_id,
                worker_id,
                status=ProcurementExecutionTaskStatus.PROCUREMENT_FAILED,
                reason_code="procurement_internal_error",
                event_type=ProcurementEventType.CONVERSATION_FAILED,
                now=datetime.now(UTC),
            )
        finally:
            self._repository.release_claim(
                task.task_id,
                worker_id,
                not_before=not_before,
            )
        return True

    async def _process_claimed(
        self,
        task: ProcurementRuntimeTask,
        worker_id: str,
    ) -> datetime | None:
        """
        在已持有租约时核验来源、打开绑定会话并推进一次对话。

        输入不可变任务快照和 Worker；返回可选下一次轮询时间；所有外部动作按核验、草稿、
        策略、发送顺序执行，发送前必须先提交 ``sending``。
        """

        now = datetime.now(UTC)
        if not _deadline_active(task.response_deadline_at, now):
            self._repository.mark_terminal(
                task.task_id,
                worker_id,
                status=ProcurementExecutionTaskStatus.SELLER_UNRESPONSIVE,
                reason_code="seller_response_deadline_expired",
                event_type=ProcurementEventType.CONVERSATION_TIMED_OUT,
                now=now,
            )
            return None
        if task.has_uncertain_send:
            self._repository.require_human_review_after_send_uncertainty(
                task.task_id,
                worker_id,
                message_id=task.latest_outbound_message_id,
                reason_code="send_result_uncertain",
                now=now,
            )
            return None
        queued_outbound = (
            self._repository.get_queued_outbound(task.task_id)
            if task.has_pending_outbound
            else None
        )
        max_rounds = min(task.max_auto_rounds, self._global_max_auto_rounds, MAX_AUTO_ROUNDS)

        if not self._local_source_matches(task):
            self._repository.mark_terminal(
                task.task_id,
                worker_id,
                status=ProcurementExecutionTaskStatus.PROCUREMENT_FAILED,
                reason_code="source_snapshot_mismatch",
                event_type=ProcurementEventType.CONVERSATION_FAILED,
                now=now,
            )
            return None

        verification = await self._verifier.verify(VerificationTarget(item_id=task.source_item_id))
        if verification.status is not LiveVerificationStatus.AVAILABLE:
            self._record_verification_failure(task, worker_id, verification.status)
            return None
        expected_price = Decimal(task.expected_price_cny_minor) / Decimal(100)
        if verification.current_price != expected_price:
            self._repository.mark_terminal(
                task.task_id,
                worker_id,
                status=ProcurementExecutionTaskStatus.PRICE_CHANGED,
                reason_code="price_changed",
                event_type=ProcurementEventType.CONVERSATION_FAILED,
                now=datetime.now(UTC),
            )
            return None
        if task.task_status is ProcurementExecutionTaskStatus.PENDING_SOURCE_VERIFICATION:
            self._repository.mark_source_verified(task.task_id, worker_id, datetime.now(UTC))

        async with self._chat_factory.open(
            item_url=task.item_url,
            source_item_id=task.source_item_id,
            expected_seller_id=task.expected_seller_id,
            expected_account_id=self._expected_account_id,
        ) as opened:
            latest = await opened.client.open_conversation()
            self._repository.mark_conversation_opened(
                task.task_id,
                worker_id,
                seller_id=opened.binding.seller_id,
                account_id=opened.binding.account_id,
                baseline_fingerprint=latest.fingerprint,
                now=datetime.now(UTC),
            )
            if task.session_status is ConversationSessionStatus.PENDING_OPEN:
                return None
            if queued_outbound is not None:
                if task.round_count >= max_rounds:
                    self._repository.mark_review_ready(
                        task.task_id,
                        worker_id,
                        summary=_existing_or_empty_summary(task, "round_limit_reached"),
                        reason_code="round_limit_reached",
                        now=datetime.now(UTC),
                    )
                    return None
                await self._send_queued_draft(
                    task,
                    worker_id,
                    opened.client,
                    latest,
                    queued_outbound,
                )
                return datetime.now(UTC) + timedelta(seconds=self._seller_poll_seconds)
            reply_to_message_id: str | None = None
            inbound_created = False
            is_initial_baseline = (
                task.latest_outbound_message_id is None
                and latest.fingerprint == task.conversation_baseline_fingerprint
            )
            if latest.direction == "seller":
                if not is_initial_baseline:
                    reply_to_message_id, inbound_created = (
                        self._repository.record_inbound_message(
                            task.task_id,
                            worker_id,
                            external_message_id=latest.fingerprint,
                            content=latest.text,
                            content_hash=hashlib.sha256(
                                normalize_chat_text(latest.text).encode("utf-8")
                            ).hexdigest(),
                            observed_at=datetime.now(UTC),
                        )
                    )
            elif latest.direction not in {"self", "none"}:
                raise ChatSafetyError(
                    "message_direction_not_confirmed",
                    "无法确认最新聊天消息方向",
                )

            if inbound_created:
                return None

            if (
                task.task_status is ProcurementExecutionTaskStatus.AWAITING_SELLER_REPLY
                and not inbound_created
            ):
                return datetime.now(UTC) + timedelta(seconds=self._seller_poll_seconds)

            seller_messages = self._repository.list_seller_messages(task.task_id)
            if any(
                scan_draft_risks(message.content)
                or PROMPT_INJECTION_PATTERN.search(message.content)
                for message in seller_messages
            ):
                self._repository.mark_review_ready(
                    task.task_id,
                    worker_id,
                    summary=_existing_or_empty_summary(task, "seller_message_risk"),
                    reason_code="seller_message_risk",
                    now=datetime.now(UTC),
                )
                return None

            output, summary = await self._generate_draft(task, seller_messages)
            if task.round_count >= max_rounds:
                self._repository.mark_review_ready(
                    task.task_id,
                    worker_id,
                    summary=summary,
                    reason_code="round_limit_reached",
                    now=datetime.now(UTC),
                )
                return None
            if output.decision is not ProcurementDecision.CONTINUE_CONVERSATION:
                self._repository.mark_review_ready(
                    task.task_id,
                    worker_id,
                    summary=summary,
                    reason_code=output.reason_code,
                    now=datetime.now(UTC),
                )
                return None

            latest_before_policy = await opened.client.read_latest_message()
            policy = evaluate_auto_send(
                output,
                AutoSendContext(
                    enabled=self._auto_send_enabled,
                    session_status=ConversationSessionStatus.ACTIVE,
                    round_count=task.round_count,
                    max_auto_rounds=max_rounds,
                    min_confidence=self._min_confidence,
                    is_initial_outreach=reply_to_message_id is None,
                    latest_inbound_message_id=reply_to_message_id,
                    reply_to_message_id=reply_to_message_id,
                    has_pending_outbound=False,
                    item_matches=True,
                    seller_matches=True,
                    account_matches=True,
                    price_matches=True,
                    listing_available=True,
                    auth_and_risk_clear=True,
                    within_response_deadline=_deadline_active(
                        task.response_deadline_at,
                        datetime.now(UTC),
                    ),
                    write_lock_acquired=True,
                    latest_dom_unchanged=(latest_before_policy.fingerprint == latest.fingerprint),
                    cooldown_elapsed=True,
                    objective_still_open=bool(output.questions_remaining),
                ),
            )
            content = output.reply_draft or ""
            content_hash = hashlib.sha256(normalize_chat_text(content).encode("utf-8")).hexdigest()
            idempotency_key = hashlib.sha256(
                (
                    f"{task.task_id}:{task.round_count}:{reply_to_message_id or 'initial'}:"
                    f"{output.intent.value}:{content_hash}"
                ).encode()
            ).hexdigest()
            self._repository.save_draft(
                task.task_id,
                worker_id,
                DraftPersistenceCommand(
                    content=content,
                    content_hash=content_hash,
                    intent=output.intent.value,
                    llm_model=self._llm_model,
                    prompt_version="procurement-v1",
                    confidence=Decimal(str(output.confidence)),
                    risk_flags=tuple(flag.value for flag in output.risk_flags),
                    requires_human_review=output.requires_human_review,
                    policy_version=policy.policy_version,
                    policy_allowed=policy.allowed,
                    policy_reason_codes=tuple(reason.value for reason in policy.reason_codes),
                    reply_to_message_id=reply_to_message_id,
                    idempotency_key=idempotency_key,
                    summary=summary,
                ),
                datetime.now(UTC),
            )
            return None

    async def _send_queued_draft(
        self,
        task: ProcurementRuntimeTask,
        worker_id: str,
        client: ProcurementChatClient,
        latest: ChatMessageSnapshot,
        queued: RuntimeQueuedOutbound,
    ) -> None:
        """
        在草稿事件已回调商城后执行一次且仅一次受保护发送。

        输入任务、Worker、受限客户端、当前消息与排队草稿；上下文变化或发送不确定会转
        人工，取消先提交时静默停止；不自动重试。
        """

        if latest.fingerprint != queued.expected_latest_fingerprint:
            self._repository.require_human_review_after_send_uncertainty(
                task.task_id,
                worker_id,
                message_id=queued.message_id,
                reason_code="queued_context_changed",
                now=datetime.now(UTC),
            )
            return
        if not self._repository.prepare_single_send(
            task.task_id,
            worker_id,
            queued.message_id,
            datetime.now(UTC),
        ):
            self._repository.require_human_review_after_send_uncertainty(
                task.task_id,
                worker_id,
                message_id=queued.message_id,
                reason_code="send_attempt_already_recorded",
                now=datetime.now(UTC),
            )
            return
        try:
            with self._repository.hold_send_transaction(
                task.task_id,
                worker_id,
                queued.message_id,
            ) as send_transaction:
                try:
                    await client.send_policy_allowed_draft(
                        PolicyAllowedDraft(
                            text=queued.content,
                            policy_decision_id=queued.message_id,
                        ),
                        expected_latest_fingerprint=queued.expected_latest_fingerprint,
                        auto_send_enabled=self._auto_send_enabled,
                    )
                except Exception:
                    send_transaction.mark_uncertain(
                        "send_result_uncertain",
                        datetime.now(UTC),
                    )
                else:
                    send_transaction.confirm_sent(datetime.now(UTC))
        except ProcurementSendNotAllowedError:
            return

    async def _generate_draft(
        self,
        task: ProcurementRuntimeTask,
        seller_message_rows: list[RuntimeSellerMessage],
    ) -> tuple[ProcurementLlmOutput, dict[str, object]]:
        """
        从数据库中有限卖家证据构造请求，并在线程中调用同步草稿器。

        输入任务快照和已通过确定性敏感信息扫描的卖家消息；返回二次校验输出和已清洗
        回调摘要；模型或 Schema 错误安全向上抛出。
        """

        objectives = [ProcurementObjective(value) for value in task.objectives]
        answered, remaining = _question_partition(task, objectives)
        seller_messages = [
            UntrustedSellerMessage(
                message_id=UUID(message.message_id),
                content=message.content,
            )
            for message in seller_message_rows
        ]
        request = ProcurementDraftRequest(
            product_title=task.current_title or task.expected_title,
            objectives=objectives,
            questions_answered=answered,
            questions_remaining=remaining,
            seller_messages=seller_messages,
            round_count=task.round_count,
            max_auto_rounds=min(
                task.max_auto_rounds,
                self._global_max_auto_rounds,
                MAX_AUTO_ROUNDS,
            ),
            summary_only=(
                task.round_count
                >= min(task.max_auto_rounds, self._global_max_auto_rounds, MAX_AUTO_ROUNDS)
            ),
        )
        output = await asyncio.to_thread(self._draft_generator.generate, request)
        output = validate_procurement_draft_output(output, request)
        return output, _safe_summary_projection(output)

    def _local_source_matches(self, task: ProcurementRuntimeTask) -> bool:
        """
        核对任务与当前本地商品的 ID、官方 URL 和人民币价格快照。

        输入任务快照；完全匹配返回 True，否则 False；不访问网络或写数据库。
        """

        if (
            task.current_title is None
            or task.current_price is None
            or task.current_item_url is None
        ):
            return False
        if task.current_item_url != task.item_url:
            return False
        if not item_url_matches_binding(task.item_url, task.source_item_id):
            return False
        return task.current_price == Decimal(task.expected_price_cny_minor) / Decimal(100)

    def _record_verification_failure(
        self,
        task: ProcurementRuntimeTask,
        worker_id: str,
        status: LiveVerificationStatus,
    ) -> None:
        """
        将实时核验非 available 结果映射为有限粗粒度任务状态。

        输入任务、Worker 和核验状态；提交终态与事件；不记录页面原因原文。
        """

        if status is LiveVerificationStatus.UNAVAILABLE:
            task_status = ProcurementExecutionTaskStatus.SOURCE_SOLD
            reason = "source_sold"
            event = ProcurementEventType.CONVERSATION_FAILED
        elif status is LiveVerificationStatus.BLOCKED:
            task_status = ProcurementExecutionTaskStatus.BLOCKED_BY_AUTH_OR_RISK_CONTROL
            reason = "blocked_by_auth_or_risk_control"
            event = ProcurementEventType.CONVERSATION_BLOCKED
        else:
            task_status = ProcurementExecutionTaskStatus.VERIFICATION_TIMEOUT
            reason = "verification_timeout"
            event = ProcurementEventType.CONVERSATION_TIMED_OUT
        self._repository.mark_terminal(
            task.task_id,
            worker_id,
            status=task_status,
            reason_code=reason,
            event_type=event,
            now=datetime.now(UTC),
        )

    def _mark_chat_blocked(
        self,
        task: ProcurementRuntimeTask,
        worker_id: str,
        adapter_code: str,
    ) -> None:
        """
        把聊天适配器细分错误压缩为不泄露页面细节的有限阻断码。

        输入任务、Worker 和稳定适配器码；提交 blocked 状态；不保存异常正文或 DOM 信息。
        """

        reason_code = _coarse_chat_reason(adapter_code)
        self._repository.mark_terminal(
            task.task_id,
            worker_id,
            status=ProcurementExecutionTaskStatus.BLOCKED_BY_AUTH_OR_RISK_CONTROL,
            reason_code=reason_code,
            event_type=ProcurementEventType.CONVERSATION_BLOCKED,
            now=datetime.now(UTC),
        )


def _question_partition(
    task: ProcurementRuntimeTask,
    objectives: list[ProcurementObjective],
) -> tuple[list[ProcurementObjective], list[ProcurementObjective]]:
    """
    从上一轮内部摘要恢复已回答和待回答目标，并对异常状态失败关闭。

    输入任务与目标白名单；返回严格分区；摘要缺失时全部待回答，矛盾时抛出 ValueError。
    """

    if task.summary is None:
        return [], objectives
    answered = [ProcurementObjective(value) for value in task.summary.get("questions_answered", [])]
    remaining = [
        ProcurementObjective(value) for value in task.summary.get("questions_remaining", [])
    ]
    if set(answered) & set(remaining) or set(answered) | set(remaining) != set(objectives):
        raise ValueError("采购摘要问题分区不完整")
    return answered, remaining


def _safe_summary_projection(output: ProcurementLlmOutput) -> dict[str, object]:
    """
    将模型输出投影到 shopping Joi 白名单，并移除可能含 PII/链接的自由文本。

    输入严格模型输出；返回只含 resultSchema 字段的字典；不记录或发送原始卖家消息。
    """

    facts = output.facts.model_dump(mode="json")
    condition_summary = facts.get("condition_summary")
    if isinstance(condition_summary, str) and scan_draft_risks(condition_summary):
        facts["condition_summary"] = None
    defects = facts.get("defects")
    if isinstance(defects, list):
        facts["defects"] = [
            item for item in defects if isinstance(item, str) and not scan_draft_risks(item)
        ]
    return {
        "facts": facts,
        "confidence": output.confidence,
        "evidence_message_ids": [str(value) for value in output.evidence_message_ids],
        "questions_answered": [value.value for value in output.questions_answered],
        "questions_remaining": [value.value for value in output.questions_remaining],
        "reason_code": output.reason_code,
    }


def _existing_or_empty_summary(
    task: ProcurementRuntimeTask,
    reason_code: str,
) -> dict[str, object]:
    """
    返回已有安全摘要，或构造满足商城 resultSchema 的最小摘要。

    输入任务与原因码；返回新字典；无数据库或网络副作用。
    """

    if task.summary is not None:
        return dict(task.summary)
    return {
        "questions_answered": [],
        "questions_remaining": list(task.objectives),
        "reason_code": reason_code,
    }


def _deadline_active(deadline: datetime, now: datetime) -> bool:
    """
    统一比较可能来自 SQLite 离线测试的朴素时间与 UTC 当前时间。

    输入期限与当前时间；期限未到返回 True；无副作用。
    """

    normalized = deadline if deadline.tzinfo is not None else deadline.replace(tzinfo=UTC)
    return normalized > now


def _coarse_chat_reason(adapter_code: str) -> str:
    """
    将页面适配器错误压缩为固定、可回调且不泄露页面结构的原因码。

    输入稳定适配器码；统一返回商城已识别的阻断状态码；无副作用。
    """

    del adapter_code
    return "blocked_by_auth_or_risk_control"
