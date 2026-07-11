"""
本文件提供采集任务创建与状态查询 API。

它属于 api 模块，只做校验、服务调用和异常映射，不执行 Playwright。
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.jobs import JobRepository
from app.schemas.crawl_job import CrawlJobCreate, CrawlJobRead
from app.services.jobs import CrawlJobService

router = APIRouter(prefix="/api/v1/crawl-jobs", tags=["crawl-jobs"])


def get_job_service(session: Session = Depends(get_db)) -> CrawlJobService:
    """
    为请求装配采集任务服务。

    输入数据库会话并返回服务；无额外异常或外部副作用。
    """

    return CrawlJobService(JobRepository(session))


@router.post("", response_model=CrawlJobRead, status_code=status.HTTP_202_ACCEPTED)
def create_job(
    payload: CrawlJobCreate,
    request: Request,
    service: CrawlJobService = Depends(get_job_service),
) -> object:
    """
    创建 pending 任务并立即返回任务 ID。

    输入关键词请求，返回任务；校验/数据库错误向上抛出；副作用为创建任务记录。
    """

    job = service.create(payload.keyword)
    worker = getattr(request.app.state, "crawl_worker", None)
    if worker is not None:
        worker.enqueue(job.job_id)
    return job


@router.get("/{job_id}", response_model=CrawlJobRead)
def get_job(job_id: str, service: CrawlJobService = Depends(get_job_service)) -> object:
    """
    返回指定任务状态和统计。

    输入任务 ID；不存在时抛出 404；无写入副作用。
    """

    job = service.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="采集任务不存在")
    return job
