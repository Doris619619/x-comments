"""
本文件负责串行执行采集任务并更新数据库状态和统计。

它属于 jobs 模块，连接爬虫与仓储；不实现页面选择器或 HTTP 协议。
"""

import asyncio
from contextlib import suppress
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.crawler.client import XianyuCrawler
from app.crawler.risk_control import RiskControlBlocked
from app.models.crawl_job import CrawlJob, CrawlJobStatus, utc_now
from app.repositories.items import ItemRepository


class CrawlWorker:
    """
    使用单队列串行运行任务，避免同一账号并发。

    输入会话工厂和配置；任务异常会安全落库；启动/停止会创建或取消后台协程。
    """

    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        """
        初始化空队列和爬虫依赖。

        输入会话工厂与配置；无返回；仅创建内存对象。
        """

        self.session_factory = session_factory
        self.crawler = XianyuCrawler(settings)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """
        启动唯一后台消费协程。

        无输入输出；无运行事件循环时抛出 RuntimeError；副作用为创建异步任务。
        """

        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """
        取消并等待后台消费协程结束。

        无输入输出；取消异常被抑制；副作用为停止 worker。
        """

        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    def enqueue(self, job_id: str) -> None:
        """
        将任务 ID 非阻塞加入单 worker 队列。

        输入任务 ID；队列满时抛出异常；副作用为修改进程内队列。
        """

        self.queue.put_nowait(job_id)

    async def _run(self) -> None:
        """
        持续串行消费任务直到取消。

        无输入输出；单任务异常在 `_execute` 内落库；副作用为消费队列。
        """

        while True:
            job_id = await self.queue.get()
            try:
                await self._execute(job_id)
            finally:
                self.queue.task_done()

    async def _execute(self, job_id: str) -> None:
        """
        执行一个任务并持久化准确终态。

        输入任务 ID；内部捕获任务错误；副作用为有限网络访问和数据库更新。
        """

        with self.session_factory() as session:
            job = session.get(CrawlJob, job_id)
            if job is None or job.status is not CrawlJobStatus.PENDING:
                return
            job.status = CrawlJobStatus.RUNNING
            job.started_at = utc_now()
            session.commit()
            keyword = job.keyword
        try:
            result = await self.crawler.collect(keyword)
            seen_at = datetime.now().astimezone()
            with self.session_factory() as session:
                stats = ItemRepository(session).upsert_many(keyword, result.items, seen_at)
                job = session.get(CrawlJob, job_id)
                if job is None:
                    return
                job.discovered_count = stats.discovered
                job.new_count = stats.new
                job.updated_count = stats.updated
                job.duplicate_count = stats.duplicate
                job.error_count = len(result.errors)
                job.error_message = "; ".join(result.errors[:5]) or None
                job.status = (
                    CrawlJobStatus.PARTIALLY_SUCCEEDED
                    if result.errors
                    else CrawlJobStatus.SUCCEEDED
                )
                job.finished_at = utc_now()
                session.commit()
        except RiskControlBlocked as exc:
            self._finish_error(job_id, CrawlJobStatus.BLOCKED, str(exc))
        except Exception as exc:
            self._finish_error(job_id, CrawlJobStatus.FAILED, f"采集执行失败：{type(exc).__name__}")

    def _finish_error(self, job_id: str, status: CrawlJobStatus, message: str) -> None:
        """
        将任务安全标记为失败或风控阻塞。

        输入任务、终态和脱敏消息；数据库错误向上抛出；副作用为提交状态。
        """

        with self.session_factory() as session:
            job = session.get(CrawlJob, job_id)
            if job is None:
                return
            job.status = status
            job.error_count += 1
            job.error_message = message[:1000]
            job.finished_at = utc_now()
            session.commit()
