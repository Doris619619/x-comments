"""
本文件定义采集任务 ORM 模型及状态枚举。

它属于 models 模块，只描述持久化结构，不负责执行任务或状态转换。
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utc_now() -> datetime:
    """
    返回当前 UTC 时间。

    无输入和异常；无外部副作用，用作 ORM 默认值。
    """

    return datetime.now().astimezone()


class CrawlJobStatus(StrEnum):
    """
    定义采集任务可持久化状态。

    枚举无副作用；非法值由 SQLAlchemy/Pydantic 拒绝。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"
    BLOCKED = "blocked_by_auth_or_risk_control"


class CrawlJob(Base):
    """
    保存一次关键词采集任务及统计。

    由服务层创建和更新；数据库约束异常向上抛出，持久化是其副作用。
    """

    __tablename__ = "crawl_jobs"

    job_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    keyword: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[CrawlJobStatus] = mapped_column(
        Enum(CrawlJobStatus, native_enum=False), default=CrawlJobStatus.PENDING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
