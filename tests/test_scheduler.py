"""
本文件测试杂货铺搜索清单的轮流调度规则。

它属于 jobs 测试模块，使用内存数据库和假 worker；不访问闲鱼、浏览器或真实登录态。
"""

from sqlalchemy.orm import Session, sessionmaker

from app.jobs.scheduler import CatalogScheduler
from app.models.catalog_keyword import CatalogKeyword
from app.models.crawl_job import CrawlJob


class FakeWorker:
    """
    记录调度器入队任务 ID 的测试替身。

    输入任务 ID 并保存在内存列表；不执行采集，没有外部副作用。
    """

    def __init__(self) -> None:
        """初始化空任务 ID 列表；无输入、返回和外部副作用。"""

        self.job_ids: list[str] = []

    def enqueue(self, job_id: str) -> None:
        """记录一个被调度的任务 ID；无异常；副作用为修改内存列表。"""

        self.job_ids.append(job_id)


def test_scheduler_enqueues_one_due_keyword_and_updates_schedule_time(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证一次全局调度最多创建一个任务并写入对应清单时间。

    输入内存会话工厂；断言失败抛出 AssertionError；副作用仅为内存数据库写入。
    """

    with session_factory() as session:
        session.add(CatalogKeyword(category="潮玩", keyword="手办", interval_minutes=60))
        session.add(CatalogKeyword(category="收藏", keyword="古董", interval_minutes=60))
        session.commit()

    worker = FakeWorker()
    scheduler = CatalogScheduler(session_factory, worker, interval_seconds=600)  # type: ignore[arg-type]
    job_id = scheduler.schedule_once()

    assert job_id is not None
    assert worker.job_ids == [job_id]
    with session_factory() as session:
        assert session.query(CrawlJob).count() == 1
        scheduled_count = (
            session.query(CatalogKeyword)
            .filter(CatalogKeyword.last_scheduled_at.is_not(None))
            .count()
        )
        assert scheduled_count == 1
