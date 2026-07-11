"""
本文件定义关键词及商品关键词多对多关联模型。

它属于 models 模块，保证同一商品可关联多个关键词，不负责规范化输入。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.crawl_job import utc_now


class Keyword(Base):
    """
    保存规范化关键词和展示值。

    由仓储创建；唯一约束冲突由数据库处理，持久化是其副作用。
    """

    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    normalized_value: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    display_value: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    item_links: Mapped[list["ItemKeyword"]] = relationship(
        back_populates="keyword", cascade="all, delete-orphan"
    )


class ItemKeyword(Base):
    """
    记录商品与关键词之间的多对多关系及发现时间。

    输入由仓储提供；复合唯一约束防止重复关联，写库是其副作用。
    """

    __tablename__ = "item_keywords"
    __table_args__ = (UniqueConstraint("item_id", "keyword_id"),)

    item_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), primary_key=True)
    keyword_id: Mapped[int] = mapped_column(ForeignKey("keywords.id"), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    item: Mapped["Item"] = relationship(back_populates="keyword_links")
    keyword: Mapped[Keyword] = relationship(back_populates="item_links")


from app.models.item import Item  # noqa: E402
