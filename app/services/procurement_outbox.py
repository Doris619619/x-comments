"""
本文件实现采购事务 Outbox 到固定商城回调地址的独立投递器。

它属于 services 模块，只重试 HTTP 回调事件，不读取或修改聊天发送状态，也绝不重新执行
Playwright 发送、购买或付款。回调使用独立令牌与事件幂等键，正文来自已校验 Outbox。
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol, Self

import httpx

from app.repositories.procurement_outbox import (
    ClaimedProcurementEvent,
    ProcurementOutboxRepository,
)


class CallbackDeliveryError(RuntimeError):
    """
    表示不包含响应正文、令牌或底层异常文本的商城回调失败。

    ``reason_code`` 可安全写入 Outbox；异常对象不泄露敏感上下文。
    """

    def __init__(self, reason_code: str, *, retryable: bool) -> None:
        """
        用稳定原因码创建安全投递异常。

        输入粗粒度原因；无返回；副作用仅为初始化异常对象。
        """

        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable


class ProcurementCallbackTransport(Protocol):
    """
    定义可被离线 fake 替换的固定商城回调传输接口。

    实现不得根据事件内容更换目标地址，也不得触发采购聊天动作。
    """

    async def send(
        self,
        *,
        callback_url: str,
        token: str,
        event: ClaimedProcurementEvent,
    ) -> None:
        """
        投递一条事件；成功无返回，失败抛出 ``CallbackDeliveryError``。

        输入固定 URL、专用令牌和事件；网络请求是唯一副作用。
        """


class HttpxProcurementCallbackTransport:
    """
    使用异步 httpx Client 向固定商城端点投递采购事件。

    每次请求携带 Bearer token 与事件 ID 幂等头；不记录响应正文或请求秘密。
    """

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        """
        创建专用异步 HTTP Client。

        输入正数超时；无返回；仅分配本地连接资源，不发起网络请求。
        """

        if timeout_seconds <= 0:
            raise ValueError("采购回调超时必须大于零")
        self._client = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    async def send(
        self,
        *,
        callback_url: str,
        token: str,
        event: ClaimedProcurementEvent,
    ) -> None:
        """
        向调用方固定配置的 URL 投递一次 Outbox 事件。

        输入 URL、令牌和事件；2xx 成功，其余 HTTP 或网络失败转为粗粒度安全异常；不重定向。
        """

        try:
            response = await self._client.post(
                callback_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": event.event_id,
                },
                json=event.payload,
            )
        except httpx.TimeoutException:
            raise CallbackDeliveryError("callback_timeout", retryable=True) from None
        except httpx.RequestError:
            raise CallbackDeliveryError("callback_transport_error", retryable=True) from None
        if not 200 <= response.status_code < 300:
            retryable = response.status_code in {409, 429} or response.status_code >= 500
            reason_code = (
                "callback_auth_rejected"
                if response.status_code in {401, 403}
                else "callback_contract_rejected"
                if response.status_code in {400, 422}
                else "callback_sequence_conflict"
                if response.status_code == 409
                else "callback_rate_limited"
                if response.status_code == 429
                else "callback_server_error"
                if response.status_code >= 500
                else "callback_http_rejected"
            )
            raise CallbackDeliveryError(reason_code, retryable=retryable)

    async def close(self) -> None:
        """
        关闭内部异步 HTTP Client。

        无输入和返回；释放连接资源，不投递事件。
        """

        await self._client.aclose()

    async def __aenter__(self) -> Self:
        """
        返回传输实例以支持异步上下文管理。

        无输入；返回自身；不发起网络请求。
        """

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """
        离开上下文时关闭内部 Client 且不吞掉异常。

        输入标准异常上下文；无返回；副作用仅为资源释放。
        """

        del exc_type, exc_value, traceback
        await self.close()


class ProcurementOutboxDispatcher:
    """
    领取一条 Outbox、调用传输并记录成功或有限退避。

    回调是至少一次投递；商城必须按 ``event_id`` 幂等。该类与聊天适配器无依赖，因此任何
    回调重试都不可能重新执行闲鱼消息发送。
    """

    def __init__(
        self,
        repository: ProcurementOutboxRepository,
        transport: ProcurementCallbackTransport,
        *,
        callback_url: str,
        token: str,
        max_attempts: int = 8,
        lease_seconds: int = 30,
    ) -> None:
        """
        保存固定端点、独立令牌、仓储和有限重试参数。

        输入依赖与配置；无返回；参数非法抛出 ValueError；不发起数据库或网络操作。
        """

        if not callback_url.strip():
            raise ValueError("商城采购回调 URL 不能为空")
        if len(token.strip()) < 32:
            raise ValueError("商城采购回调令牌至少需要 32 字符")
        if not 1 <= max_attempts <= 20:
            raise ValueError("采购回调最大尝试次数必须在 1 至 20 之间")
        if lease_seconds < 5:
            raise ValueError("采购回调租约至少需要 5 秒")
        self._repository = repository
        self._transport = transport
        self._callback_url = callback_url
        self._token = token
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds

    async def dispatch_next(self, worker_id: str) -> bool:
        """
        领取并投递一条到期事件。

        输入 Worker ID；有事件时返回 True，否则 False；失败会记录粗粒度原因和退避时间，
        不向循环抛出传输异常，也不执行任何聊天动作。
        """

        now = datetime.now(UTC)
        event = self._repository.claim_next(
            worker_id,
            now,
            now + timedelta(seconds=self._lease_seconds),
            self._max_attempts,
        )
        if event is None:
            return False
        try:
            await self._transport.send(
                callback_url=self._callback_url,
                token=self._token,
                event=event,
            )
        except CallbackDeliveryError as exc:
            retry_at = (
                now + timedelta(seconds=_retry_delay_seconds(event.attempt_count))
                if exc.retryable and event.attempt_count < self._max_attempts
                else None
            )
            self._repository.mark_failed(
                event.outbox_id,
                worker_id,
                reason_code=exc.reason_code,
                next_attempt_at=retry_at,
                now=datetime.now(UTC),
            )
            return True
        self._repository.mark_delivered(
            event.outbox_id,
            worker_id,
            datetime.now(UTC),
        )
        return True


def _retry_delay_seconds(attempt_count: int) -> int:
    """
    为 Outbox 尝试次数计算有上限的确定性指数退避秒数。

    输入从一开始的尝试次数；返回 5 至 300 秒；无随机数、数据库或网络副作用。
    """

    return int(min(300, 5 * (2 ** max(0, attempt_count - 1))))
