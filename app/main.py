"""
本文件负责创建并装配 FastAPI 应用。

它属于应用入口，注册路由和元数据，不包含业务、解析或数据库查询实现。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import catalog_keywords, crawl_jobs, demo, health, items
from app.core.config import get_settings
from app.core.database import SessionFactory
from app.jobs.scheduler import CatalogScheduler
from app.jobs.worker import CrawlWorker
from app.repositories.catalog_keywords import CatalogKeywordRepository
from app.repositories.jobs import JobRepository


def create_app(start_worker: bool = False) -> FastAPI:
    """
    创建 FastAPI 应用并注册版本化路由。

    无输入，返回应用；装配路由是唯一副作用，不访问外部网络。
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """
        按配置启动并停止进程内单采集 worker。

        输入应用，产出生命周期控制；启动失败向上抛出；副作用为后台任务生命周期。
        """

        settings = get_settings()
        worker = CrawlWorker(SessionFactory, settings) if start_worker else None
        scheduler = (
            CatalogScheduler(SessionFactory, worker, settings.catalog_scheduler_interval_seconds)
            if worker is not None
            else None
        )
        application.state.crawl_worker = worker
        application.state.catalog_scheduler = scheduler
        if worker is not None:
            with SessionFactory() as session:
                CatalogKeywordRepository(session).ensure_defaults()
                JobRepository(session).recover_interrupted_jobs()
            worker.start()
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            if worker is not None:
                await worker.stop()

    application = FastAPI(
        title="闲鱼关键词采集 POC",
        description="仅用于验证关键词采集闭环，不保证全量覆盖。",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(health.router)
    application.include_router(crawl_jobs.router)
    application.include_router(items.router)
    application.include_router(catalog_keywords.router)
    application.include_router(demo.router)
    application.mount("/static", StaticFiles(directory=demo.STATIC_DIR), name="static")
    return application


app = create_app(start_worker=True)
