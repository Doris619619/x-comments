"""
本文件验证 PostgreSQL 作为 x-comments 唯一运行时数据库的配置边界。

它属于 tests 配置模块，只构造内存配置对象，不连接真实数据库、浏览器或网络。
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.database import build_engine


def test_settings_rejects_sqlite_runtime_url() -> None:
    """
    验证运行时配置拒绝旧 SQLite 连接串。

    无输入；断言失败抛出 AssertionError；不创建数据库连接或文件。
    """

    with pytest.raises(ValidationError, match="DATABASE_URL 必须使用"):
        Settings(database_url="sqlite:///./data/app.sqlite3")


def test_build_engine_rejects_non_postgresql_url() -> None:
    """
    验证底层引擎工厂同样拒绝绕过配置层的 SQLite URL。

    无输入；断言失败抛出 AssertionError；不创建数据库连接或文件。
    """

    with pytest.raises(ValueError, match="仅支持 PostgreSQL"):
        build_engine("sqlite:///./data/app.sqlite3")


def test_procurement_auto_send_defaults_closed_and_has_hard_limits() -> None:
    """
    验证采购自动发送默认关闭，且置信度和轮次不能被配置为不安全值。

    无输入；断言失败抛出 AssertionError；不访问数据库、浏览器或网络。
    """

    settings = Settings()
    assert settings.procurement_auto_send_enabled is False
    assert settings.procurement_auto_send_min_confidence == 0.85
    assert settings.procurement_max_auto_rounds == 3

    with pytest.raises(ValidationError):
        Settings(procurement_auto_send_min_confidence=0.5)
    with pytest.raises(ValidationError):
        Settings(procurement_max_auto_rounds=4)
