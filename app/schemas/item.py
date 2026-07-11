"""
本文件定义解析商品、商品响应和分页响应结构。

它属于 schemas 模块，不负责抓取、持久化或业务状态变更。
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class ParsedItem(BaseModel):
    """
    表示爬虫解析后、写库前的标准商品。

    字段不合法时抛出校验异常；没有副作用。
    """

    item_id: str = Field(pattern=r"^\d+$")
    title: str = Field(min_length=1)
    price: Decimal = Field(ge=0)
    image_url: HttpUrl | None = None
    item_url: HttpUrl
    location: str | None = None
    source: str = "xianyu"

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        """
        折叠标题空白并拒绝空标题。

        输入标题并返回清洗值；空标题抛出 ValueError，无副作用。
        """

        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("标题不能为空")
        return cleaned


class ItemRead(BaseModel):
    """
    序列化数据库商品。

    输入 ORM 对象并输出公开字段；无副作用。
    """

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    title: str
    price: Decimal
    image_url: str | None
    item_url: str
    location: str | None
    source: str
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class ItemPage(BaseModel):
    """
    表示商品分页结果。

    输入列表和分页元数据，返回 API 响应；无副作用。
    """

    items: list[ItemRead]
    page: int
    page_size: int
    total: int
    pages: int
