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
from app.models.catalog_sync import CrawlRunStatus
from app.models.crawl_job import CrawlJob, CrawlJobStatus, utc_now
from app.repositories.catalog_sync import CatalogSyncRepository
from app.repositories.jobs import JobRepository


class CrawlWorker:
    """
    使用单队列串行运行任务，避免同一账号并发。

    输入会话工厂和配置；任务异常会安全落库；启动/停止会创建或取消后台协程。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        account_lock: asyncio.Lock | None = None,
    ) -> None:
        """
        初始化空队列和爬虫依赖。

        输入会话工厂、配置与可选账号级锁；无返回；仅创建内存对象。
        """

        self.session_factory = session_factory
        self.settings = settings
        self.crawler = XianyuCrawler(settings, account_lock)
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
            queued_job = False
            job_id: str | None
            try:
                job_id = await asyncio.wait_for(
                    self.queue.get(), timeout=self.settings.worker_poll_seconds
                )
                queued_job = True
            except TimeoutError:
                job_id = self._find_oldest_pending_job_id()
            if job_id is None:
                continue
            try:
                await self._execute(job_id)
            finally:
                if queued_job:
                    self.queue.task_done()

    def _find_oldest_pending_job_id(self) -> str | None:
        """
        从持久化队列读取一个待认领任务，支持 API 与 worker 独立部署后的重启恢复。

        无输入；返回候选任务 ID 或 None；实际认领在执行前原子完成，因此本方法不产生状态写入。
        """

        with self.session_factory() as session:
            return JobRepository(session).find_oldest_pending_job_id()

    async def _execute(self, job_id: str) -> None:
        """
        执行一个任务并持久化准确终态。

        输入任务 ID；内部捕获任务错误；副作用为有限网络访问和数据库更新。
        """

        try:
            with self.session_factory() as session:
                job = JobRepository(session).claim_pending_job(job_id)
                if job is None:
                    return
                started_at = job.started_at
                if started_at is None:
                    raise ValueError("已认领任务缺少开始时间")
                keyword = job.keyword
                run = CatalogSyncRepository(session).begin_run(job_id, keyword, started_at)
                run_id = run.run_id
        except Exception as exc:
            self._finish_error(job_id, CrawlJobStatus.FAILED, f"采集启动失败：{type(exc).__name__}")
            return
        try:
            result = await asyncio.wait_for(
                self.crawler.collect(keyword),
                timeout=self.crawler.settings.xianyu_collect_timeout_seconds,
            )
            seen_at = datetime.now().astimezone()
            with self.session_factory() as session:
                catalog_repository = CatalogSyncRepository(session)
                if result.errors:
                    stats = catalog_repository.finish_incomplete_run(
                        run_id,
                        keyword,
                        result.items,
                        seen_at,
                        "; ".join(result.errors[:5]) or None,
                    )
                else:
                    published = catalog_repository.publish_complete_run(
                        run_id,
                        keyword,
                        result.items,
                        seen_at,
                        self.settings.catalog_missing_threshold,
                    )
                    stats = published.stats
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
        except TimeoutError:
            self._finish_error(
                job_id,
                CrawlJobStatus.FAILED,
                "采集超过安全时限，已停止并等待下一轮调度",
            )
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
            run_status = (
                CrawlRunStatus.BLOCKED
                if status is CrawlJobStatus.BLOCKED
                else CrawlRunStatus.FAILED
            )
            CatalogSyncRepository(session).finish_failed_run(
                job_id,
                run_status,
                message,
                job.finished_at,
            )
            session.commit()
