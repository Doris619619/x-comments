"""
本文件负责从环境变量读取应用配置。

它属于 core 模块，为数据库、API 和爬虫提供只读配置，不读取登录态内容。
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    定义应用环境配置。

    输入来自环境变量和可选 `.env`；返回类型安全的配置对象。校验失败时抛出
    Pydantic 异常，没有外部副作用。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/app.sqlite3"
    xianyu_storage_state_path: str = "storage_state.json"
    xianyu_headless: bool = True
    xianyu_max_pages: int = Field(default=3, ge=1, le=3)
    xianyu_max_items: int = Field(default=50, ge=1, le=50)
    xianyu_page_delay_seconds: float = Field(default=2.0, ge=1.0, le=10.0)
    xianyu_collect_timeout_seconds: int = Field(default=120, ge=30, le=300)
    catalog_scheduler_interval_seconds: int = Field(default=600, ge=60, le=3600)
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """
    返回进程级缓存配置。

    无输入；首次调用读取环境，配置不合法时抛出校验异常；副作用仅为内存缓存。
    """

    return Settings()
