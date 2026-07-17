"""
本文件在真实 PostgreSQL 中验证调度器的数据库级同关键词并发约束。

它属于集成测试模块，只在 POSTGRES_TEST_URL 已配置且目标库已完成 Alembic 迁移时执行；
不访问闲鱼、浏览器或登录态，也不替代本地快速单元测试。
"""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.jobs.scheduler import CatalogScheduler
from app.models.catalog_keyword import CatalogKeyword
from app.models.crawl_job import CrawlJob


class RecordingWorker:
    """
    记录调度器提交的任务 ID，作为不访问 Playwright 的 worker 替身。

    输入任务 ID；记录入队顺序；不执行网络或数据库操作。
    """

    def __init__(self) -> None:
        """初始化空的任务 ID 记录列表；无输入输出和外部副作用。"""

        self.job_ids: list[str] = []

    def enqueue(self, job_id: str) -> None:
        """记录一个应由真实 worker 执行的任务 ID；无异常，副作用为内存列表追加。"""

        self.job_ids.append(job_id)


def _postgres_session_factory() -> tuple[sessionmaker[Session], object]:
    """
    连接已迁移的 PostgreSQL 测试库并清空业务表。

    无输入；返回会话工厂与待释放引擎；未配置 URL 或迁移缺失时跳过/失败；副作用为清理测试库。
    """

    database_url = os.environ.get("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("未配置 POSTGRES_TEST_URL，跳过 PostgreSQL 集成测试")
    engine = create_engine(database_url, pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("POSTGRES_TEST_URL 必须指向 PostgreSQL")
    with engine.begin() as connection:
        required_index = connection.scalar(
            text("SELECT to_regclass('public.uq_crawl_jobs_inflight_keyword')")
        )
        if required_index is None:
            pytest.fail("PostgreSQL 未执行至包含 uq_crawl_jobs_inflight_keyword 的 Alembic head")
        connection.execute(
            text(
                "TRUNCATE TABLE catalog_changes, catalog_revisions, catalog_item_states, "
                "crawl_runs, item_keywords, items, crawl_jobs, catalog_keywords, keywords "
                "RESTART IDENTITY CASCADE"
            )
        )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False), engine


@pytest.mark.postgresql
def test_two_scheduler_instances_create_only_one_inflight_job_for_one_keyword() -> None:
    """
    验证两个同时启动的调度器不会为同一到期关键词创建两条进行中任务。

    无输入；断言 PostgreSQL 部分唯一索引与事务竞争只允许一个成功；副作用仅限独立测试库。
    """

    session_factory, engine = _postgres_session_factory()
    try:
        with session_factory() as session:
            session.add(
                CatalogKeyword(category="集成测试", keyword="并发手办", interval_minutes=10)
            )
            session.commit()

        barrier = Barrier(2)
        first_worker = RecordingWorker()
        second_worker = RecordingWorker()
        first_scheduler = CatalogScheduler(session_factory, first_worker, interval_seconds=600)  # type: ignore[arg-type]
        second_scheduler = CatalogScheduler(session_factory, second_worker, interval_seconds=600)  # type: ignore[arg-type]

        def schedule_once(scheduler: CatalogScheduler) -> str | None:
            """等待两个线程对齐后执行一次调度；返回任务 ID 或 None；会写入测试数据库。"""

            barrier.wait(timeout=10)
            return scheduler.schedule_once()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(schedule_once, (first_scheduler, second_scheduler))
            )

        assert sum(result is not None for result in results) == 1
        assert len(first_worker.job_ids) + len(second_worker.job_ids) == 1
        with session_factory() as session:
            jobs = list(session.query(CrawlJob).filter_by(keyword="并发手办").all())
            assert len(jobs) == 1
            assert jobs[0].status.value == "pending"
    finally:
        engine.dispose()
