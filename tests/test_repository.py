"""
本文件测试商品唯一性、多关键词关联、分页过滤和时间更新。

它使用内存 SQLite，不访问真实页面或外部网络。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.crawl_job import CrawlJob, CrawlJobStatus
from app.models.item import Item
from app.models.keyword import ItemKeyword
from app.repositories.items import ItemRepository
from app.repositories.jobs import JobRepository
from app.schemas.item import ParsedItem


def make_item(item_id: str = "10001") -> ParsedItem:
    """
    构造仓储测试用标准商品。

    输入可选 ID，返回内存对象；校验失败会抛出 Pydantic 异常；无副作用。
    """

    return ParsedItem(
        item_id=item_id,
        title=f"发饰 {item_id}",
        price=Decimal("12.80"),
        image_url=f"https://example.invalid/{item_id}.jpg",
        image_urls=[f"https://example.invalid/{item_id}.jpg"],
        item_url=f"https://www.goofish.com/item?id={item_id}",
        location="杭州",
    )


def test_upsert_deduplicates_and_links_keywords(session_factory: sessionmaker[Session]) -> None:
    """
    验证商品全局去重、last_seen 更新和多关键词关联。

    输入会话工厂；断言失败抛出 AssertionError；副作用仅在内存数据库。
    """

    first = datetime.now(UTC)
    second = first + timedelta(minutes=1)
    with session_factory() as session:
        repository = ItemRepository(session)
        assert repository.upsert_many("女生发饰", [make_item()], first).new == 1
        stats = repository.upsert_many("发夹", [make_item()], second)
        assert stats.new == 0
        assert stats.updated == 1
        assert stats.duplicate == 1
        assert session.scalar(select(func.count()).select_from(Item)) == 1
        assert session.scalar(select(func.count()).select_from(ItemKeyword)) == 2
        stored = session.get(Item, "10001")
        assert stored is not None
        assert stored.last_seen_at.replace(tzinfo=UTC) == second


def test_pagination_and_keyword_filter(session_factory: sessionmaker[Session]) -> None:
    """
    验证分页总数、页数和关键词过滤。

    输入会话工厂；断言失败抛出 AssertionError；副作用仅在内存数据库。
    """

    now = datetime.now(UTC)
    with session_factory() as session:
        repository = ItemRepository(session)
        repository.upsert_many("女生发饰", [make_item("1"), make_item("2"), make_item("3")], now)
        repository.upsert_many("其他", [make_item("4")], now)
        rows, total, pages = repository.list_page(1, 2, "女生发饰")
        assert len(rows) == 2
        assert total == 3
        assert pages == 2


def test_recover_interrupted_jobs_marks_running_job_failed(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证服务重启时遗留的 running 任务会结束，避免阻塞后续采集。

    输入内存会话工厂；断言失败抛出 AssertionError；副作用仅为内存任务状态写入。
    """

    with session_factory() as session:
        job = JobRepository(session).create("遥控器")
        job.status = CrawlJobStatus.RUNNING
        session.commit()
        recovered = JobRepository(session).recover_interrupted_jobs()
        refreshed = session.get(CrawlJob, job.job_id)

    assert recovered == 1
    assert refreshed is not None
    assert refreshed.status is CrawlJobStatus.FAILED
    assert refreshed.error_message == "采集进程已重启，上一轮任务未完成而安全停止"
