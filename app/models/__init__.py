"""
本包集中导出数据库模型，确保 Alembic 能发现全部元数据。

它不执行查询或业务规则。
"""

from app.models.catalog_keyword import CatalogKeyword
from app.models.catalog_sync import (
    CatalogAvailability,
    CatalogChange,
    CatalogChangeType,
    CatalogItemState,
    CatalogRevision,
    CrawlRun,
    CrawlRunStatus,
)
from app.models.crawl_job import CrawlJob, CrawlJobStatus
from app.models.item import Item
from app.models.keyword import ItemKeyword, Keyword

__all__ = [
    "CatalogAvailability",
    "CatalogChange",
    "CatalogChangeType",
    "CatalogItemState",
    "CatalogKeyword",
    "CatalogRevision",
    "CrawlJob",
    "CrawlJobStatus",
    "CrawlRun",
    "CrawlRunStatus",
    "Item",
    "ItemKeyword",
    "Keyword",
]
