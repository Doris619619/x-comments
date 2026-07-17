"""
本文件负责创建 SQLAlchemy 引擎、会话和声明式基类。

它属于 core 基础设施，被模型和仓储使用，不包含业务查询或迁移逻辑。
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """
    提供全部 ORM 模型共享的声明式基类。

    无业务输入输出；模型元数据注册是其唯一副作用。
    """


def build_engine(database_url: str) -> Engine:
    """
    根据数据库 URL 创建同步 SQLAlchemy 引擎。

    PostgreSQL URL 会创建同步引擎；非 PostgreSQL 或无效 URL 会抛出 ValueError/SQLAlchemy 异常。
    """

    if not database_url.startswith("postgresql+psycopg://"):
        raise ValueError("运行时仅支持 PostgreSQL psycopg 连接串")
    return create_engine(database_url, pool_pre_ping=True)


engine = build_engine(get_settings().database_url)
SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """
    为单次请求提供数据库会话。

    返回可迭代会话；数据库错误向上抛出；结束时总会关闭会话。
    """

    with SessionFactory() as session:
        yield session
