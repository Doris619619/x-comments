"""
本文件定义采集任务请求与响应结构。

它属于 schemas 模块，只负责校验和序列化，不创建或运行任务。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.crawl_job import CrawlJobStatus


class CrawlJobCreate(BaseModel):
    """
    校验创建任务输入。

    关键词为空或超长时抛出校验异常；没有副作用。
    """

    keyword: str = Field(min_length=1, max_length=100)

    @field_validator("keyword")
    @classmethod
    def normalize_keyword(cls, value: str) -> str:
        """
        去除关键词首尾空白并拒绝空值。

        输入原始关键词，返回清洗值；空值抛出 ValueError，无副作用。
        """

        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("关键词不能为空")
        return normalized


class CrawlJobRead(BaseModel):
    """
    序列化采集任务及统计。

    输入 ORM 对象并返回 JSON 兼容结构；无副作用。
    """

    model_config = ConfigDict(from_attributes=True)

    job_id: str
    keyword: str
    status: CrawlJobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    discovered_count: int
    new_count: int
    updated_count: int
    duplicate_count: int
    error_count: int
    error_message: str | None
