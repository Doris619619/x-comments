"""
本文件负责从环境变量读取应用配置。

它属于 core 模块，为数据库、API 和爬虫提供只读配置，不读取登录态内容。
"""

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    定义应用环境配置。

    输入来自环境变量和可选 `.env`；返回类型安全的配置对象。校验失败时抛出
    Pydantic 异常，没有外部副作用。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://xcomments:xcomments@127.0.0.1:5432/x_comments"
    xianyu_storage_state_path: str = "storage_state.json"
    xianyu_headless: bool = True
    xianyu_max_pages: int = Field(default=3, ge=1, le=3)
    xianyu_max_items: int = Field(default=50, ge=1, le=50)
    xianyu_max_images_per_item: int = Field(default=9, ge=1, le=9)
    xianyu_detail_image_budget_seconds: int = Field(default=30, ge=5, le=60)
    xianyu_page_delay_seconds: float = Field(default=2.0, ge=1.0, le=10.0)
    xianyu_collect_timeout_seconds: int = Field(default=120, ge=30, le=300)
    xianyu_verify_timeout_seconds: int = Field(default=12, ge=5, le=30)
    xianyu_api_token: SecretStr | None = None
    catalog_scheduler_interval_seconds: int = Field(default=600, ge=60, le=3600)
    app_role: Literal["api", "scheduler_worker"] = "api"
    worker_poll_seconds: int = Field(default=5, ge=1, le=60)
    catalog_missing_threshold: int = Field(default=2, ge=2, le=10)
    catalog_sync_token: SecretStr | None = None
    procurement_chat_enabled: bool = False
    procurement_auto_send_enabled: bool = False
    procurement_auto_send_min_confidence: float = Field(default=0.85, ge=0.8, le=1.0)
    procurement_max_auto_rounds: int = Field(default=3, ge=1, le=3)
    procurement_api_token: SecretStr | None = None
    procurement_source_item_allowlist: str = ""
    procurement_worker_poll_seconds: int = Field(default=5, ge=1, le=60)
    procurement_task_lease_seconds: int = Field(default=90, ge=30, le=300)
    procurement_seller_poll_seconds: int = Field(default=15, ge=5, le=300)
    procurement_outbox_max_attempts: int = Field(default=8, ge=1, le=20)
    xianyu_expected_account_id: str | None = None
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    shopping_callback_url: str | None = None
    shopping_procurement_token: SecretStr | None = None
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def require_postgresql_url(cls, value: str) -> str:
        """
        拒绝 SQLite 或其他非 PostgreSQL 运行时数据库连接串。

        输入 DATABASE_URL；返回 PostgreSQL psycopg URL；不符合时抛出 ValueError，无副作用。
        """

        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL 必须使用 postgresql+psycopg:// PostgreSQL 连接串")
        return value

    @field_validator("deepseek_api_key", "shopping_procurement_token", mode="before")
    @classmethod
    def normalize_optional_worker_secret(cls, value: object) -> object | None:
        """
        将 Compose 注入的空字符串规范化为未配置密钥。

        输入原始环境值；返回原值或 None；不展示 SecretStr 内容且无外部副作用。
        """

        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        return value if str(value).strip() else None

    @field_validator("procurement_source_item_allowlist")
    @classmethod
    def normalize_procurement_source_item_allowlist(cls, value: str) -> str:
        """
        规范化采购商品白名单并拒绝不稳定的闲鱼商品标识。

        输入英文逗号分隔的 item_id；返回去重后的同格式字符串；含非数字或超长值时
        抛出 ValueError，无外部副作用。
        """

        items = [item.strip() for item in str(value or "").split(",") if item.strip()]
        if any(not item.isdigit() or len(item) > 64 for item in items):
            raise ValueError("PROCUREMENT_SOURCE_ITEM_ALLOWLIST 只能包含数字 item_id")
        return ",".join(dict.fromkeys(items))

    @property
    def procurement_source_item_ids(self) -> frozenset[str]:
        """
        返回采购 API 可以接受的闲鱼商品 ID 集合。

        无输入；返回不可变集合；空集合表示失败关闭，不访问数据库或网络。
        """

        return frozenset(item for item in self.procurement_source_item_allowlist.split(",") if item)

    @model_validator(mode="after")
    def validate_procurement_worker_settings(self) -> "Settings":
        """
        对采购聊天、自动发送、模型密钥和固定回调配置执行失败关闭校验。

        输入已完成字段校验的配置并返回自身；开关依赖、密钥长度或 URL 不合法时抛出
        ValueError；不读取登录态、不连接外部服务且无副作用。
        """

        if self.procurement_auto_send_enabled and not self.procurement_chat_enabled:
            raise ValueError("PROCUREMENT_AUTO_SEND_ENABLED 依赖 PROCUREMENT_CHAT_ENABLED")
        callback_configured = bool((self.shopping_callback_url or "").strip())
        token_configured = self.shopping_procurement_token is not None
        if callback_configured != token_configured:
            raise ValueError("SHOPPING_CALLBACK_URL 与 SHOPPING_PROCUREMENT_TOKEN 必须同时配置")
        if token_configured:
            token = self.shopping_procurement_token
            assert token is not None
            if len(token.get_secret_value().strip()) < 32:
                raise ValueError("SHOPPING_PROCUREMENT_TOKEN 至少需要 32 字符")
        if callback_configured and not str(self.shopping_callback_url).startswith(
            ("http://", "https://")
        ):
            raise ValueError("SHOPPING_CALLBACK_URL 必须使用 HTTP 或 HTTPS")
        if self.procurement_chat_enabled:
            expected_account = (self.xianyu_expected_account_id or "").strip()
            if not expected_account:
                raise ValueError("采购聊天开启时必须配置 XIANYU_EXPECTED_ACCOUNT_ID")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_account):
                raise ValueError(
                    "XIANYU_EXPECTED_ACCOUNT_ID 必须是只读标定得到的 tracknick SHA-256"
                )
            if self.deepseek_api_key is None:
                raise ValueError("采购聊天开启时必须配置 DEEPSEEK_API_KEY")
            if not callback_configured:
                raise ValueError("采购聊天开启时必须配置固定商城回调与独立令牌")
        return self


@lru_cache
def get_settings() -> Settings:
    """
    返回进程级缓存配置。

    无输入；首次调用读取环境，配置不合法时抛出校验异常；副作用仅为内存缓存。
    """

    return Settings()
