"""
本文件定义杂货铺采集清单的公开只读响应结构。

它属于 schemas 模块，被 API 路由序列化使用；不负责配置写入或调度判断。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CatalogKeywordRead(BaseModel):
    """
    表示一条可展示的杂货铺采集清单配置。

    输入 ORM 对象并输出 API 字段；无异常和副作用。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    keyword: str
    interval_minutes: int
    last_scheduled_at: datetime | None
    note: str | None
