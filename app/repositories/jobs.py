"""
本文件负责采集任务的数据库读写。

它属于 repositories 模块，不执行 Playwright 或决定 HTTP 响应。
"""

from sqlalchemy import select, update
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

    def create(self, keyword: str, *, commit: bool = True) -> CrawlJob:
        """
        创建 pending 采集任务；默认提交，也可交由调用方与调度时间一并提交。

        输入清洗后的关键词和提交开关，返回任务；数据库错误向上抛出并产生写库副作用。
        """

        job = CrawlJob(keyword=keyword)
        self.session.add(job)
        self.session.flush()
        if commit:
            self.session.commit()
        self.session.refresh(job)
        return job

    def list_inflight_keywords(self) -> set[str]:
        """
        返回仍处于 pending 或 running 的关键词集合。

        无输入；数据库错误向上抛出；无写入副作用，用于避免同一关键词重复入队。
        """

        return set(
            self.session.scalars(
                select(CrawlJob.keyword).where(
                    CrawlJob.status.in_((CrawlJobStatus.PENDING, CrawlJobStatus.RUNNING))
                )
            )
        )

    def get(self, job_id: str) -> CrawlJob | None:
        """
        按任务 ID 查询任务。

        输入 UUID 字符串，返回任务或 None；数据库错误向上抛出，无写入副作用。
        """

        return self.session.get(CrawlJob, job_id)

    def claim_pending_job(self, job_id: str) -> CrawlJob | None:
        """
        原子认领一个仍处于 pending 的任务，避免多个 worker 重复执行。

        输入任务 ID；返回已改为 running 的任务或 None；数据库错误向上抛出，副作用为短事务
        更新任务开始时间和状态。
        """

        job = self.session.scalar(
            select(CrawlJob)
            .where(CrawlJob.job_id == job_id, CrawlJob.status == CrawlJobStatus.PENDING)
            .with_for_update()
        )
        if job is None:
            return None
        job.status = CrawlJobStatus.RUNNING
        job.started_at = utc_now()
        self.session.commit()
        self.session.refresh(job)
        return job

    def find_oldest_pending_job_id(self) -> str | None:
        """
        查找最早待执行任务的 ID，供独立 worker 轮询持久化队列。

        无输入；返回任务 ID 或 None；不会认领任务，实际并发保护由 claim_pending_job 完成；
        数据库异常向上抛出，无写入副作用。
        """

        return self.session.scalar(
            select(CrawlJob.job_id)
            .where(CrawlJob.status == CrawlJobStatus.PENDING)
            .order_by(CrawlJob.created_at.asc(), CrawlJob.job_id.asc())
            .limit(1)
        )

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
