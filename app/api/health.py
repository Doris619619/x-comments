"""
本文件提供健康检查 API。

它属于 api 模块，只验证应用和数据库连通性，不执行采集任务。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.catalog_sync import CrawlRun, CrawlRunStatus
from app.repositories.catalog_sync import CatalogSyncRepository

router = APIRouter()


@router.get("/health", tags=["system"])
def health(session: Session = Depends(get_db)) -> dict[str, object]:
    """
    返回应用、数据库与采集发布的最小运维状态。

    输入请求级会话；数据库失败时由 FastAPI 返回错误；只执行只读查询。
    """

    session.execute(text("SELECT 1"))
    latest_revision = CatalogSyncRepository(session).latest_revision()
    completed_statuses = list(
        session.scalars(
            select(CrawlRun.status)
            .where(CrawlRun.finished_at.is_not(None))
            .order_by(CrawlRun.finished_at.desc())
            .limit(100)
        )
    )
    consecutive_failed_runs = 0
    for run_status in completed_statuses:
        if run_status is CrawlRunStatus.SUCCEEDED:
            break
        consecutive_failed_runs += 1
    last_successful_crawl_at = session.scalar(
        select(CrawlRun.finished_at)
        .where(CrawlRun.status == CrawlRunStatus.SUCCEEDED)
        .order_by(CrawlRun.finished_at.desc())
        .limit(1)
    )
    return {
        "status": "ok",
        "database": "ok",
        "last_successful_crawl_at": last_successful_crawl_at,
        "last_published_revision": latest_revision.revision if latest_revision is not None else 0,
        "last_published_at": latest_revision.published_at if latest_revision is not None else None,
        "consecutive_failed_runs": consecutive_failed_runs,
    }
