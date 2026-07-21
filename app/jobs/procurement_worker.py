"""
本文件装配并运行采购对话编排器与独立 Outbox 回调投递循环。

它属于 jobs 模块，只在 scheduler-worker 角色启动；DeepSeek 与商城回调密钥在此进程读取，
不会注入 API 容器。聊天与自动发送默认关闭，回调重试绝不重新执行 Playwright 发送。
"""

import asyncio
from contextlib import suppress
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.ai.deepseek import DeepSeekConfig, DeepSeekDraftGenerator
from app.core.config import Settings
from app.crawler.chat_runtime import PlaywrightXianyuChatFactory
from app.crawler.item_verifier import XianyuItemVerifier
from app.repositories.procurement_outbox import ProcurementOutboxRepository
from app.repositories.procurement_runtime import ProcurementRuntimeRepository
from app.services.procurement_orchestrator import ProcurementConversationOrchestrator
from app.services.procurement_outbox import (
    HttpxProcurementCallbackTransport,
    ProcurementOutboxDispatcher,
)
from app.services.xianyu_account_guard import AccountAccessGuard


class ProcurementBackgroundWorker:
    """
    在一个串行协程中推进采购任务并投递最多一条 Outbox 事件。

    账号级 guard 与采集 Worker 共享，避免同一登录态并发访问；该类不提供购买或付款入口。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        account_guard: AccountAccessGuard,
    ) -> None:
        """
        按默认关闭配置装配可选编排器和可选固定回调投递器。

        输入会话工厂、配置和共享账号 guard；无返回；仅创建本地对象，不访问外部网络。
        """

        self._settings = settings
        self._worker_id = f"procurement-worker-{uuid4().hex[:12]}"
        self._task: asyncio.Task[None] | None = None
        self._generator: DeepSeekDraftGenerator | None = None
        self._transport: HttpxProcurementCallbackTransport | None = None
        self._orchestrator: ProcurementConversationOrchestrator | None = None
        self._dispatcher: ProcurementOutboxDispatcher | None = None

        if settings.procurement_chat_enabled:
            api_key = settings.deepseek_api_key
            account_id = settings.xianyu_expected_account_id
            if api_key is None or account_id is None:
                raise ValueError("采购聊天配置未通过失败关闭校验")
            self._generator = DeepSeekDraftGenerator(
                DeepSeekConfig(
                    api_key=api_key,
                    base_url=settings.deepseek_base_url,
                    model=settings.deepseek_model,
                    timeout_seconds=settings.deepseek_timeout_seconds,
                )
            )
            self._orchestrator = ProcurementConversationOrchestrator(
                ProcurementRuntimeRepository(session_factory),
                XianyuItemVerifier(settings, account_guard),
                self._generator,
                PlaywrightXianyuChatFactory(settings, account_guard),
                chat_enabled=settings.procurement_chat_enabled,
                auto_send_enabled=settings.procurement_auto_send_enabled,
                expected_account_id=account_id,
                llm_model=settings.deepseek_model,
                min_confidence=settings.procurement_auto_send_min_confidence,
                global_max_auto_rounds=settings.procurement_max_auto_rounds,
                lease_seconds=settings.procurement_task_lease_seconds,
                seller_poll_seconds=settings.procurement_seller_poll_seconds,
            )

        callback_url = (settings.shopping_callback_url or "").strip()
        callback_token = settings.shopping_procurement_token
        if callback_url and callback_token is not None:
            self._transport = HttpxProcurementCallbackTransport()
            self._dispatcher = ProcurementOutboxDispatcher(
                ProcurementOutboxRepository(session_factory),
                self._transport,
                callback_url=callback_url,
                token=callback_token.get_secret_value(),
                max_attempts=settings.procurement_outbox_max_attempts,
            )

    def start(self) -> None:
        """
        启动唯一后台采购循环。

        无输入和返回；无运行事件循环时抛出 RuntimeError；副作用仅为创建协程任务。
        """

        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """
        取消后台循环并关闭自建 DeepSeek/HTTP 客户端。

        无输入和返回；取消异常被处理；不投递新事件或执行聊天发送。
        """

        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._transport is not None:
            await self._transport.close()
        if self._generator is not None:
            self._generator.close()

    async def _run(self) -> None:
        """
        持续串行执行一次任务推进和一次 Outbox 投递。

        无输入和返回；单次异常被限制在当前周期，随后按配置等待，避免无间隔失败循环。
        """

        while True:
            did_work = False
            try:
                if self._orchestrator is not None:
                    did_work = await asyncio.wait_for(
                        self._orchestrator.process_next(self._worker_id),
                        timeout=self._settings.procurement_task_lease_seconds,
                    )
                if self._dispatcher is not None:
                    did_work = await self._dispatcher.dispatch_next(self._worker_id) or did_work
            except asyncio.CancelledError:
                raise
            except Exception:
                did_work = False
            if not did_work:
                await asyncio.sleep(self._settings.procurement_worker_poll_seconds)
