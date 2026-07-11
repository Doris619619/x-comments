"""
本文件定义闲鱼商品 ORM 模型。

它属于 models 模块，负责字段和唯一约束，不负责解析、清洗或分页查询。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.crawl_job import utc_now


class Item(Base):
    """
    保存以闲鱼商品 ID 唯一标识的公开商品。

    由仓储 upsert；数据库约束异常向上抛出，持久化是其副作用。
    """

    __tablename__ = "items"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    image_url: Mapped[str | None] = mapped_column(Text)
    item_url: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(32), default="xianyu")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    keyword_links: Mapped[list["ItemKeyword"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


from app.models.keyword import ItemKeyword  # noqa: E402
