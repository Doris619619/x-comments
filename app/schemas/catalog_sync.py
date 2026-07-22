"""
本文件定义 Catalog Sync 只读 API 的稳定请求和响应结构。

它属于 schemas 模块，只承担跨服务数据契约；不查询数据库、不解析页面，也不保存令牌。
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.models.catalog_sync import CatalogAvailability, CatalogChangeType


class CatalogRevisionRead(BaseModel):
    """
    表示当前对 shopping 可见的最新发布版本。

    空目录使用 revision 为 0、published_at 为 None；模型没有副作用。
    """

    revision: int
    published_at: datetime | None
    source: str = "xianyu"
    status: str


class CatalogChangeRead(BaseModel):
    """
    表示 shopping 可幂等写入的一条商品同步变更。

    价格保留为十进制数，币种固定为 CNY；模型不包含闲鱼原始链接、登录态或内部 ID。
    """

    revision: int
    change_type: CatalogChangeType
    item_id: str
    availability: CatalogAvailability
    title: str
    price: Decimal
    currency: str
    image_url: str | None
    image_urls: list[str]
    location: str | None
    last_seen_at: datetime
    status_changed_at: datetime


class CatalogChangePageRead(BaseModel):
    """
    表示一个不切断 revision 边界的增量同步页面。

    shopping 只应在本地事务成功后把游标推进到 to_revision；模型没有副作用。
    """

    from_revision: int
    to_revision: int
    has_more: bool
    changes: list[CatalogChangeRead]


class CatalogSyncItemRead(BaseModel):
    """
    表示某商品当前最近一次已发布的同步快照。

    用于 shopping 在恢复或人工诊断时按 item_id 查询；模型没有副作用。
    """

    revision: int
    item_id: str
    availability: CatalogAvailability
    title: str
    price: Decimal
    currency: str
    image_url: str | None
    image_urls: list[str]
    location: str | None
    last_seen_at: datetime
    status_changed_at: datetime


class CatalogSyncSnapshotPageRead(BaseModel):
    """
    表示游标失效后可分页读取的当前全量商品同步快照。

    响应每项复用 CatalogChangeRead，shopping 完成全部页面后才能推进本地游标；无副作用。
    """

    items: list[CatalogChangeRead]
    page: int
    page_size: int
    total: int
    pages: int
