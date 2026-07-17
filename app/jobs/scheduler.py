"""
本文件负责按固定全局间隔轮流创建杂货铺采集任务。

它属于 jobs 模块，只从持久化清单选择一个到期搜索词并交给既有单 worker；不解析页面或管理数据库商品。
"""

import asyncio
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.jobs.worker import CrawlWorker
from app.repositories.catalog_keywords import CatalogKeywordRepository
from app.repositories.jobs import JobRepository


class CatalogScheduler:
    """
    使用单一全局节奏轮流调度配置的杂货铺搜索词。

    输入会话工厂、采集 worker 和间隔秒数；启动后在后台创建任务；不并发执行网页采集。
    """

    def __init__(
        self, session_factory: sessionmaker[Session], worker: CrawlWorker, interval_seconds: int
    ) -> None:
        """
        初始化调度依赖与空后台任务引用。

        参数：session_factory、worker 和大于零的 interval_seconds。
        返回：无。副作用：仅创建内存对象。
        """

        self.session_factory = session_factory
        self.worker = worker
        self.interval_seconds = interval_seconds
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """
        启动后台调度协程。

        返回：无。异常：无事件循环时抛出 RuntimeError。副作用：创建后台任务。
        """

        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """
        取消并等待后台调度协程。

        返回：无。异常：取消异常被安全处理。副作用：停止调度。
        """

        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def _run(self) -> None:
        """
        立即尝试一次后，按全局间隔持续选择一个到期搜索词。

        返回：无。异常：单次调度错误会在下一周期重试。副作用：可能创建任务并入队。
        """

        while True:
            self.schedule_once()
            await asyncio.sleep(self.interval_seconds)

    def schedule_once(self) -> str | None:
        """
        为一个到期配置创建任务并加入单 worker 队列。

        返回：创建的任务 ID；没有到期项时返回 None。
        异常：数据库错误向上抛出。副作用：写任务、更新调度时间、入队。
        """

        now = datetime.now().astimezone()
        try:
            with self.session_factory() as session:
                catalog_repository = CatalogKeywordRepository(session)
                job_repository = JobRepository(session)
                config = catalog_repository.get_next_due(
                    now, job_repository.list_inflight_keywords()
                )
                if config is None:
                    return None
                job = job_repository.create(config.keyword, commit=False)
                catalog_repository.mark_scheduled(config.id, now, commit=False)
                session.commit()
        except IntegrityError:
            return None
        self.worker.enqueue(job.job_id)
        return job.job_id
