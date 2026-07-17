"""
本文件负责创建并装配 FastAPI 应用。

它属于应用入口，注册路由和元数据，不包含业务、解析或数据库查询实现。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import catalog_keywords, catalog_sync, crawl_jobs, demo, health, items
from app.core.config import get_settings
from app.core.database import SessionFactory
from app.crawler.item_verifier import XianyuItemVerifier
from app.jobs.scheduler import CatalogScheduler
from app.jobs.worker import CrawlWorker
from app.repositories.catalog_keywords import CatalogKeywordRepository
from app.repositories.jobs import JobRepository
from app.services.item_verification import LiveItemVerifier


def create_app(
    start_worker: bool | None = None,
    item_verifier: LiveItemVerifier | None = None,
    verification_token: str | None = None,
    catalog_sync_token: str | None = None,
) -> FastAPI:
    """
    创建 FastAPI 应用并注册版本化路由。

    输入可选的 worker 覆盖开关、核验器和服务端令牌，返回应用；未覆盖时仅
    scheduler_worker 角色启动 worker，装配阶段不访问外部网络。
    """

    settings = get_settings()
    account_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """
        按配置启动并停止进程内单采集 worker。

        输入应用，产出生命周期控制；启动失败向上抛出；副作用为后台任务生命周期。
        """

        should_start_worker = (
            start_worker if start_worker is not None else settings.app_role == "scheduler_worker"
        )
        worker = (
            CrawlWorker(SessionFactory, settings, account_lock) if should_start_worker else None
        )
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
    application.include_router(catalog_sync.router)
    application.include_router(catalog_keywords.router)
    application.include_router(demo.router)
    application.mount("/static", StaticFiles(directory=demo.STATIC_DIR), name="static")
    configured_token = (
        verification_token.strip()
        if verification_token is not None
        else settings.xianyu_api_token.get_secret_value().strip()
        if settings.xianyu_api_token is not None
        else ""
    )
    if len(configured_token) < 32:
        configured_token = ""
    application.state.xianyu_account_lock = account_lock
    application.state.item_verifier = item_verifier or XianyuItemVerifier(settings, account_lock)
    application.state.item_verification_token = configured_token or None
    configured_sync_token = (
        catalog_sync_token.strip()
        if catalog_sync_token is not None
        else settings.catalog_sync_token.get_secret_value().strip()
        if settings.catalog_sync_token is not None
        else ""
    )
    if len(configured_sync_token) < 32:
        configured_sync_token = ""
    application.state.catalog_sync_token = configured_sync_token or None
    return application


app = create_app()
