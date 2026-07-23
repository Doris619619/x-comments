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


def test_procurement_source_allowlist_is_normalized_and_rejects_unsafe_ids() -> None:
    """
    验证采购商品白名单去重、去空白并只接受稳定数字 item_id。

    无输入；断言失败抛出 AssertionError；配置校验不访问数据库或网络。
    """

    settings = Settings(
        procurement_source_item_allowlist=" 81001,81002,81001 ",
    )

    assert settings.procurement_source_item_allowlist == "81001,81002"
    assert settings.procurement_source_item_ids == frozenset({"81001", "81002"})

    with pytest.raises(ValidationError, match="只能包含数字"):
        Settings(procurement_source_item_allowlist="81001,not-an-item")


def test_chat_requires_hashed_account_identity() -> None:
    """
    验证开启聊天时拒绝账号昵称明文，只接受只读标定得到的 SHA-256。

    无输入；断言失败抛出 AssertionError；不读取真实 Cookie、登录态或外部服务。
    """

    common = {
        "procurement_chat_enabled": True,
        "deepseek_api_key": "test-key",
        "shopping_callback_url": "http://shopping.test/callback",
        "shopping_procurement_token": "t" * 32,
    }
    with pytest.raises(ValidationError, match="tracknick SHA-256"):
        Settings(**common, xianyu_expected_account_id="nickname-plaintext")

    settings = Settings(**common, xianyu_expected_account_id="a" * 64)
    assert settings.xianyu_expected_account_id == "a" * 64
