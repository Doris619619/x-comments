"""
本文件用于人工验收时串行执行一次真实关键词采集任务。

它通过正式 worker 和数据库模型运行，不伪造数据；不输出或读取登录态具体内容。
"""

import argparse
import asyncio

from app.core.config import get_settings
from app.core.database import SessionFactory
from app.jobs.worker import CrawlWorker
from app.models.crawl_job import CrawlJob
from app.repositories.jobs import JobRepository


def build_parser() -> argparse.ArgumentParser:
    """
    创建真实验收命令行解析器。

    无输入，返回解析器；无异常或外部副作用。
    """

    parser = argparse.ArgumentParser(description="执行一次有限范围闲鱼关键词采集")
    parser.add_argument("--keyword", default="女生发饰")
    return parser


async def run(keyword: str) -> CrawlJob:
    """
    创建任务、交给单 worker 执行并返回最终任务。

    输入关键词；返回终态任务；网络或数据库初始化错误向上抛出并产生真实采集副作用。
    """

    with SessionFactory() as session:
        job = JobRepository(session).create(keyword)
        job_id = job.job_id
    worker = CrawlWorker(SessionFactory, get_settings())
    worker.start()
    try:
        worker.enqueue(job_id)
        await worker.queue.join()
    finally:
        await worker.stop()
    with SessionFactory() as session:
        result = session.get(CrawlJob, job_id)
        if result is None:
            raise RuntimeError("任务执行后记录不存在")
        session.expunge(result)
        return result


def main() -> None:
    """
    执行 CLI 并打印不含凭据的任务统计。

    无输入输出对象；失败时向命令行抛出异常；副作用为创建并执行一次任务。
    """

    args = build_parser().parse_args()
    job = asyncio.run(run(args.keyword))
    print(
        {
            "job_id": job.job_id,
            "keyword": job.keyword,
            "status": job.status.value,
            "discovered_count": job.discovered_count,
            "new_count": job.new_count,
            "updated_count": job.updated_count,
            "duplicate_count": job.duplicate_count,
            "error_count": job.error_count,
            "error_message": job.error_message,
        }
    )


if __name__ == "__main__":
    main()
