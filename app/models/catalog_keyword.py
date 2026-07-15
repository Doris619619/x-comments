"""
本文件定义杂货铺采集清单的 ORM 模型。

它属于 models 模块，保存哪些公开搜索词可被定时采集；不负责调度、页面访问或 API 响应。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.crawl_job import utc_now


class CatalogKeyword(Base):
    """
    保存杂货铺分类、搜索词和最近一次调度时间。

    输入由配置仓储提供；数据库唯一约束保证搜索词不重复；持久化为其副作用。
    """

    __tablename__ = "catalog_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    keyword: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
