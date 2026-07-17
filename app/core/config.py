"""
本文件负责从环境变量读取应用配置。

它属于 core 模块，为数据库、API 和爬虫提供只读配置，不读取登录态内容。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    xianyu_page_delay_seconds: float = Field(default=2.0, ge=1.0, le=10.0)
    xianyu_collect_timeout_seconds: int = Field(default=120, ge=30, le=300)
    xianyu_verify_timeout_seconds: int = Field(default=12, ge=5, le=30)
    xianyu_api_token: SecretStr | None = None
    catalog_scheduler_interval_seconds: int = Field(default=600, ge=60, le=3600)
    app_role: Literal["api", "scheduler_worker"] = "api"
    worker_poll_seconds: int = Field(default=5, ge=1, le=60)
    catalog_missing_threshold: int = Field(default=2, ge=2, le=10)
    catalog_sync_token: SecretStr | None = None
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


@lru_cache
def get_settings() -> Settings:
    """
    返回进程级缓存配置。

    无输入；首次调用读取环境，配置不合法时抛出校验异常；副作用仅为内存缓存。
    """

    return Settings()
