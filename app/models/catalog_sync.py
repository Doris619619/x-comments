"""
本文件定义采集批次、商品清单状态和跨服务同步版本的 ORM 模型。

它属于 models 模块，为采集发布事务和只读 Catalog Sync API 提供持久化结构；不负责
Playwright 访问、状态转换算法或 HTTP 响应。
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.crawl_job import utc_now


class CatalogAvailability(StrEnum):
    """
    定义商城同步使用的公开商品可用状态。

    枚举值仅表示持久化语义；状态转换由仓储层处理，非法值由 SQLAlchemy/Pydantic 拒绝。
    """

    ACTIVE = "active"
    SUSPECTED_MISSING = "suspected_missing"
    SOLD = "sold"
    OFF_SHELF = "off_shelf"
    UNKNOWN = "unknown"


class CrawlRunStatus(StrEnum):
    """
    定义一次采集运行的终态和执行中状态。

    该枚举不访问数据库；运行状态由 worker 根据采集结果写入。
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"
    BLOCKED = "blocked_by_auth_or_risk_control"


class CatalogChangeType(StrEnum):
    """
    定义同步增量事件的两类稳定语义。

    `UPSERT` 表示商品字段或最近观察时间可更新，`AVAILABILITY_CHANGED` 表示公开可用状态变化。
    """

    UPSERT = "upsert"
    AVAILABILITY_CHANGED = "availability_changed"


class CrawlRun(Base):
    """
    保存一次采集任务是否完整成功且可用于缺失判断。

    由 worker 创建并由发布仓储更新；持久化是副作用，不能替代原有 crawl_jobs 的用户任务记录。
    """

    __tablename__ = "crawl_runs"

    run_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("crawl_jobs.job_id"), unique=True, index=True)
    catalog_keyword_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_keywords.id"), index=True
    )
    status: Mapped[CrawlRunStatus] = mapped_column(
        Enum(CrawlRunStatus, native_enum=False), default=CrawlRunStatus.RUNNING, index=True
    )
    is_comparable: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class CatalogItemState(Base):
    """
    保存某商品在一个采集清单关键词下的最近可见性。

    它不能用商品全局 last_seen_at 代替，因为同一商品可能同时命中多个关键词；状态由发布事务更新。
    """

    __tablename__ = "catalog_item_states"
    __table_args__ = (UniqueConstraint("item_id", "catalog_keyword_id"),)

    item_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), primary_key=True)
    catalog_keyword_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_keywords.id"), primary_key=True
    )
    availability: Mapped[CatalogAvailability] = mapped_column(
        Enum(CatalogAvailability, native_enum=False), default=CatalogAvailability.ACTIVE, index=True
    )
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_checked_run_id: Mapped[str | None] = mapped_column(ForeignKey("crawl_runs.run_id"))
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CatalogRevision(Base):
    """
    表示 shopping 可读取的一次完整、原子发布版本。

    revision 由数据库递增生成；只有完整成功采集才能创建该记录。
    """

    __tablename__ = "catalog_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_run_id: Mapped[str] = mapped_column(ForeignKey("crawl_runs.run_id"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="published")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CatalogChange(Base):
    """
    保存一个 revision 中可供 shopping 幂等应用的单商品增量事件。

    由发布事务创建；它保存展示所需快照，不暴露源商品 URL、登录态或内部数据库信息。
    """

    __tablename__ = "catalog_changes"
    __table_args__ = (UniqueConstraint("revision", "item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision: Mapped[int] = mapped_column(ForeignKey("catalog_revisions.revision"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), index=True)
    change_type: Mapped[CatalogChangeType] = mapped_column(
        Enum(CatalogChangeType, native_enum=False)
    )
    availability: Mapped[CatalogAvailability] = mapped_column(
        Enum(CatalogAvailability, native_enum=False), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    image_url: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(100))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
