"""
本文件提供隔离 SQLite 数据库和 FastAPI 测试客户端夹具。

它属于 tests 基础设施，不访问真实网络或登录态。
"""

import os
from collections.abc import Generator

os.environ["DATABASE_URL"] = "postgresql+psycopg://xcomments:xcomments@127.0.0.1:5432/x_comments"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import create_app


@pytest.fixture
def session_factory() -> Generator[sessionmaker[Session], None, None]:
    """
    创建内存 SQLite 会话工厂。

    无输入，返回夹具；数据库错误会使测试失败；结束时释放引擎。
    """

    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    yield factory
    test_engine.dispose()


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Generator[TestClient, None, None]:
    """
    创建覆盖数据库依赖的测试客户端。

    输入内存会话工厂，返回客户端；请求错误由测试断言；不访问外部网络。
    """

    application = create_app(
        verification_token="offline-test-token-0123456789abcdef",
        catalog_sync_token="offline-sync-token-0123456789abcdef",
        procurement_api_token="offline-procurement-token-0123456789abcdef",
        procurement_source_item_allowlist=frozenset(
            {
                "81001",
                "81002",
                "81003",
                "81004",
                "81005",
                "81006",
                "81007",
                "89999",
            }
        ),
    )

    def override_db() -> Generator[Session, None, None]:
        """为一次测试请求提供内存会话，并在结束时关闭。"""

        with session_factory() as session:
            yield session

    application.dependency_overrides[get_db] = override_db
    with TestClient(application) as test_client:
        yield test_client
