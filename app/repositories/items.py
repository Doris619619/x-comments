"""
本文件负责商品、关键词关联及分页查询。

它属于 repositories 模块，不解析页面、不创建 HTTP 响应。
"""

import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog_keyword import CatalogKeyword
from app.models.item import Item
from app.models.keyword import ItemKeyword, Keyword
from app.schemas.item import ParsedItem


@dataclass(frozen=True)
class UpsertStats:
    """
    表示一次商品批量写入统计。

    由仓储返回；无异常和副作用。
    """

    discovered: int
    new: int
    updated: int
    duplicate: int


class ItemRepository:
    """
    封装商品与关键词关联的持久化操作。

    输入 SQLAlchemy 会话；数据库错误向上抛出，写方法会提交事务。
    """

    def __init__(self, session: Session) -> None:
        """
        保存请求或任务级数据库会话。

        输入有效会话；无返回；仅保存引用。
        """

        self.session = session

    def upsert_many(
        self, keyword_value: str, items: list[ParsedItem], seen_at: datetime
    ) -> UpsertStats:
        """
        按商品 ID 写入或更新商品，并维护关键词关联。

        输入关键词、商品和观察时间；返回统计；数据库错误回滚后向上抛出。
        """

        normalized = keyword_value.casefold().strip()
        keyword = self.session.scalar(select(Keyword).where(Keyword.normalized_value == normalized))
        if keyword is None:
            keyword = Keyword(normalized_value=normalized, display_value=keyword_value)
            self.session.add(keyword)
            self.session.flush()
        new = updated = duplicate = 0
        seen_ids: set[str] = set()
        try:
            for parsed in items:
                if parsed.item_id in seen_ids:
                    duplicate += 1
                    continue
                seen_ids.add(parsed.item_id)
                existing = self.session.get(Item, parsed.item_id)
                if existing is None:
                    existing = Item(
                        item_id=parsed.item_id,
                        title=parsed.title,
                        price=parsed.price,
                        image_url=str(parsed.image_url) if parsed.image_url else None,
                        item_url=str(parsed.item_url),
                        location=parsed.location,
                        source=parsed.source,
                        first_seen_at=seen_at,
                        last_seen_at=seen_at,
                    )
                    self.session.add(existing)
                    new += 1
                else:
                    changed = any(
                        (
                            existing.title != parsed.title,
                            existing.price != parsed.price,
                            existing.image_url
                            != (str(parsed.image_url) if parsed.image_url else None),
                            existing.item_url != str(parsed.item_url),
                            existing.location != parsed.location,
                        )
                    )
                    existing.title = parsed.title
                    existing.price = parsed.price
                    existing.image_url = str(parsed.image_url) if parsed.image_url else None
                    existing.item_url = str(parsed.item_url)
                    existing.location = parsed.location
                    existing.last_seen_at = seen_at
                    updated += 1
                    if not changed:
                        duplicate += 1
                self.session.flush()
                link = self.session.get(ItemKeyword, (parsed.item_id, keyword.id))
                if link is None:
                    self.session.add(
                        ItemKeyword(
                            item_id=parsed.item_id,
                            keyword_id=keyword.id,
                            first_seen_at=seen_at,
                            last_seen_at=seen_at,
                        )
                    )
                else:
                    link.last_seen_at = seen_at
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return UpsertStats(len(items), new, updated, duplicate)

    def list_page(
        self, page: int, page_size: int, keyword: str | None, category: str | None = None
    ) -> tuple[list[Item], int, int]:
        """
        返回稳定排序的商品分页数据。

        输入页码、大小、可选关键词和分类，返回商品、总数、总页数；无写入副作用。
        """

        query = select(Item)
        count_query = select(func.count(func.distinct(Item.item_id))).select_from(Item)
        if keyword:
            normalized = keyword.casefold().strip()
            query = (
                query.join(ItemKeyword).join(Keyword).where(Keyword.normalized_value == normalized)
            )
            count_query = (
                count_query.join(ItemKeyword)
                .join(Keyword)
                .where(Keyword.normalized_value == normalized)
            )
        if category:
            query = (
                query.join(ItemKeyword)
                .join(Keyword)
                .join(CatalogKeyword, CatalogKeyword.keyword == Keyword.display_value)
                .where(CatalogKeyword.category == category)
                .distinct()
            )
            count_query = (
                count_query.join(ItemKeyword)
                .join(Keyword)
                .join(CatalogKeyword, CatalogKeyword.keyword == Keyword.display_value)
                .where(CatalogKeyword.category == category)
            )
        total = int(self.session.scalar(count_query) or 0)
        rows = list(
            self.session.scalars(
                query.order_by(Item.last_seen_at.desc(), Item.item_id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return rows, total, math.ceil(total / page_size) if total else 0

    def get(self, item_id: str) -> Item | None:
        """
        按商品 ID 查询公开商品。

        输入商品 ID，返回商品或 None；数据库错误向上抛出，无写入副作用。
        """

        return self.session.get(Item, item_id)

    def exists(self, item_id: str) -> bool:
        """
        判断指定商品 ID 是否存在，供不需要 ORM 对象的业务服务使用。

        输入商品 ID，返回布尔值；数据库错误向上抛出，无写入副作用。
        """

        return self.session.scalar(select(Item.item_id).where(Item.item_id == item_id)) is not None
