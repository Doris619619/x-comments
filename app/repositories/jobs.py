"""
本文件负责采集任务的数据库读写。

它属于 repositories 模块，不执行 Playwright 或决定 HTTP 响应。
"""

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.crawl_job import CrawlJob, CrawlJobStatus, utc_now


class JobRepository:
    """
    封装采集任务持久化操作。

    输入 SQLAlchemy 会话；数据库错误向上抛出，提交事务会修改数据库。
    """

    def __init__(self, session: Session) -> None:
        """
        保存请求级数据库会话。

        输入有效会话；无返回和异常；仅保存引用。
        """

        self.session = session

    def create(self, keyword: str) -> CrawlJob:
        """
        创建 pending 采集任务并提交。

        输入清洗后的关键词，返回任务；数据库错误向上抛出并产生写库副作用。
        """

        job = CrawlJob(keyword=keyword)
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def get(self, job_id: str) -> CrawlJob | None:
        """
        按任务 ID 查询任务。

        输入 UUID 字符串，返回任务或 None；数据库错误向上抛出，无写入副作用。
        """

        return self.session.get(CrawlJob, job_id)

    def recover_interrupted_jobs(self) -> int:
        """
        将进程退出后遗留的 running 任务显式标记为失败。

        返回：恢复的任务数。异常：数据库错误向上抛出。副作用：更新任务终态并提交事务。
        """

        result = self.session.execute(
            update(CrawlJob)
            .where(CrawlJob.status == CrawlJobStatus.RUNNING)
            .values(
                status=CrawlJobStatus.FAILED,
                error_message="采集进程已重启，上一轮任务未完成而安全停止",
                error_count=CrawlJob.error_count + 1,
                finished_at=utc_now(),
            )
        )
        self.session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
