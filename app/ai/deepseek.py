"""
本文件实现 DeepSeek Chat Completions 的采购草稿适配器。

它属于 ai 模块，只发送提示并返回严格验证的草稿；配置由调用方显式注入，
不读取环境变量、不记录密钥/提示词/卖家原文，也不操作数据库、Playwright、购买或付款。
"""

from typing import Any, Self

import httpx
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic import field_validator as pydantic_field_validator

from app.ai.base import (
    ProcurementAiHttpError,
    ProcurementAiOutputError,
    ProcurementAiTimeoutError,
    ProcurementAiTransportError,
    ProcurementDraftRequest,
    validate_procurement_draft_output,
)
from app.ai.prompts.procurement_v1 import build_procurement_messages
from app.schemas.procurement_llm import ProcurementLlmOutput

MAX_PROVIDER_CONTENT_LENGTH = 20_000


class DeepSeekConfig(BaseModel):
    """
    表示调用 DeepSeek 所需的显式、不可变配置。

    API 密钥使用 SecretStr 防止对象展示泄漏；配置不从环境变量自动读取，也不产生网络副作用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    api_key: SecretStr = Field(min_length=16)
    base_url: AnyHttpUrl = Field(default=AnyHttpUrl("https://api.deepseek.com"))
    model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=128)
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    max_tokens: int = Field(default=1200, ge=256, le=4096)
    temperature: float = Field(default=0.1, ge=0.0, le=1.0)

    @pydantic_field_validator("base_url")
    @classmethod
    def require_https_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """
        要求模型端点使用 HTTPS，避免密钥和消息在传输中明文暴露。

        输入已解析 URL 并返回原值；非 HTTPS 时抛出 ValueError，无外部副作用。
        """

        if value.scheme != "https":
            raise ValueError("DeepSeek base_url 必须使用 HTTPS")
        return value

    @property
    def chat_completions_url(self) -> str:
        """
        返回去除尾部斜杠后的 Chat Completions 完整地址。

        无输入，返回 HTTPS URL 字符串；不读取密钥且无外部副作用。
        """

        return f"{str(self.base_url).rstrip('/')}/chat/completions"


class DeepSeekDraftGenerator:
    """
    使用同步 httpx Client 调用 DeepSeek 并生成采购草稿。

    调用方可注入 MockTransport 或共享 Client；实例不会关闭外部注入的 Client。
    """

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """
        保存显式配置并接收可选 HTTP Client。

        输入配置与可注入 Client；未注入时创建自有 Client，除此之外不发起网络请求。
        """

        self._config = config
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client()

    def generate(self, request: ProcurementDraftRequest) -> ProcurementLlmOutput:
        """
        调用 DeepSeek 生成并严格验证一份采购聊天草稿。

        输入不可信数据已标记的请求并返回验证输出；网络、HTTP、JSON 或安全偏离均抛安全异常。
        """

        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": build_procurement_messages(request),
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        response = self._request_completion(payload)
        output = self._parse_response(response)
        return validate_procurement_draft_output(output, request)

    def close(self) -> None:
        """
        关闭由本实例创建的 HTTP Client，保留调用方注入 Client 的所有权。

        无输入和返回值；可能释放自有连接资源，不发送请求，也不记录配置或消息。
        """

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        """
        返回生成器自身以支持受控关闭的上下文管理。

        无输入并返回自身；不发送网络请求且无其他副作用。
        """

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """
        离开上下文时仅关闭实例自建 Client。

        输入标准异常上下文且无返回值；不吞掉异常，也不关闭外部注入 Client。
        """

        del exc_type, exc_value, traceback
        self.close()

    def _request_completion(self, payload: dict[str, Any]) -> httpx.Response:
        """
        发起一次非流式 DeepSeek 请求并屏蔽含敏感上下文的底层异常。

        输入请求 JSON 并返回 HTTP 200 响应；超时、传输或非 200 状态转换为固定安全异常。
        """

        try:
            response = self._client.post(
                self._config.chat_completions_url,
                headers={
                    "Authorization": (
                        f"Bearer {self._config.api_key.get_secret_value()}"
                    ),
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._config.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise ProcurementAiTimeoutError from None
        except httpx.RequestError:
            raise ProcurementAiTransportError from None

        if response.status_code != 200:
            raise ProcurementAiHttpError
        return response

    def _parse_response(self, response: httpx.Response) -> ProcurementLlmOutput:
        """
        从供应商响应提取唯一完成内容并按采购 Schema 严格解析。

        输入 HTTP 200 响应并返回结构化输出；包络、结束原因、JSON 或 Schema 偏离均安全失败。
        """

        try:
            body: object = response.json()
        except (ValueError, UnicodeDecodeError):
            raise ProcurementAiOutputError from None

        content = _extract_completed_content(body)
        try:
            return ProcurementLlmOutput.model_validate_json(content)
        except ValidationError:
            raise ProcurementAiOutputError from None


def _extract_completed_content(body: object) -> str:
    """
    从 DeepSeek 非流式包络提取自然结束的字符串内容。

    输入已解码 JSON 对象并返回有限长度字符串；任一结构偏离或非 stop 结束均抛安全输出异常。
    """

    if not isinstance(body, dict):
        raise ProcurementAiOutputError
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProcurementAiOutputError
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise ProcurementAiOutputError
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProcurementAiOutputError
    content = message.get("content")
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > MAX_PROVIDER_CONTENT_LENGTH
    ):
        raise ProcurementAiOutputError
    return content
