"""
本文件负责采集任务创建与查询业务规则。

它属于 services 模块，依赖仓储接口，不执行真实爬虫。
"""

from app.models.crawl_job import CrawlJob
from app.repositories.jobs import JobRepository


class CrawlJobService:
    """
    编排离线阶段的任务创建和查询。

    输入任务仓储；创建会写数据库，找不到任务时返回 None。
    """

    def __init__(self, repository: JobRepository) -> None:
        """
        注入任务仓储。

        输入仓储实例；无返回和异常；仅保存引用。
        """

        self.repository = repository

    def create(self, keyword: str) -> CrawlJob:
        """
        创建并立即返回 pending 任务。

        输入已校验关键词，返回任务；数据库异常向上抛出并产生写库副作用。
        """

        return self.repository.create(keyword)

    def get(self, job_id: str) -> CrawlJob | None:
        """
        查询任务状态。

        输入任务 ID，返回任务或 None；无写入副作用。
        """

        return self.repository.get(job_id)
